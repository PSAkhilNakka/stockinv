"""Purchase posting, supplier payments and debit notes.

The caller owns the database transaction and must commit only after these
functions return. A failure leaves the complete transaction for rollback.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func

from domain import (
    audit, base_quantity, balance_locked, get_product, ledger_post,
    next_document_no, party_balance, stock_post,
)

from models import (
    PartyPayment,
    PaymentAllocation,
    Purchase,
    PurchaseItem,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchasePayment,
    PurchaseReturn,
    PurchaseReturnItem,
    Supplier,
)
from tax import dec, line_amount, money, quantity, sum_lines


ZERO = Decimal("0")
ONE = Decimal("1")
SIX = Decimal("0.000001")


def _date(value, default=None):
    if not value:
        return default or date.today()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _supplier(db, branch_id, supplier_id):
    supplier = db.query(Supplier).filter_by(id=supplier_id, branch_id=branch_id, active=True).one_or_none()
    if supplier is None:
        raise ValueError("Active supplier not found in this branch")
    return supplier


def _tax_location(supplier, settings, payload, gst_rate):
    # The purchase bill's place of supply is explicit when present. Otherwise
    # use the receiving branch. Supplier's state is needed to classify IGST.
    receiving_state = str(payload.get("place_of_supply_code") or settings.state_code or "").strip()
    supplier_state = str(supplier.state_code or "").strip()
    if dec(gst_rate) > 0 and settings.is_gst_registered and (not receiving_state or not supplier_state):
        raise ValueError("Store and supplier state codes are required for GST purchase entries")
    return receiving_state, bool(receiving_state and supplier_state and receiving_state != supplier_state)


def _audit(db, branch_id, user_id, action, entity_type, entity_id, details=None):
    audit(db, branch_id, user_id, action, entity_type, entity_id, after=details or {})


def create_purchase(db, branch_id, user_id, payload, settings):
    """Save a draft purchase; inventory and payable remain unchanged."""
    supplier = _supplier(db, branch_id, int(payload.get("supplier_id") or 0))
    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Purchase requires at least one product")
    day = _date(payload.get("purchase_date"))
    purchase = Purchase(
        branch_id=branch_id, supplier_id=supplier.id,
        purchase_order_id=payload.get("purchase_order_id") or None,
        supplier_invoice_no=str(payload.get("supplier_invoice_no") or payload.get("invoice_no") or "").strip(),
        purchase_date=day, status="draft", notes=str(payload.get("notes") or ""),
        created_by=user_id,
    )
    db.add(purchase)
    db.flush()
    calculated = []
    for raw in raw_items:
        product = get_product(db, branch_id, int(raw.get("product_id") or 0))
        selected, factor, base_qty = base_quantity(db, product, raw.get("quantity"), raw.get("unit_id"))
        unit_id = selected.id
        qty = quantity(raw.get("quantity"))
        rate = dec(raw.get("unit_price", raw.get("purchase_price", dec(product.purchase_price) * factor)))
        gst_rate = dec(raw.get("gst_rate", product.gst_rate))
        if settings.is_gst_registered and gst_rate > 0 and not product.hsn_code:
            raise ValueError(f"HSN code is required for taxable product {product.name}")
        place_code, interstate = _tax_location(supplier, settings, payload, gst_rate)
        purchase.place_of_supply_code = place_code
        calc = line_amount(
            qty, rate, raw.get("discount_percent", 0), gst_rate,
            bool(payload.get("tax_inclusive", settings.tax_inclusive)), interstate,
        )
        calculated.append(calc)
        purchase.items.append(PurchaseItem(
            product_id=product.id, quantity=qty, unit_id=unit_id,
            conversion_factor=factor, base_quantity=base_qty, unit_price=rate,
            discount=calc["discount"], taxable_value=calc["taxable"],
            gst_rate=gst_rate, cgst=calc["cgst"], sgst=calc["sgst"],
            igst=calc["igst"], line_total=calc["total"], hsn_code=product.hsn_code,
        ))
    totals = sum_lines(calculated)
    purchase.subtotal = totals["gross"]
    purchase.discount_total = totals["discount"]
    purchase.taxable_total = totals["taxable"]
    purchase.cgst_total = totals["cgst"]
    purchase.sgst_total = totals["sgst"]
    purchase.igst_total = totals["igst"]
    purchase.grand_total = totals["total"]
    db.flush()
    _audit(db, branch_id, user_id, "purchase.create", "purchase", purchase.id,
           {"supplier_id": supplier.id, "grand_total": str(purchase.grand_total)})
    return purchase


def _post_receipt(db, purchase, item, user_id):
    added_qty = dec(item.base_quantity)
    # Recoverable input GST is not part of inventory cost. The frozen pre-tax
    # line value, after discount, is the purchase cost snapshot.
    added_value = dec(item.taxable_value)
    stock_post(
        db, purchase.branch_id, item.product_id, added_qty,
        (added_value / added_qty).quantize(SIX), "purchase", "purchase_item",
        item.id, user_id,
    )


def _record_purchase_payment(db, purchase, user_id, raw):
    amount = money(raw.get("amount"))
    if amount <= 0:
        raise ValueError("Payment amount must be positive")
    if amount > _purchase_remaining(db, purchase):
        raise ValueError("Payment exceeds the remaining purchase amount")
    method = str(raw.get("method") or "cash").strip().lower()
    payment = PurchasePayment(
        purchase_id=purchase.id, supplier_id=purchase.supplier_id,
        amount=amount, method=method, reference=str(raw.get("reference") or ""),
        created_by=user_id,
    )
    db.add(payment)
    db.flush()
    purchase.paid_total = money(dec(purchase.paid_total) + amount)
    ledger_post(db, purchase.branch_id, "supplier", purchase.supplier_id, -amount,
                "purchase_payment", "purchase_payment", payment.id, user_id,
                f"Payment against purchase {purchase.id}")
    return payment


def _purchase_remaining(db, purchase):
    returned = db.query(func.coalesce(func.sum(PurchaseReturn.grand_total), 0)).filter(
        PurchaseReturn.purchase_id == purchase.id,
        PurchaseReturn.status == "confirmed",
    ).scalar()
    return max(ZERO, money(dec(purchase.grand_total) - dec(purchase.paid_total) - dec(returned)))


def confirm_purchase(db, purchase, user_id, payments=None):
    """Post draft once: stock, supplier payable, optional payments."""
    purchase = (
        db.query(Purchase).filter_by(id=purchase.id).with_for_update().one()
    )
    if purchase.status == "confirmed":
        return purchase
    if purchase.status != "draft":
        raise ValueError("Only draft purchases can be confirmed")
    if not purchase.items:
        raise ValueError("Cannot confirm an empty purchase")
    # Lock balances consistently to reduce cross-document deadlocks.
    for item in sorted(purchase.items, key=lambda row: row.product_id):
        _post_receipt(db, purchase, item, user_id)
    purchase.status = "confirmed"
    purchase.confirmed_at = datetime.now(timezone.utc)
    purchase.confirmed_by = user_id
    ledger_post(db, purchase.branch_id, "supplier", purchase.supplier_id,
                purchase.grand_total, "purchase", "purchase", purchase.id,
                user_id, f"Supplier invoice {purchase.supplier_invoice_no or purchase.id}",
                due_date=purchase.purchase_date + timedelta(days=max(0, purchase.supplier.credit_days)))
    if isinstance(payments, dict):
        payments = [payments]
    for payment in payments or []:
        _record_purchase_payment(db, purchase, user_id, payment)
    _audit(db, purchase.branch_id, user_id, "purchase.confirm", "purchase", purchase.id,
           {"stock_items": len(purchase.items), "total": str(purchase.grand_total)})
    db.flush()
    return purchase


def purchase_payment(db, purchase_id, user_id, payload):
    """Pay a confirmed purchase in part or in full."""
    purchase = db.query(Purchase).filter_by(id=purchase_id).with_for_update().one_or_none()
    if purchase is None or purchase.status != "confirmed":
        raise ValueError("Confirmed purchase not found")
    payment = _record_purchase_payment(db, purchase, user_id, payload)
    _audit(db, purchase.branch_id, user_id, "purchase.payment", "purchase_payment",
           payment.id, {"purchase_id": purchase.id, "amount": str(payment.amount)})
    db.flush()
    return payment


def create_purchase_order(db, branch_id, user_id, payload, settings):
    """Create an order without posting stock or supplier payable."""
    supplier = _supplier(db, branch_id, int(payload.get("supplier_id") or 0))
    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Purchase order requires at least one product")
    day = _date(payload.get("order_date"))
    number, _ = next_document_no(db, branch_id, settings, "purchase_order", day)
    order = PurchaseOrder(
        branch_id=branch_id, supplier_id=supplier.id, number=number,
        status="draft", order_date=day,
        expected_date=_date(payload.get("expected_date")) if payload.get("expected_date") else None,
        notes=str(payload.get("notes") or ""), created_by=user_id,
    )
    db.add(order)
    db.flush()
    subtotal = tax_total = grand_total = ZERO
    for raw in raw_items:
        product = get_product(db, branch_id, int(raw.get("product_id") or 0))
        selected, factor, base_qty = base_quantity(db, product, raw.get("quantity"), raw.get("unit_id"))
        unit_id = selected.id
        qty = quantity(raw.get("quantity"))
        rate = dec(raw.get("unit_price", dec(product.purchase_price) * factor))
        gst_rate = dec(raw.get("gst_rate", product.gst_rate))
        if settings.is_gst_registered and gst_rate > 0 and not product.hsn_code:
            raise ValueError(f"HSN code is required for taxable product {product.name}")
        _, interstate = _tax_location(supplier, settings, payload, gst_rate)
        calc = line_amount(qty, rate, raw.get("discount_percent", 0), gst_rate,
                           bool(payload.get("tax_inclusive", settings.tax_inclusive)), interstate)
        order.items.append(PurchaseOrderItem(
            product_id=product.id, quantity=qty, unit_id=unit_id,
            base_quantity=base_qty, unit_price=rate,
            discount_percent=dec(raw.get("discount_percent", 0)), gst_rate=gst_rate,
            line_total=calc["total"],
        ))
        subtotal += calc["taxable"]
        tax_total += calc["gst"]
        grand_total += calc["total"]
    order.subtotal = money(subtotal)
    order.tax_total = money(tax_total)
    order.grand_total = money(grand_total)
    db.flush()
    _audit(db, branch_id, user_id, "purchase_order.create", "purchase_order", order.id,
           {"number": number})
    return order


def purchase_return(db, purchase_id, user_id, payload, settings):
    """Post a supplier return/debit note and reverse stock and payable."""
    purchase = db.query(Purchase).filter_by(id=purchase_id).with_for_update().one_or_none()
    if purchase is None or purchase.status != "confirmed":
        raise ValueError("Confirmed purchase not found")
    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("Select at least one purchase line to return")
    day = _date(payload.get("return_date"))
    number, _ = next_document_no(db, purchase.branch_id, settings, "purchase_return", day)
    result = PurchaseReturn(
        purchase_id=purchase.id, supplier_id=purchase.supplier_id,
        number=number, status="confirmed", return_date=day,
        reason=str(payload.get("reason") or ""), created_by=user_id,
    )
    db.add(result)
    db.flush()
    taxable_total = tax_total = grand_total = ZERO
    seen = set()
    for raw in raw_items:
        item_id = int(raw.get("purchase_item_id") or 0)
        if item_id in seen:
            raise ValueError("Purchase line appears more than once in return")
        seen.add(item_id)
        item = db.query(PurchaseItem).filter_by(id=item_id, purchase_id=purchase.id).one_or_none()
        if item is None:
            raise ValueError("Purchase line not found on this invoice")
        returned = db.query(func.coalesce(func.sum(PurchaseReturnItem.base_quantity), 0)).join(
            PurchaseReturn, PurchaseReturn.id == PurchaseReturnItem.return_id
        ).filter(PurchaseReturnItem.purchase_item_id == item.id,
                 PurchaseReturn.status == "confirmed").scalar()
        qty = quantity(raw.get("quantity"))
        base_qty = quantity(qty * dec(item.conversion_factor))
        if base_qty <= 0 or base_qty > quantity(dec(item.base_quantity) - dec(returned)):
            raise ValueError("Return quantity exceeds unreturned purchased quantity")
        fraction = base_qty / dec(item.base_quantity)
        if base_qty == quantity(dec(item.base_quantity) - dec(returned)):
            prior_taxable, prior_cgst, prior_sgst, prior_igst = db.query(
                func.coalesce(func.sum(PurchaseReturnItem.taxable_value), 0),
                func.coalesce(func.sum(PurchaseReturnItem.cgst), 0),
                func.coalesce(func.sum(PurchaseReturnItem.sgst), 0),
                func.coalesce(func.sum(PurchaseReturnItem.igst), 0),
            ).join(PurchaseReturn, PurchaseReturn.id == PurchaseReturnItem.return_id).filter(
                PurchaseReturnItem.purchase_item_id == item.id,
                PurchaseReturn.status == "confirmed",
            ).one()
            taxable = money(dec(item.taxable_value) - dec(prior_taxable))
            cgst = money(dec(item.cgst) - dec(prior_cgst))
            sgst = money(dec(item.sgst) - dec(prior_sgst))
            igst = money(dec(item.igst) - dec(prior_igst))
        else:
            taxable = money(dec(item.taxable_value) * fraction)
            cgst = money(dec(item.cgst) * fraction)
            sgst = money(dec(item.sgst) * fraction)
            igst = money(dec(item.igst) * fraction)
        tax = cgst + sgst + igst
        total = taxable + tax
        balance = balance_locked(db, purchase.branch_id, item.product_id)
        old_qty = dec(balance.quantity)
        if not settings.allow_negative_stock and old_qty < base_qty:
            raise ValueError("Insufficient current stock for supplier return")
        avg_cost = dec(balance.weighted_avg_cost)
        # Under weighted average, removals are valued at the current average.
        # Supplier credit remains tied to the frozen original purchase line.
        return_item = PurchaseReturnItem(
            purchase_item_id=item.id, product_id=item.product_id,
            base_quantity=base_qty, unit_cost=avg_cost,
            taxable_value=taxable, cgst=cgst, sgst=sgst, igst=igst,
            tax_total=tax, line_total=total,
        )
        result.items.append(return_item)
        db.flush()
        stock_post(db, purchase.branch_id, item.product_id, -base_qty, avg_cost,
                   "purchase_return", "purchase_return_item", return_item.id,
                   user_id, allow_negative=settings.allow_negative_stock)
        taxable_total += taxable
        tax_total += tax
        grand_total += total
    result.taxable_total = money(taxable_total)
    result.tax_total = money(tax_total)
    result.grand_total = money(grand_total)
    ledger_post(db, purchase.branch_id, "supplier", purchase.supplier_id,
                -result.grand_total, "purchase_return", "purchase_return",
                result.id, user_id, f"Debit note {number} against purchase {purchase.id}")
    _audit(db, purchase.branch_id, user_id, "purchase.return", "purchase_return", result.id,
           {"number": number, "total": str(result.grand_total)})
    db.flush()
    return result


def supplier_payment(db, supplier_id, branch_id, user_id, payload):
    """Record a supplier payment and allocate it to oldest open purchases."""
    supplier = db.query(Supplier).filter_by(id=supplier_id, branch_id=branch_id, active=True).with_for_update().one_or_none()
    if supplier is None:
        raise ValueError("Active supplier not found in this branch")
    amount = money(payload.get("amount"))
    if amount <= 0:
        raise ValueError("Payment amount must be positive")
    db.flush()
    if amount > party_balance(db, "supplier", supplier):
        raise ValueError("Payment exceeds supplier outstanding balance")
    specified_purchase_id = payload.get("purchase_id")
    purchases = db.query(Purchase).filter_by(
        branch_id=branch_id, supplier_id=supplier.id, status="confirmed"
    )
    if specified_purchase_id:
        purchases = purchases.filter(Purchase.id == int(specified_purchase_id))
    purchases = purchases.order_by(Purchase.purchase_date, Purchase.id).with_for_update().all()
    if specified_purchase_id and not purchases:
        raise ValueError("Confirmed purchase not found for supplier")
    if specified_purchase_id and amount > _purchase_remaining(db, purchases[0]):
        raise ValueError("Payment exceeds selected purchase balance")
    payment = PartyPayment(
        branch_id=branch_id, party_type="supplier", supplier_id=supplier.id,
        amount=amount, method=str(payload.get("method") or "bank_transfer").strip().lower(),
        reference=str(payload.get("reference") or ""),
        note=str(payload.get("note") or ""), created_by=user_id,
    )
    db.add(payment)
    db.flush()
    unallocated = amount
    for purchase in purchases:
        remaining = _purchase_remaining(db, purchase)
        applied = min(unallocated, remaining)
        if applied <= 0:
            continue
        db.add(PaymentAllocation(
            party_payment_id=payment.id, purchase_id=purchase.id, amount=applied,
        ))
        purchase.paid_total = money(dec(purchase.paid_total) + applied)
        unallocated = money(unallocated - applied)
        if unallocated == 0:
            break
    # A residual payment can settle a recorded opening balance or represent a
    # supplier credit, while the immutable ledger remains the balance authority.
    ledger_post(db, branch_id, "supplier", supplier.id, -amount,
                "supplier_payment", "party_payment", payment.id,
                user_id, payment.note or f"Supplier payment {payment.id}")
    _audit(db, branch_id, user_id, "supplier.payment", "party_payment", payment.id,
           {"supplier_id": supplier.id, "amount": str(amount),
            "unallocated": str(unallocated)})
    db.flush()
    return payment
