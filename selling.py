"""Sales, quotations and returns. All posting is committed by the API layer."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, func, text

from models import (
    Customer, HeldBill, InventoryBalance, Product, Quotation, QuotationItem,
    Sale, SaleItem, SalePayment, SaleReturn, SaleReturnItem, TaxRate,
)
from domain import (
    audit, balance_locked, base_quantity, get_product, ledger_post,
    next_document_no, party_balance, require_positive, stock_post,
)
from tax import dec, line_amount, money, quantity, sum_lines


PAYMENT_METHODS = {"cash", "upi", "card", "bank_transfer", "credit"}
PAID_METHODS = PAYMENT_METHODS - {"credit"}
PAYMENT_TYPES = PAYMENT_METHODS | {"split"}


def _rate(db, product, when):
    record = db.execute(select(TaxRate).where(
        TaxRate.product_id == product.id,
        TaxRate.effective_from <= when,
        (TaxRate.effective_to.is_(None)) | (TaxRate.effective_to >= when),
    ).order_by(TaxRate.effective_from.desc()).limit(1)).scalar_one_or_none()
    if record is None and product.hsn_code:
        record = db.execute(select(TaxRate).where(
            TaxRate.product_id.is_(None), TaxRate.hsn_code == product.hsn_code,
            TaxRate.effective_from <= when,
            (TaxRate.effective_to.is_(None)) | (TaxRate.effective_to >= when),
        ).order_by(TaxRate.effective_from.desc()).limit(1)).scalar_one_or_none()
    return dec(record.rate if record else product.gst_rate)


def _sale_items(db, branch_id, payload, settings, interstate, can_override, when):
    raw_items = payload.get("items") or []
    if not raw_items:
        raise ValueError("Add at least one product")
    prepared = []
    for raw in raw_items:
        product = get_product(db, branch_id, raw.get("product_id"))
        unit, factor, base_qty = base_quantity(db, product, raw.get("quantity"), raw.get("unit") or raw.get("unit_id"))
        supplied_price = raw.get("unit_price")
        default_price = money(dec(product.selling_price) * factor)
        price = dec(default_price if supplied_price in (None, "") else supplied_price)
        if price < 0:
            raise ValueError("Selling price cannot be negative")
        if price != default_price and not can_override:
            raise ValueError("Price override requires manager permission")
        discount_pct = dec(raw.get("discount_percent", 0))
        rate = _rate(db, product, when) if settings.is_gst_registered else Decimal("0")
        if settings.is_gst_registered and rate > 0 and not product.hsn_code:
            raise ValueError(f"HSN code is required for taxable product {product.name}")
        values = line_amount(raw.get("quantity"), price, discount_pct, rate, settings.tax_inclusive, interstate)
        current_balance = db.execute(select(InventoryBalance).where(
            InventoryBalance.branch_id == branch_id,
            InventoryBalance.product_id == product.id,
        )).scalar_one_or_none()
        current_cost = dec(current_balance.weighted_avg_cost) if current_balance else dec(product.purchase_price)
        if not can_override and values["taxable"] < money(base_qty * current_cost):
            raise ValueError(f"Below-cost sale of {product.name} requires manager permission")
        prepared.append((product, unit, factor, base_qty, price, rate, values))
    return prepared


def _place(settings, customer, payload):
    explicit = payload.get("place_of_supply_state") or payload.get("place_of_supply_code")
    place = str(explicit or settings.state_code or "").zfill(2) if (explicit or settings.state_code) else ""
    if not place or not settings.state_code:
        raise ValueError("Set store state code and place of supply before billing")
    if len(place) != 2 or not place.isdigit():
        raise ValueError("Place of supply must be a two-digit state code")
    if customer and customer.state_code and customer.state_code != settings.state_code and not explicit:
        raise ValueError("Explicit place of supply is required for out-of-state customers")
    return place


def preview_sale(db, branch_id, payload, settings, can_override=False):
    customer_id = payload.get("customer_id")
    customer = db.get(Customer, int(customer_id)) if customer_id else None
    if customer_id and (not customer or customer.branch_id != branch_id):
        raise ValueError("Customer not found")
    place = _place(settings, customer, payload)
    prepared = _sale_items(db, branch_id, payload, settings,
                           place != settings.state_code, can_override, date.today())
    totals = sum_lines([entry[6] for entry in prepared])
    return {
        "subtotal": totals["gross"], "discount_total": totals["discount"],
        "taxable_total": totals["taxable"], "cgst_total": totals["cgst"],
        "sgst_total": totals["sgst"], "igst_total": totals["igst"],
        "gst_total": totals["gst"], "grand_total": totals["total"],
        "place_of_supply_code": place,
    }


def checkout(db, branch_id, user_id, payload, settings, can_override=False):
    key = payload.get("idempotency_key")
    if key:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                       {"key": f"sale:{key}"})
        existing = db.execute(select(Sale).where(Sale.idempotency_key == key)).scalar_one_or_none()
        if existing:
            if existing.branch_id != branch_id:
                raise ValueError("Idempotency key belongs to a different branch")
            return existing
    customer_id = payload.get("customer_id")
    customer = db.execute(select(Customer).where(Customer.id == int(customer_id)).with_for_update()).scalar_one_or_none() if customer_id else None
    if customer_id and (not customer or customer.branch_id != branch_id or not customer.active):
        raise ValueError("Customer not found")
    quote = None
    if payload.get("quotation_id"):
        quote = db.execute(select(Quotation).where(
            Quotation.id == int(payload["quotation_id"]), Quotation.branch_id == branch_id
        ).with_for_update()).scalar_one_or_none()
        if quote is None:
            raise ValueError("Quotation not found")
        if quote.converted_sale_id:
            return db.get(Sale, quote.converted_sale_id)
        if quote.status != "approved":
            raise ValueError("Approve the quotation before conversion")
        payload = dict(payload, items=[{
            "product_id": item.product_id,
            "quantity": str(item.quantity),
            "unit_id": item.unit_id,
            "unit_price": str(item.unit_price),
            "discount_percent": str(item.discount_percent),
        } for item in quote.items], customer_id=quote.customer_id)
        customer = db.get(Customer, quote.customer_id) if quote.customer_id else None
    invoice_type = payload.get("invoice_type") or ("tax_invoice" if settings.is_gst_registered else "retail")
    if invoice_type not in ("tax_invoice", "bill_of_supply", "retail"):
        raise ValueError("Invalid invoice type")
    if invoice_type == "tax_invoice" and (not settings.is_gst_registered or not settings.gstin):
        raise ValueError("Configure the store GSTIN before issuing a GST tax invoice")
    if invoice_type == "bill_of_supply" and not settings.is_gst_registered:
        raise ValueError("Bill of Supply requires a registered store")
    if settings.e_invoice_applicable and customer and customer.gstin and invoice_type == "tax_invoice":
        raise ValueError("E-invoicing is enabled; connect an IRP before posting B2B tax invoices")
    place = _place(settings, customer, payload)
    interstate = place != settings.state_code
    today = date.today()
    prepared = _sale_items(db, branch_id, payload, settings, interstate, can_override, today)
    totals = sum_lines([entry[6] for entry in prepared])
    if invoice_type == "bill_of_supply" and totals["gst"] != 0:
        raise ValueError("Bill of Supply lines must have zero GST")
    if invoice_type == "retail" and totals["gst"] != 0:
        raise ValueError("Taxable GST sales must use a tax invoice")
    if not customer or not customer.gstin:
        if totals["taxable"] >= Decimal("50000"):
            name = payload.get("customer_name") or (customer.name if customer else "")
            address = payload.get("customer_address") or (customer.address if customer else "")
            delivery = payload.get("delivery_address") or address
            if not name or not address or not delivery:
                raise ValueError("Unregistered recipient name, address and delivery address required for taxable supply of ₹50,000 or more")
    payment_type = str(payload.get("payment_type") or "").strip().lower()
    if not payment_type:
        raise ValueError("Select a payment type before generating the invoice")
    if payment_type not in PAYMENT_TYPES:
        raise ValueError("Invalid payment type")

    raw_payments = payload.get("payments") or []
    if not isinstance(raw_payments, list):
        raise ValueError("Payments must be a list")
    paid = Decimal("0")
    normalized_payments = []
    for row in raw_payments:
        if not isinstance(row, dict):
            raise ValueError("Invalid payment entry")
        method = str(row.get("method") or "").strip().lower()
        if method not in PAID_METHODS:
            raise ValueError("Select a valid method for each received payment")
        amount = money(require_positive(row.get("amount"), "Payment amount"))
        normalized_payments.append((method, amount, row.get("reference") or ""))
        paid += amount
    if payment_type == "credit":
        if normalized_payments:
            raise ValueError("Customer credit cannot include an immediate payment")
        if not customer:
            raise ValueError("Select a registered customer for a credit sale")
    elif payment_type == "split":
        if len(normalized_payments) < 2 or len({method for method, _, _ in normalized_payments}) < 2:
            raise ValueError("Split payment requires at least two different payment methods")
    elif not (totals["total"] == 0 and not normalized_payments):
        if len(normalized_payments) != 1 or normalized_payments[0][0] != payment_type:
            raise ValueError("Received payment must match the selected payment type")
    if paid > totals["total"]:
        raise ValueError("Payments cannot exceed the invoice total")
    due = totals["total"] - paid
    if due > 0 and not customer:
        raise ValueError("Select a registered customer for a credit sale")
    if customer and due > 0:
        current = party_balance(db, "customer", customer)
        if dec(customer.credit_limit) > 0 and current + due > dec(customer.credit_limit):
            raise ValueError("Customer credit limit exceeded")
    number, fy = next_document_no(db, branch_id, settings, "sale", today)
    sale = Sale(
        branch_id=branch_id, invoice_no=number, invoice_type=invoice_type,
        payment_type=payment_type,
        financial_year=fy, customer_id=customer.id if customer else None,
        customer_name=payload.get("customer_name") or (customer.name if customer else "Walk-in Customer"),
        customer_address=payload.get("customer_address") or (customer.address if customer else ""),
        delivery_address=payload.get("delivery_address") or payload.get("customer_address") or (customer.address if customer else ""),
        customer_gstin=customer.gstin if customer else "",
        customer_state_code=customer.state_code if customer else "",
        place_of_supply_code=place, status="confirmed",
        subtotal=totals["gross"], discount_total=totals["discount"],
        taxable_total=totals["taxable"], cgst_total=totals["cgst"],
        sgst_total=totals["sgst"], igst_total=totals["igst"],
        grand_total=totals["total"], paid_total=paid,
        cost_of_goods=Decimal("0"), quotation_id=quote.id if quote else None,
        idempotency_key=key, created_by=user_id,
    )
    db.add(sale)
    db.flush()
    total_cost = Decimal("0")
    for product, unit, factor, base_qty, price, rate, values in prepared:
        balance = balance_locked(db, branch_id, product.id)
        if dec(balance.quantity) < base_qty and not settings.allow_negative_stock:
            raise ValueError(f"Insufficient stock for {product.name}")
        unit_cost = dec(balance.weighted_avg_cost)
        cogs = (base_qty * unit_cost).quantize(Decimal("0.000001"))
        db.add(SaleItem(
            sale_id=sale.id, product_id=product.id, description=product.name,
            hsn_code=product.hsn_code, quantity=quantity(base_qty / factor),
            unit_id=unit.id, conversion_factor=factor, base_quantity=base_qty,
            unit_price=price, discount=values["discount"], taxable_value=values["taxable"],
            gst_rate=rate, cgst=values["cgst"], sgst=values["sgst"], igst=values["igst"],
            line_total=values["total"], unit_cost=unit_cost, cost_of_goods=cogs,
        ))
        stock_post(db, branch_id, product.id, -base_qty, unit_cost, "sale", "sale", sale.id,
                   user_id, allow_negative=settings.allow_negative_stock)
        total_cost += cogs
    sale.cost_of_goods = total_cost
    for method, amount, reference in normalized_payments:
        db.add(SalePayment(sale_id=sale.id, customer_id=sale.customer_id,
                           amount=amount, method=method, reference=reference, created_by=user_id))
    if customer:
        ledger_post(db, branch_id, "customer", customer.id, sale.grand_total, "sale", "sale", sale.id,
                    user_id, due_date=today + timedelta(days=max(0, customer.credit_days)))
        if paid:
            ledger_post(db, branch_id, "customer", customer.id, -paid, "payment", "sale", sale.id, user_id)
    if quote:
        quote.status = "converted"
        quote.converted_sale_id = sale.id
    audit(db, branch_id, user_id, "sale.confirm", "sale", sale.id,
          after={"invoice_no": sale.invoice_no, "total": str(sale.grand_total)})
    db.flush()
    return sale


def create_quotation(db, branch_id, user_id, payload, settings):
    customer_id = payload.get("customer_id")
    customer = db.get(Customer, int(customer_id)) if customer_id else None
    if customer_id and (not customer or customer.branch_id != branch_id):
        raise ValueError("Customer not found")
    place = _place(settings, customer, payload)
    prepared = _sale_items(db, branch_id, payload, settings, place != settings.state_code, True, date.today())
    totals = sum_lines([entry[6] for entry in prepared])
    number, _ = next_document_no(db, branch_id, settings, "quotation")
    valid_until = payload.get("valid_until")
    quote = Quotation(branch_id=branch_id, number=number, customer_id=customer_id,
                      customer_name=payload.get("customer_name") or (customer.name if customer else ""),
                      quote_date=date.today(), valid_until=date.fromisoformat(valid_until) if valid_until else None,
                      subtotal=totals["gross"], discount_total=totals["discount"],
                      tax_total=totals["gst"], grand_total=totals["total"],
                      terms=payload.get("terms") or settings.terms,
                      notes=payload.get("notes") or "", created_by=user_id)
    db.add(quote)
    db.flush()
    for raw, (product, unit, factor, base_qty, price, rate, values) in zip(payload["items"], prepared):
        db.add(QuotationItem(
            quotation_id=quote.id, product_id=product.id, quantity=quantity(base_qty / factor),
            unit_id=unit.id, conversion_factor=factor, unit_price=price,
            discount_percent=dec(raw.get("discount_percent", 0)),
            discount=values["discount"], gst_rate=rate, taxable_value=values["taxable"],
            tax_total=values["gst"], line_total=values["total"],
        ))
    audit(db, branch_id, user_id, "quotation.create", "quotation", quote.id)
    db.flush()
    return quote


def replace_quotation_items(db, quote, payload, settings, user_id):
    """Revise an unconverted quotation without affecting stock or its number."""
    if quote.converted_sale_id:
        raise ValueError("Converted quotation cannot be revised")
    customer_id = payload.get("customer_id", quote.customer_id)
    customer = db.get(Customer, int(customer_id)) if customer_id else None
    if customer_id and (not customer or customer.branch_id != quote.branch_id):
        raise ValueError("Customer not found")
    place = _place(settings, customer, payload)
    prepared = _sale_items(db, quote.branch_id, payload, settings,
                           place != settings.state_code, True, date.today())
    totals = sum_lines([entry[6] for entry in prepared])
    quote.customer_id = customer_id
    quote.customer_name = payload.get("customer_name") or (customer.name if customer else "")
    quote.subtotal = totals["gross"]
    quote.discount_total = totals["discount"]
    quote.tax_total = totals["gst"]
    quote.grand_total = totals["total"]
    quote.items.clear()
    db.flush()
    for raw, (product, unit, factor, base_qty, price, rate, values) in zip(payload["items"], prepared):
        quote.items.append(QuotationItem(product_id=product.id,
            quantity=quantity(base_qty / factor), unit_id=unit.id,
            conversion_factor=factor, unit_price=price,
            discount_percent=dec(raw.get("discount_percent", 0)), discount=values["discount"],
            gst_rate=rate, taxable_value=values["taxable"], tax_total=values["gst"],
            line_total=values["total"]))
    quote.status = "draft"
    audit(db, quote.branch_id, user_id, "quotation.revise", "quotation", quote.id,
          after={"grand_total": str(quote.grand_total)})
    db.flush()
    return quote


def return_sale(db, branch_id, sale_id, user_id, payload, settings):
    sale = db.execute(select(Sale).where(Sale.id == sale_id, Sale.branch_id == branch_id).with_for_update()).scalar_one_or_none()
    if not sale or sale.status != "confirmed":
        raise ValueError("Confirmed sale not found")
    items = payload.get("items") or []
    if not items:
        raise ValueError("Select at least one return item")
    number, _ = next_document_no(db, branch_id, settings, "sale_return")
    refund_method = str(payload.get("refund_method") or "cash").lower()
    if refund_method not in PAYMENT_METHODS:
        raise ValueError("Invalid refund method")
    if refund_method == "credit" and not sale.customer_id:
        raise ValueError("Store credit requires a registered customer")
    ret = SaleReturn(sale_id=sale.id, number=number, reason=payload.get("reason") or "",
                     refund_method=refund_method, created_by=user_id)
    db.add(ret)
    db.flush()
    taxable = cgst = sgst = igst = total = Decimal("0")
    seen_items = set()
    for raw in items:
        sale_item = db.get(SaleItem, int(raw.get("sale_item_id")))
        if not sale_item or sale_item.sale_id != sale.id:
            raise ValueError("Sale item not found")
        if sale_item.id in seen_items:
            raise ValueError("Duplicate sale item in return")
        seen_items.add(sale_item.id)
        base_qty = quantity(require_positive(raw.get("quantity"), "Return quantity") * dec(sale_item.conversion_factor))
        returned = db.execute(select(func.coalesce(func.sum(SaleReturnItem.base_quantity), 0)).where(
            SaleReturnItem.sale_item_id == sale_item.id
        )).scalar_one()
        remaining = dec(sale_item.base_quantity) - dec(returned)
        if base_qty > remaining:
            raise ValueError("Return quantity exceeds sold quantity")
        fraction = base_qty / dec(sale_item.base_quantity)
        # Final return takes the full remaining monetary amount to avoid paise drift.
        prior_rows = db.execute(select(SaleReturnItem).where(SaleReturnItem.sale_item_id == sale_item.id)).scalars().all()
        def prorate(field):
            full = dec(getattr(sale_item, field))
            prior = sum((dec(getattr(row, field)) for row in prior_rows), Decimal("0"))
            return full - prior if base_qty == remaining else money(full * fraction)
        row_taxable, row_cgst, row_sgst, row_igst, row_total = map(
            prorate, ("taxable_value", "cgst", "sgst", "igst", "line_total")
        )
        db.add(SaleReturnItem(
            return_id=ret.id, sale_item_id=sale_item.id, product_id=sale_item.product_id,
            base_quantity=base_qty, unit_cost=sale_item.unit_cost,
            taxable_value=row_taxable, cgst=row_cgst, sgst=row_sgst,
            igst=row_igst, line_total=row_total,
        ))
        stock_post(db, branch_id, sale_item.product_id, base_qty, sale_item.unit_cost,
                   "sale_return", "sale_return", ret.id, user_id)
        taxable += row_taxable; cgst += row_cgst; sgst += row_sgst; igst += row_igst; total += row_total
    ret.taxable_total, ret.cgst_total, ret.sgst_total, ret.igst_total, ret.grand_total = taxable, cgst, sgst, igst, total
    refund = Decimal("0")
    if sale.customer_id:
        customer = db.execute(select(Customer).where(Customer.id == sale.customer_id).with_for_update()).scalar_one()
        prior_balance = party_balance(db, "customer", customer)
        ledger_post(db, branch_id, "customer", customer.id, -total,
                    "sale_return", "sale_return", ret.id, user_id)
        refund = min(total, max(Decimal("0"), total - max(Decimal("0"), prior_balance)))
        if refund > 0 and refund_method != "credit":
            ledger_post(db, branch_id, "customer", customer.id, refund,
                        "refund", "sale_return", ret.id, user_id)
    else:
        refund = total
    ret.refunded_amount = money(refund if refund_method != "credit" else 0)
    audit(db, branch_id, user_id, "sale.return", "sale_return", ret.id,
          after={"sale_id": sale.id, "total": str(total)})
    db.flush()
    return ret
