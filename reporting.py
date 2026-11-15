"""Read-only business summaries and downloadable reports.

Reports use posted document snapshots rather than mutable product prices.
All monetary values are Decimal internally and strings at the JSON boundary.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from io import BytesIO
import os
import re

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import object_session, selectinload

from models import (
    Customer, Expense, InventoryBalance, LedgerEntry, PartyPayment, Product,
    Purchase, PurchaseReturn, Sale, SaleReturn, StockMovement, Supplier, Unit,
)
from tax import dec, money


ZERO = Decimal("0")
IST = timezone(timedelta(hours=5, minutes=30))
NUMERIC_HEADERS = {
    "Taxable", "GST", "Total", "Paid", "COGS", "Gross Profit", "Revenue Ex GST",
    "Expenses", "Net Profit", "Return Cost Variance", "CGST", "SGST", "IGST", "Total GST", "Amount",
    "Total Tax", "Document Count",
    "Stock", "Minimum", "Average Cost", "Cost Value", "Selling Price",
    "Selling Value", "Opening Balance", "Outstanding", "Overdue",
    "Qty Sold", "Invoice Count", "Purchase Count", "Return Count", "Quantity",
    "Line Sales", "Net Sales", "Net Purchases", "Purchase Rate",
    "Cost per Base Unit", "Change %", "Quantity Delta", "Unit Cost", "Value Delta",
    "Balance After", "90-Day Net Sold", "Turnover Ratio", "Days Since Sale",
}


def _day(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        # Database timestamps are UTC. SQLite can return them without tzinfo.
        return value.replace(tzinfo=timezone.utc).astimezone(IST).date() if value.tzinfo is None else value.astimezone(IST).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _date_bounds(start=None, end=None):
    first, last = _day(start), _day(end)
    if first and last and first > last:
        raise ValueError("Start date must not be after end date")
    return first, last


def _within(value, first=None, last=None):
    day = _day(value)
    return day is not None and (first is None or day >= first) and (last is None or day <= last)


def _number(value):
    return str(money(value))


def _quantity(value):
    rendered = format(dec(value), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _json(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _now_day():
    return datetime.now(IST).date()


def _months_ago(day, count):
    ordinal = day.year * 12 + day.month - 1 - count
    return date(ordinal // 12, ordinal % 12 + 1, 1)


def _money_sum(values):
    return money(sum((dec(value) for value in values), ZERO))


def _load(session, branch_id, include_movements=False):
    """Load posted source documents; draft/held/quoted documents are excluded."""
    sales = (session.query(Sale).options(selectinload(Sale.items), selectinload(Sale.payments))
             .filter(Sale.branch_id == branch_id, Sale.status == "confirmed").all())
    purchases = (session.query(Purchase).options(selectinload(Purchase.items), selectinload(Purchase.payments))
                 .filter(Purchase.branch_id == branch_id, Purchase.status == "confirmed").all())
    sale_returns = (session.query(SaleReturn).options(selectinload(SaleReturn.items))
                    .join(Sale, SaleReturn.sale_id == Sale.id)
                    .filter(Sale.branch_id == branch_id, Sale.status == "confirmed",
                            SaleReturn.status == "confirmed").all())
    purchase_returns = (session.query(PurchaseReturn).options(selectinload(PurchaseReturn.items))
                        .join(Purchase, PurchaseReturn.purchase_id == Purchase.id)
                        .filter(Purchase.branch_id == branch_id, Purchase.status == "confirmed",
                                PurchaseReturn.status == "confirmed").all())
    result = {
        "sales": sales,
        "purchases": purchases,
        "sale_returns": sale_returns,
        "purchase_returns": purchase_returns,
        "expenses": session.query(Expense).filter(Expense.branch_id == branch_id).all(),
        "products": session.query(Product).options(selectinload(Product.category), selectinload(Product.base_unit)).filter(Product.branch_id == branch_id).all(),
        "balances": session.query(InventoryBalance).filter(InventoryBalance.branch_id == branch_id).all(),
        "customers": session.query(Customer).filter(Customer.branch_id == branch_id).all(),
        "suppliers": session.query(Supplier).filter(Supplier.branch_id == branch_id).all(),
        "ledger": session.query(LedgerEntry).filter(LedgerEntry.branch_id == branch_id).all(),
        "party_payments": session.query(PartyPayment).filter(PartyPayment.branch_id == branch_id).all(),
    }
    if include_movements:
        result["movements"] = session.query(StockMovement).filter(StockMovement.branch_id == branch_id).all()
    return result


def _sales_line_events(data, first=None, last=None):
    """Yield signed posted sale-line values, adjusting at return posting dates."""
    sale_by_id = {sale.id: sale for sale in data["sales"]}
    item_by_id = {item.id: item for sale in data["sales"] for item in sale.items}
    for sale in _filtered(data["sales"], "sale_date", first, last):
        for item in sale.items:
            yield {"date": _day(sale.sale_date), "type": "sale", "document_id": sale.id,
                   "product_id": item.product_id, "customer_id": sale.customer_id,
                   "customer_name": sale.customer_name, "quantity": dec(item.base_quantity),
                   "taxable": dec(item.taxable_value),
                   "gst": dec(item.cgst) + dec(item.sgst) + dec(item.igst),
                   "cogs": dec(item.cost_of_goods)}
    for returned in _filtered(data["sale_returns"], "return_date", first, last):
        sale = sale_by_id.get(returned.sale_id)
        for item in returned.items:
            original = item_by_id.get(item.sale_item_id)
            yield {"date": _day(returned.return_date), "type": "return", "document_id": returned.id,
                   "product_id": item.product_id,
                   "customer_id": sale.customer_id if sale else None,
                   "customer_name": sale.customer_name if sale else "",
                   "quantity": -dec(item.base_quantity), "taxable": -dec(item.taxable_value),
                   "gst": -dec(item.cgst) - dec(item.sgst) - dec(item.igst),
                   "cogs": -dec(item.unit_cost) * dec(item.base_quantity)}


def _filtered(items, field, first=None, last=None):
    return [item for item in items if _within(getattr(item, field), first, last)]


def _period_values(data, first, last):
    sales = _filtered(data["sales"], "sale_date", first, last)
    sale_returns = _filtered(data["sale_returns"], "return_date", first, last)
    purchases = _filtered(data["purchases"], "purchase_date", first, last)
    purchase_returns = _filtered(data["purchase_returns"], "return_date", first, last)
    expenses = _filtered(data["expenses"], "expense_date", first, last)
    revenue = _money_sum([s.taxable_total for s in sales] + [-dec(r.taxable_total) for r in sale_returns])
    cogs = _money_sum([s.cost_of_goods for s in sales] +
                      [-dec(item.unit_cost) * dec(item.base_quantity) for r in sale_returns for item in r.items])
    sales_total = _money_sum([s.grand_total for s in sales] + [-dec(r.grand_total) for r in sale_returns])
    purchases_total = _money_sum([p.grand_total for p in purchases] + [-dec(r.grand_total) for r in purchase_returns])
    gst_collected = _money_sum([dec(s.cgst_total) + dec(s.sgst_total) + dec(s.igst_total) for s in sales] +
                               [-dec(r.cgst_total) - dec(r.sgst_total) - dec(r.igst_total) for r in sale_returns])
    gst_paid = _money_sum([dec(p.cgst_total) + dec(p.sgst_total) + dec(p.igst_total) for p in purchases] +
                          [-dec(r.tax_total) for r in purchase_returns])
    expense_total = _money_sum(e.amount for e in expenses)
    gross_profit = revenue - cogs
    purchase_return_variance = _money_sum(
        dec(returned.taxable_total) - sum((dec(item.unit_cost) * dec(item.base_quantity)
                                          for item in returned.items), ZERO)
        for returned in purchase_returns
    )
    return {
        "sales": sales_total, "purchases": purchases_total, "revenue": revenue,
        "cogs": cogs, "gross_profit": gross_profit, "expenses": expense_total,
        "purchase_return_variance": purchase_return_variance,
        "net_profit": gross_profit + purchase_return_variance - expense_total,
        "gst_collected": gst_collected,
        "gst_paid": gst_paid,
    }


def _party_balances(data, party_type, as_of=None):
    parties = data["customers"] if party_type == "customer" else data["suppliers"]
    id_field = "customer_id" if party_type == "customer" else "supplier_id"
    entries_by_party = defaultdict(list)
    for entry in data["ledger"]:
        if entry.party_type == party_type and (as_of is None or _day(entry.occurred_at) <= as_of):
            entries_by_party[getattr(entry, id_field)].append(entry)
    result = []
    today = as_of or _now_day()
    for party in parties:
        entries = entries_by_party[party.id]
        balance = _money_sum([party.opening_balance] + [e.amount for e in entries])
        # Apply credits against oldest charges before calculating overdue exposure.
        charges = [[_day(party.created_at), dec(party.opening_balance)]] if dec(party.opening_balance) > 0 else []
        charges.extend([[_day(e.due_date) if e.due_date else None, dec(e.amount)]
                        for e in entries if dec(e.amount) > 0])
        charges.sort(key=lambda pair: pair[0] or date.max)
        credits = -sum((dec(e.amount) for e in entries if dec(e.amount) < 0), ZERO)
        for charge in charges:
            applied = min(charge[1], credits)
            charge[1] -= applied
            credits -= applied
        overdue = _money_sum(amount for due, amount in charges if due is not None and due < today)
        result.append({"id": party.id, "name": party.name, "phone": party.phone,
                       "opening_balance": money(party.opening_balance), "outstanding": balance,
                       "overdue": overdue})
    return result


def dashboard(session, start=None, end=None, branch_id=1):
    """Return live KPI values and chart arrays from committed transactions."""
    first, last = _date_bounds(start, end)
    today = _now_day()
    if first is None and last is None:
        first, last = today.replace(day=1), today
    data = _load(session, branch_id)
    selected = _period_values(data, first, last)
    today_values = _period_values(data, today, today)
    month_values = _period_values(data, today.replace(day=1), today)
    balances = {item.product_id: item for item in data["balances"]}
    customer_balances = _party_balances(data, "customer")
    supplier_balances = _party_balances(data, "supplier")
    active_products = [p for p in data["products"] if not p.archived]
    total_stock = sum((dec(balances[p.id].quantity) if p.id in balances else ZERO for p in active_products), ZERO)
    low_stock = sum(1 for p in active_products if ZERO < dec(balances[p.id].quantity if p.id in balances else ZERO) <= dec(p.minimum_stock))
    out_of_stock = sum(1 for p in active_products if dec(balances[p.id].quantity if p.id in balances else ZERO) <= 0)
    cost_value = _money_sum(dec(balances[p.id].quantity) * dec(balances[p.id].weighted_avg_cost)
                            for p in active_products if p.id in balances)
    sale_value = _money_sum(dec(balances[p.id].quantity) * dec(p.selling_price)
                            for p in active_products if p.id in balances)
    daily_sales = []
    for offset in range(13, -1, -1):
        day = today - timedelta(days=offset)
        daily_sales.append({"date": day.isoformat(), "total": _number(_period_values(data, day, day)["sales"])})
    monthly_revenue, monthly_profit, sales_vs_purchases, monthly_expenses = [], [], [], []
    for offset in range(11, -1, -1):
        month_start = _months_ago(today, offset)
        month_end = _months_ago(month_start, -1) - timedelta(days=1)
        values = _period_values(data, month_start, month_end)
        label = month_start.strftime("%Y-%m")
        monthly_revenue.append({"month": label, "total": _number(values["revenue"])})
        monthly_profit.append({"month": label, "gross": _number(values["gross_profit"]), "net": _number(values["net_profit"])})
        sales_vs_purchases.append({"month": label, "sales": _number(values["sales"]), "purchases": _number(values["purchases"])})
        monthly_expenses.append({"month": label, "total": _number(values["expenses"])})
    product_names = {p.id: p.name for p in data["products"]}
    product_categories = {p.id: p.category.name if p.category else "Uncategorised" for p in data["products"]}
    sold_units = defaultdict(lambda: ZERO)
    category_revenue = defaultdict(lambda: ZERO)
    for sale in _filtered(data["sales"], "sale_date", first, last):
        for item in sale.items:
            sold_units[item.product_id] += dec(item.base_quantity)
            category_revenue[product_categories.get(item.product_id, "Uncategorised")] += dec(item.taxable_value)
    for return_doc in _filtered(data["sale_returns"], "return_date", first, last):
        for item in return_doc.items:
            sold_units[item.product_id] -= dec(item.base_quantity)
            category_revenue[product_categories.get(item.product_id, "Uncategorised")] -= dec(item.taxable_value)
    top_products = [{"product_id": pid, "name": product_names.get(pid, str(pid)), "quantity": _quantity(qty)}
                    for pid, qty in sorted(sold_units.items(), key=lambda pair: pair[1], reverse=True)[:5]]
    category_performance = [{"category": name, "revenue": _number(amount)}
                            for name, amount in sorted(category_revenue.items(), key=lambda pair: pair[1], reverse=True)[:5]]
    payments = defaultdict(lambda: ZERO)
    for sale in data["sales"]:
        for payment in sale.payments:
            if _within(payment.paid_at, first, last):
                payments[payment.method] += dec(payment.amount)
    for payment in data["party_payments"]:
        if payment.party_type == "customer" and _within(payment.paid_at, first, last):
            payments[payment.method] += dec(payment.amount)
    for returned in _filtered(data["sale_returns"], "return_date", first, last):
        if dec(returned.refunded_amount) > 0:
            payments[returned.refund_method] -= dec(returned.refunded_amount)
    result = {
        "period_start": first.isoformat() if first else None, "period_end": last.isoformat() if last else None,
        "today_sales": _number(today_values["sales"]), "today_purchases": _number(today_values["purchases"]),
        "monthly_sales": _number(month_values["sales"]), "monthly_purchases": _number(month_values["purchases"]),
        "period_sales": _number(selected["sales"]), "period_purchases": _number(selected["purchases"]),
        "gross_profit": _number(selected["gross_profit"]), "net_profit": _number(selected["net_profit"]),
        "purchase_return_variance": _number(selected["purchase_return_variance"]),
        "inventory_cost_value": _number(cost_value), "inventory_sale_value": _number(sale_value),
        "gst_collected": _number(selected["gst_collected"]), "gst_paid": _number(selected["gst_paid"]),
        "customer_outstanding": _number(sum((max(ZERO, row["outstanding"]) for row in customer_balances), ZERO)),
        "supplier_outstanding": _number(sum((max(ZERO, row["outstanding"]) for row in supplier_balances), ZERO)),
        "total_products": len(active_products), "total_stock": _quantity(total_stock),
        "low_stock": low_stock, "out_of_stock": out_of_stock,
        "daily_sales": daily_sales, "monthly_revenue": monthly_revenue,
        "monthly_profit": monthly_profit, "sales_vs_purchases": sales_vs_purchases,
        "top_products": top_products, "category_performance": category_performance,
        "payment_breakdown": [{"method": method, "total": _number(amount)} for method, amount in payments.items()],
        "monthly_expenses": monthly_expenses,
    }
    return result


def report_rows(session, kind, start=None, end=None, branch_id=1):
    """Return JSON-safe table data for report views and exports.

    Date filters are inclusive in the store's India local calendar. Current
    inventory is a live balance; receivables/payables are as of the end date.
    """
    first, last = _date_bounds(start, end)
    kind = str(kind).strip().lower()
    allowed = {"sales", "purchases", "inventory", "profit", "gst", "gstr1_prep",
               "gstr3b_prep", "expenses", "product_sales", "category_sales",
               "customer_sales", "supplier_purchases", "purchase_price_trends",
               "stock_movement", "stock_ageing", "sales_daily", "sales_weekly",
               "sales_monthly", "sales_yearly",
               "customer_outstanding", "supplier_outstanding"}
    if kind not in allowed:
        raise ValueError(f"Unknown report kind: {kind}")
    data = _load(session, branch_id, include_movements=kind == "stock_movement")
    values = _period_values(data, first, last)
    sale_by_id = {sale.id: sale for sale in data["sales"]}
    purchase_by_id = {purchase.id: purchase for purchase in data["purchases"]}
    sale_item_by_id = {item.id: item for sale in data["sales"] for item in sale.items}
    purchase_item_by_id = {item.id: item for purchase in data["purchases"] for item in purchase.items}
    product_by_id = {product.id: product for product in data["products"]}
    supplier_by_id = {supplier.id: supplier for supplier in data["suppliers"]}
    title = kind.replace("_", " ").title()
    headers, rows, summary = [], [], {}

    if kind == "sales":
        headers = ["Date", "Invoice", "Customer", "Type", "Taxable", "GST", "Total", "Paid", "COGS", "Gross Profit"]
        for sale in _filtered(data["sales"], "sale_date", first, last):
            tax = dec(sale.cgst_total) + dec(sale.sgst_total) + dec(sale.igst_total)
            profit = dec(sale.taxable_total) - dec(sale.cost_of_goods)
            rows.append([_day(sale.sale_date).isoformat(), sale.invoice_no, sale.customer_name,
                         "Sale", _number(sale.taxable_total), _number(tax), _number(sale.grand_total),
                         _number(sale.paid_total), _number(sale.cost_of_goods), _number(profit)])
        for returned in _filtered(data["sale_returns"], "return_date", first, last):
            source = sale_by_id.get(returned.sale_id)
            returned_cost = _money_sum(dec(item.unit_cost) * dec(item.base_quantity) for item in returned.items)
            tax = dec(returned.cgst_total) + dec(returned.sgst_total) + dec(returned.igst_total)
            rows.append([_day(returned.return_date).isoformat(), returned.number,
                         source.customer_name if source else "", "Return",
                         _number(-dec(returned.taxable_total)), _number(-tax),
                         _number(-dec(returned.grand_total)), _number(-dec(returned.refunded_amount)),
                         _number(-returned_cost), _number(-dec(returned.taxable_total) + returned_cost)])
        summary = {"net_sales": _number(values["sales"]), "taxable_revenue": _number(values["revenue"]),
                   "gst_collected": _number(values["gst_collected"]), "gross_profit": _number(values["gross_profit"])}

    elif kind == "purchases":
        headers = ["Date", "Supplier Invoice", "Supplier", "Type", "Taxable", "GST", "Total", "Paid"]
        for purchase in _filtered(data["purchases"], "purchase_date", first, last):
            tax = dec(purchase.cgst_total) + dec(purchase.sgst_total) + dec(purchase.igst_total)
            supplier = supplier_by_id.get(purchase.supplier_id)
            rows.append([purchase.purchase_date.isoformat(), purchase.supplier_invoice_no or str(purchase.id),
                         supplier.name if supplier else "", "Purchase", _number(purchase.taxable_total),
                         _number(tax), _number(purchase.grand_total), _number(purchase.paid_total)])
        for returned in _filtered(data["purchase_returns"], "return_date", first, last):
            supplier = supplier_by_id.get(returned.supplier_id)
            rows.append([returned.return_date.isoformat(), returned.number,
                         supplier.name if supplier else "", "Return", _number(-dec(returned.taxable_total)),
                         _number(-dec(returned.tax_total)), _number(-dec(returned.grand_total)), "0.00"])
        summary = {"net_purchases": _number(values["purchases"]), "gst_on_purchases": _number(values["gst_paid"])}

    elif kind == "inventory":
        headers = ["SKU", "Product", "Category", "Unit", "Stock", "Minimum", "Average Cost",
                   "Cost Value", "Selling Price", "Selling Value", "Status"]
        balance_by_id = {balance.product_id: balance for balance in data["balances"]}
        for product in data["products"]:
            balance = balance_by_id.get(product.id)
            qty = dec(balance.quantity) if balance else ZERO
            avg_cost = dec(balance.weighted_avg_cost) if balance else ZERO
            status = "Archived" if product.archived else ("Out of stock" if qty <= 0 else ("Low stock" if qty <= dec(product.minimum_stock) else "In stock"))
            rows.append([product.sku, product.name, product.category.name if product.category else "",
                         product.base_unit.symbol if product.base_unit else "", _quantity(qty),
                         _quantity(product.minimum_stock), _number(avg_cost), _number(qty * avg_cost),
                         _number(product.selling_price), _number(qty * dec(product.selling_price)), status])
        summary = {"products": len(data["products"]),
                   "stock_quantity": _quantity(sum((dec(balance.quantity) for balance in data["balances"]), ZERO)),
                   "cost_value": _number(sum((dec(balance.quantity) * dec(balance.weighted_avg_cost) for balance in data["balances"]), ZERO))}

    elif kind == "profit":
        headers = ["Date", "Document", "Type", "Revenue Ex GST", "COGS", "Gross Profit",
                   "Return Cost Variance", "Expenses", "Net Profit"]
        for sale in _filtered(data["sales"], "sale_date", first, last):
            gross = dec(sale.taxable_total) - dec(sale.cost_of_goods)
            rows.append([_day(sale.sale_date).isoformat(), sale.invoice_no, "Sale", _number(sale.taxable_total),
                         _number(sale.cost_of_goods), _number(gross), "0.00", "0.00", _number(gross)])
        for returned in _filtered(data["sale_returns"], "return_date", first, last):
            returned_cost = _money_sum(dec(item.unit_cost) * dec(item.base_quantity) for item in returned.items)
            gross = -dec(returned.taxable_total) + returned_cost
            rows.append([_day(returned.return_date).isoformat(), returned.number, "Return",
                         _number(-dec(returned.taxable_total)), _number(-returned_cost),
                         _number(gross), "0.00", "0.00", _number(gross)])
        for returned in _filtered(data["purchase_returns"], "return_date", first, last):
            carrying_cost = sum((dec(item.unit_cost) * dec(item.base_quantity)
                                 for item in returned.items), ZERO)
            variance = dec(returned.taxable_total) - carrying_cost
            rows.append([returned.return_date.isoformat(), returned.number, "Purchase return variance",
                         "0.00", "0.00", "0.00", _number(variance), "0.00", _number(variance)])
        for expense in _filtered(data["expenses"], "expense_date", first, last):
            rows.append([expense.expense_date.isoformat(), f"EXP-{expense.id}", "Expense: " + expense.category,
                         "0.00", "0.00", "0.00", "0.00", _number(expense.amount),
                         _number(-dec(expense.amount))])
        summary = {"revenue_ex_gst": _number(values["revenue"]), "cogs": _number(values["cogs"]),
                   "gross_profit": _number(values["gross_profit"]),
                   "purchase_return_variance": _number(values["purchase_return_variance"]),
                   "expenses": _number(values["expenses"]),
                   "net_profit": _number(values["net_profit"])}

    elif kind in {"product_sales", "category_sales", "customer_sales"}:
        buckets = {}
        for event in _sales_line_events(data, first, last):
            product = product_by_id.get(event["product_id"])
            if kind == "product_sales":
                key = event["product_id"]
            elif kind == "category_sales":
                key = product.category.name if product and product.category else "Uncategorised"
            else:
                key = (event["customer_id"], event["customer_name"] if event["customer_id"] is None else "")
            bucket = buckets.setdefault(key, {"quantity": ZERO, "taxable": ZERO, "gst": ZERO,
                                              "sales": set(), "returns": set()})
            bucket["quantity"] += event["quantity"]
            bucket["taxable"] += event["taxable"]
            bucket["gst"] += event["gst"]
            bucket["sales" if event["type"] == "sale" else "returns"].add(event["document_id"])
        if kind == "product_sales":
            headers = ["Product", "SKU", "Base Unit", "Qty Sold", "Invoice Count", "Return Count",
                       "Taxable", "GST", "Line Sales"]
            for product_id, bucket in buckets.items():
                product = product_by_id.get(product_id)
                rows.append([product.name if product else str(product_id), product.sku if product else "",
                             product.base_unit.symbol if product and product.base_unit else "",
                             _quantity(bucket["quantity"]), len(bucket["sales"]), len(bucket["returns"]),
                             _number(bucket["taxable"]), _number(bucket["gst"]),
                             _number(bucket["taxable"] + bucket["gst"])])
        elif kind == "category_sales":
            headers = ["Category", "Invoice Count", "Return Count", "Taxable", "GST", "Line Sales"]
            for category, bucket in buckets.items():
                rows.append([category, len(bucket["sales"]), len(bucket["returns"]),
                             _number(bucket["taxable"]), _number(bucket["gst"]),
                             _number(bucket["taxable"] + bucket["gst"])])
        else:
            headers = ["Customer", "Invoice Count", "Return Count", "Taxable", "GST", "Line Sales"]
            customer_by_id = {customer.id: customer for customer in data["customers"]}
            for (customer_id, snapshot_name), bucket in buckets.items():
                customer = customer_by_id.get(customer_id)
                rows.append([customer.name if customer else (snapshot_name or "Walk-in Customer"),
                             len(bucket["sales"]), len(bucket["returns"]), _number(bucket["taxable"]),
                             _number(bucket["gst"]), _number(bucket["taxable"] + bucket["gst"])])
        rows.sort(key=lambda row: dec(row[-1]), reverse=True)
        note = "Returns reduce the period of their posting; Line Sales excludes invoice round-off."
        if kind == "category_sales":
            note += " Categories reflect the product's current category, not a historical snapshot."
        if kind == "customer_sales":
            note += " Unregistered walk-in sales are grouped by invoice customer name."
        summary = {"taxable_revenue_after_returns": _number(values["revenue"]),
                   "gst_after_returns": _number(values["gst_collected"]), "note": note}

    elif kind == "supplier_purchases":
        headers = ["Supplier", "Purchase Count", "Return Count", "Taxable", "GST", "Net Purchases"]
        buckets = {}
        for purchase in _filtered(data["purchases"], "purchase_date", first, last):
            bucket = buckets.setdefault(purchase.supplier_id, {"purchases": set(), "returns": set(),
                                                               "taxable": ZERO, "gst": ZERO, "total": ZERO})
            bucket["purchases"].add(purchase.id)
            bucket["taxable"] += dec(purchase.taxable_total)
            bucket["gst"] += dec(purchase.cgst_total) + dec(purchase.sgst_total) + dec(purchase.igst_total)
            bucket["total"] += dec(purchase.grand_total)
        for returned in _filtered(data["purchase_returns"], "return_date", first, last):
            bucket = buckets.setdefault(returned.supplier_id, {"purchases": set(), "returns": set(),
                                                               "taxable": ZERO, "gst": ZERO, "total": ZERO})
            bucket["returns"].add(returned.id)
            bucket["taxable"] -= dec(returned.taxable_total)
            bucket["gst"] -= dec(returned.tax_total)
            bucket["total"] -= dec(returned.grand_total)
        for supplier_id, bucket in buckets.items():
            supplier = supplier_by_id.get(supplier_id)
            rows.append([supplier.name if supplier else str(supplier_id), len(bucket["purchases"]),
                         len(bucket["returns"]), _number(bucket["taxable"]), _number(bucket["gst"]),
                         _number(bucket["total"])])
        rows.sort(key=lambda row: dec(row[-1]), reverse=True)
        summary = {"net_purchases": _number(values["purchases"]),
                   "gst_on_purchases": _number(values["gst_paid"]),
                   "note": "Returns reduce the period in which they were confirmed. Supplier names reflect current master data."}

    elif kind == "purchase_price_trends":
        headers = ["Date", "Supplier Invoice", "Supplier", "Product", "SKU", "Quantity", "Unit",
                   "Purchase Rate", "Cost per Base Unit", "Change %"]
        unit_by_id = {unit.id: unit for unit in session.query(Unit).all()}
        entries = [(purchase, item) for purchase in data["purchases"]
                   if last is None or purchase.purchase_date <= last
                   for item in purchase.items]
        entries.sort(key=lambda pair: (pair[0].purchase_date, pair[0].id, pair[1].id))
        previous_base_cost = {}
        for purchase, item in entries:
            product = product_by_id.get(item.product_id)
            supplier = supplier_by_id.get(purchase.supplier_id)
            unit = unit_by_id.get(item.unit_id)
            base_cost = dec(item.taxable_value) / dec(item.base_quantity) if dec(item.base_quantity) else ZERO
            previous = previous_base_cost.get(item.product_id)
            change = _number((base_cost - previous) * 100 / previous) if previous and previous > 0 else ""
            if _within(purchase.purchase_date, first, last):
                rows.append([purchase.purchase_date.isoformat(), purchase.supplier_invoice_no or str(purchase.id),
                             supplier.name if supplier else "", product.name if product else str(item.product_id),
                             product.sku if product else "", _quantity(item.quantity), unit.symbol if unit else "",
                             _quantity(item.unit_price), _quantity(base_cost), change])
            previous_base_cost[item.product_id] = base_cost
        summary = {"purchase_lines": len(rows),
                   "note": "Historical confirmed purchase rates are shown per selected unit. Cost per base unit uses net taxable value after discount divided by base quantity; purchase returns do not alter the recorded rate. Restricted to administrators."}

    elif kind == "stock_movement":
        headers = ["Date", "Product", "SKU", "Base Unit", "Movement", "Quantity Delta",
                   "Balance After", "Source", "Note"]
        for movement in _filtered(data["movements"], "created_at", first, last):
            product = product_by_id.get(movement.product_id)
            rows.append([_day(movement.created_at).isoformat(),
                         product.name if product else str(movement.product_id),
                         product.sku if product else "",
                         product.base_unit.symbol if product and product.base_unit else "",
                         movement.movement_type, _quantity(movement.quantity_delta),
                         _quantity(movement.balance_after),
                         f"{movement.source_type} #{movement.source_id}", movement.note or ""])
        summary = {"movements": len(rows),
                   "note": "Quantities are in each product's base unit. Cost and value fields are excluded from this operational report."}

    elif kind == "stock_ageing":
        title = "Stock Ageing and Movement (90 Days)"
        headers = ["Product", "SKU", "Category", "Base Unit", "Stock", "90-Day Net Sold",
                   "Turnover Ratio", "Last Sale", "Days Since Sale", "Class"]
        as_of = last or _now_day()
        window_start = max(first, as_of - timedelta(days=89)) if first else as_of - timedelta(days=89)
        net_sold = defaultdict(lambda: ZERO)
        last_sale = {}
        for event in _sales_line_events(data, None, as_of):
            if event["type"] == "sale":
                last_sale[event["product_id"]] = max(last_sale.get(event["product_id"], date.min), event["date"])
            if window_start <= event["date"] <= as_of:
                net_sold[event["product_id"]] += event["quantity"]
        balance_by_id = {balance.product_id: balance for balance in data["balances"]}
        counts = {"Dead": 0, "Slow": 0, "Fast": 0}
        for product in data["products"]:
            balance = balance_by_id.get(product.id)
            stock = dec(balance.quantity) if balance else ZERO
            if product.archived and stock <= 0:
                continue
            sold = net_sold[product.id]
            ratio = sold / stock if stock > 0 else None
            status = "Dead" if sold <= 0 else ("Fast" if stock <= 0 or ratio >= 1 else "Slow")
            counts[status] += 1
            last_sold = last_sale.get(product.id)
            rows.append([product.name, product.sku, product.category.name if product.category else "",
                         product.base_unit.symbol if product.base_unit else "", _quantity(stock),
                         _quantity(sold), _number(ratio) if ratio is not None else "",
                         last_sold.isoformat() if last_sold else "",
                         (as_of - last_sold).days if last_sold else "", status])
        rows.sort(key=lambda row: ({"Dead": 0, "Slow": 1, "Fast": 2}[row[-1]], -dec(row[4]), row[0]))
        summary = {"window_start": window_start.isoformat(), "window_end": as_of.isoformat(),
                   "dead_products": counts["Dead"], "slow_products": counts["Slow"],
                   "fast_products": counts["Fast"],
                   "note": "Dead means no net sales in the window; Slow means net sales are below current stock; Fast means net sales meet or exceed current stock (or stock is zero). Returns reduce sales on their posting date. Stock is the current live balance, including for historical filters."}

    elif kind in {"sales_daily", "sales_weekly", "sales_monthly", "sales_yearly"}:
        labels = {"sales_daily": "Daily Sales", "sales_weekly": "Weekly Sales",
                  "sales_monthly": "Monthly Sales", "sales_yearly": "Yearly Sales"}
        title = labels[kind]
        headers = ["Period", "Invoice Count", "Return Count", "Taxable", "GST", "Net Sales"]

        def period_label(day):
            if kind == "sales_daily":
                return day.isoformat()
            if kind == "sales_weekly":
                iso = day.isocalendar()
                return f"{iso.year}-W{iso.week:02d}"
            if kind == "sales_monthly":
                return day.strftime("%Y-%m")
            return str(day.year)

        buckets = {}
        for sale in _filtered(data["sales"], "sale_date", first, last):
            key = period_label(_day(sale.sale_date))
            bucket = buckets.setdefault(key, {"sales": set(), "returns": set(), "taxable": ZERO,
                                              "gst": ZERO, "total": ZERO})
            bucket["sales"].add(sale.id)
            bucket["taxable"] += dec(sale.taxable_total)
            bucket["gst"] += dec(sale.cgst_total) + dec(sale.sgst_total) + dec(sale.igst_total)
            bucket["total"] += dec(sale.grand_total)
        for returned in _filtered(data["sale_returns"], "return_date", first, last):
            key = period_label(_day(returned.return_date))
            bucket = buckets.setdefault(key, {"sales": set(), "returns": set(), "taxable": ZERO,
                                              "gst": ZERO, "total": ZERO})
            bucket["returns"].add(returned.id)
            bucket["taxable"] -= dec(returned.taxable_total)
            bucket["gst"] -= dec(returned.cgst_total) + dec(returned.sgst_total) + dec(returned.igst_total)
            bucket["total"] -= dec(returned.grand_total)
        for label, bucket in buckets.items():
            rows.append([label, len(bucket["sales"]), len(bucket["returns"]),
                         _number(bucket["taxable"]), _number(bucket["gst"]), _number(bucket["total"])])
        summary = {"net_sales": _number(values["sales"]),
                   "taxable_revenue_after_returns": _number(values["revenue"]),
                   "gst_after_returns": _number(values["gst_collected"]),
                   "note": "Returns reduce the period of their posting. Weeks follow ISO Monday-to-Sunday numbering."}

    elif kind == "gst":
        headers = ["Date", "Document", "Flow", "HSN", "Taxable", "CGST", "SGST", "IGST", "Total GST"]
        for sale in _filtered(data["sales"], "sale_date", first, last):
            for item in sale.items:
                tax = dec(item.cgst) + dec(item.sgst) + dec(item.igst)
                rows.append([_day(sale.sale_date).isoformat(), sale.invoice_no, "Outward", item.hsn_code,
                             _number(item.taxable_value), _number(item.cgst), _number(item.sgst),
                             _number(item.igst), _number(tax)])
        for returned in _filtered(data["sale_returns"], "return_date", first, last):
            for item in returned.items:
                original = sale_item_by_id.get(item.sale_item_id)
                tax = dec(item.cgst) + dec(item.sgst) + dec(item.igst)
                rows.append([_day(returned.return_date).isoformat(), returned.number, "Outward return",
                             original.hsn_code if original else "", _number(-dec(item.taxable_value)),
                             _number(-dec(item.cgst)), _number(-dec(item.sgst)), _number(-dec(item.igst)),
                             _number(-tax)])
        for purchase in _filtered(data["purchases"], "purchase_date", first, last):
            for item in purchase.items:
                tax = dec(item.cgst) + dec(item.sgst) + dec(item.igst)
                rows.append([purchase.purchase_date.isoformat(), purchase.supplier_invoice_no or str(purchase.id),
                             "Inward", item.hsn_code, _number(item.taxable_value), _number(item.cgst),
                             _number(item.sgst), _number(item.igst), _number(tax)])
        for returned in _filtered(data["purchase_returns"], "return_date", first, last):
            for item in returned.items:
                original = purchase_item_by_id.get(item.purchase_item_id)
                cgst, sgst, igst = dec(item.cgst), dec(item.sgst), dec(item.igst)
                tax = cgst + sgst + igst
                rows.append([returned.return_date.isoformat(), returned.number, "Inward return",
                             original.hsn_code if original else "", _number(-dec(item.taxable_value)),
                             _number(-cgst), _number(-sgst), _number(-igst), _number(-tax)])
        summary = {"gst_collected": _number(values["gst_collected"]),
                   "gst_on_purchases": _number(values["gst_paid"]),
                   "note": "Preparation data only; purchase GST is not a claim of eligible input tax credit."}

    elif kind == "gstr1_prep":
        title = "GSTR-1 Preparation Worksheet"
        headers = ["Date", "Document", "Document Type", "Original Invoice", "Recipient GSTIN",
                   "Recipient", "Place of Supply", "HSN", "Taxable", "CGST", "SGST", "IGST", "Total Tax"]
        for sale in _filtered(data["sales"], "sale_date", first, last):
            doc_type = "B2B invoice" if sale.customer_gstin else "B2C invoice"
            if sale.invoice_type == "bill_of_supply":
                doc_type = "Bill of supply"
            for item in sale.items:
                tax = dec(item.cgst) + dec(item.sgst) + dec(item.igst)
                rows.append([_day(sale.sale_date).isoformat(), sale.invoice_no, doc_type, "",
                             sale.customer_gstin or "", sale.customer_name,
                             sale.place_of_supply_code or "", item.hsn_code,
                             _number(item.taxable_value), _number(item.cgst), _number(item.sgst),
                             _number(item.igst), _number(tax)])
        for returned in _filtered(data["sale_returns"], "return_date", first, last):
            source = sale_by_id.get(returned.sale_id)
            for item in returned.items:
                original = sale_item_by_id.get(item.sale_item_id)
                tax = dec(item.cgst) + dec(item.sgst) + dec(item.igst)
                rows.append([_day(returned.return_date).isoformat(), returned.number, "Credit note",
                             source.invoice_no if source else "", source.customer_gstin if source else "",
                             source.customer_name if source else "",
                             source.place_of_supply_code if source else "",
                             original.hsn_code if original else "", _number(-dec(item.taxable_value)),
                             _number(-dec(item.cgst)), _number(-dec(item.sgst)), _number(-dec(item.igst)),
                             _number(-tax)])
        summary = {
            "outward_taxable_after_returns": _number(values["revenue"]),
            "outward_gst_after_returns": _number(values["gst_collected"]),
            "preparation_only": True,
            "note": "Review classification, recipient details, HSN and GST portal records before filing. This worksheet is not a return.",
        }

    elif kind == "gstr3b_prep":
        title = "GSTR-3B Preparation Worksheet"
        headers = ["Section", "Document Count", "Taxable", "CGST", "SGST", "IGST", "Total GST", "Review Note"]
        sales = _filtered(data["sales"], "sale_date", first, last)
        sale_returns = _filtered(data["sale_returns"], "return_date", first, last)
        purchases = _filtered(data["purchases"], "purchase_date", first, last)
        purchase_returns = _filtered(data["purchase_returns"], "return_date", first, last)

        def totals(documents, taxable_field="taxable_total"):
            return [
                _money_sum(getattr(document, taxable_field) for document in documents),
                _money_sum(document.cgst_total for document in documents),
                _money_sum(document.sgst_total for document in documents),
                _money_sum(document.igst_total for document in documents),
            ]

        outward = totals(sales)
        outward_returns = totals(sale_returns)
        inward = totals(purchases)
        inward_return_tax = [ZERO, ZERO, ZERO]
        for returned in purchase_returns:
            for item in returned.items:
                inward_return_tax[0] += dec(item.cgst)
                inward_return_tax[1] += dec(item.sgst)
                inward_return_tax[2] += dec(item.igst)
        inward_returns = [_money_sum(r.taxable_total for r in purchase_returns)] + inward_return_tax

        def append_section(name, count, amounts, note=""):
            taxable, cgst, sgst, igst = amounts
            rows.append([name, count, _number(taxable), _number(cgst), _number(sgst),
                         _number(igst), _number(cgst + sgst + igst), note])

        append_section("Outward invoices", len(sales), outward,
                       "Review taxable, nil-rated and exempt classification.")
        append_section("Outward credit notes / returns", len(sale_returns), [-part for part in outward_returns],
                       "Verify credit-note reporting period and eligibility.")
        append_section("Net outward after returns", len(sales) + len(sale_returns),
                       [part - returned for part, returned in zip(outward, outward_returns)])
        append_section("Inward purchase invoices", len(purchases), inward,
                       "GST on invoices, not eligible ITC.")
        append_section("Inward purchase returns", len(purchase_returns), [-part for part in inward_returns],
                       "Purchase-return tax adjustment before eligibility review.")
        append_section("Net inward GST before ITC review", len(purchases) + len(purchase_returns),
                       [part - returned for part, returned in zip(inward, inward_returns)],
                       "Do not claim this amount as input tax credit without reconciliation.")
        summary = {
            "outward_taxable_after_returns": _number(values["revenue"]),
            "outward_gst_after_returns": _number(values["gst_collected"]),
            "inward_gst_before_itc_review": _number(values["gst_paid"]),
            "preparation_only": True,
            "note": "This is a preparation worksheet, not a ready-to-file GSTR-3B or an ITC eligibility calculation.",
        }

    elif kind == "expenses":
        headers = ["Date", "Category", "Description", "Payment Method", "Amount"]
        for expense in _filtered(data["expenses"], "expense_date", first, last):
            rows.append([expense.expense_date.isoformat(), expense.category, expense.description,
                         expense.payment_method, _number(expense.amount)])
        summary = {"total_expenses": _number(values["expenses"])}

    else:
        party_type = "customer" if kind == "customer_outstanding" else "supplier"
        headers = ["Party", "Phone", "Opening Balance", "Outstanding", "Overdue"]
        balances = _party_balances(data, party_type, last)
        rows = [[item["name"], item["phone"], _number(item["opening_balance"]),
                 _number(item["outstanding"]), _number(item["overdue"])]
                for item in balances if item["outstanding"] != 0]
        summary = {"total_outstanding": _number(sum((max(ZERO, item["outstanding"]) for item in balances), ZERO)),
                   "total_overdue": _number(sum((max(ZERO, item["overdue"]) for item in balances), ZERO))}

    if kind not in {"inventory", "customer_outstanding", "supplier_outstanding", "gstr3b_prep",
                    "product_sales", "category_sales", "customer_sales", "supplier_purchases",
                    "stock_ageing"}:
        rows.sort(key=lambda row: row[0], reverse=True)
    return {"title": title, "headers": headers, "rows": rows, "summary": summary,
            "period_start": first.isoformat() if first else None,
            "period_end": last.isoformat() if last else None}


def export_xlsx(headers, rows, title="Report"):
    """Return an Excel workbook as bytes, preserving numeric cells as numbers."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = str(title or "Report")[:31]
    sheet.append([str(title or "Report")])
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(1, len(headers)))
    sheet["A1"].font = Font(size=16, bold=True, color="FFFFFF")
    sheet["A1"].fill = PatternFill("solid", fgColor="16324F")
    sheet["A1"].alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 28
    sheet.append(list(headers))
    for cell in sheet[2]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="247BA0")
    for row in rows:
        values = list(row.values()) if isinstance(row, dict) else list(row)
        sheet.append([_excel_cell(value, headers[index] if index < len(headers) else "")
                      for index, value in enumerate(values)])
    sheet.freeze_panes = "A3"
    sheet.auto_filter.ref = f"A2:{get_column_letter(max(1, len(headers)))}{max(2, sheet.max_row)}"
    for col_idx, header in enumerate(headers, 1):
        values = [str(header)]
        for row_idx in range(3, min(sheet.max_row, 1002) + 1):
            cell_value = sheet.cell(row_idx, col_idx).value
            values.append("" if cell_value is None else str(cell_value))
        sheet.column_dimensions[get_column_letter(col_idx)].width = min(48, max(12, max(map(len, values)) + 2))
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _excel_cell(value, header=""):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime, int, float)) or value is None:
        return value
    if header == "Date" and isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    if header in NUMERIC_HEADERS and isinstance(value, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        return float(value)
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return str(value)


def printable_html(title, headers, rows, subtitle=""):
    """Return escaped, self-contained HTML suitable for browser printing."""
    heading = escape(str(title))
    caption = escape(str(subtitle))
    th = "".join(f"<th>{escape(str(item))}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(value if value is not None else ''))}</td>" for value in (row.values() if isinstance(row, dict) else row)) + "</tr>"
        for row in rows
    )
    return ("<!doctype html><html><head><meta charset='utf-8'><title>" + heading + "</title>"
            "<style>@page{size:A4;margin:15mm}body{font:12px Arial,sans-serif;color:#183047}"
            "h1{font-size:20px;margin:0 0 5px}.caption{color:#66788a;margin:0 0 20px}"
            "table{width:100%;border-collapse:collapse}th{background:#16324f;color:white;text-align:left}"
            "td,th{padding:7px;border-bottom:1px solid #dbe4eb;vertical-align:top}"
            "tr:nth-child(even){background:#f5f8fa}thead{display:table-header-group}"
            "</style></head><body><h1>" + heading + "</h1><div class='caption'>" + caption +
            "</div><table><thead><tr>" + th + "</tr></thead><tbody>" + body + "</tbody></table></body></html>")


def export_pdf(headers, rows, title="Report", subtitle=""):
    """Return an A4 PDF as bytes. ReportLab is imported only for PDF requests."""
    try:
        import reportlab
    except ModuleNotFoundError:
        from simplepdf import table_pdf
        return table_pdf(title, headers, rows, [subtitle] if subtitle else [],
                         landscape=len(headers) > 6)
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    stream = BytesIO()
    width = landscape(A4) if len(headers) > 6 else A4
    document = SimpleDocTemplate(stream, pagesize=width, leftMargin=28, rightMargin=28,
                                 topMargin=30, bottomMargin=28)
    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("ReportCell", parent=styles["Normal"], fontSize=7, leading=9)
    header_style = ParagraphStyle("ReportHeader", parent=cell_style, textColor=colors.white)
    table_rows = [[Paragraph(escape(str(header)), header_style) for header in headers]]
    for row in rows:
        table_rows.append([Paragraph(escape(str(value if value is not None else "")).replace("\n", "<br/>"), cell_style)
                           for value in (row.values() if isinstance(row, dict) else row)])
    elements = [Paragraph(escape(str(title)), styles["Title"])]
    if subtitle:
        elements.extend([Paragraph(escape(str(subtitle)), styles["Normal"]), Spacer(1, 12)])
    if headers:
        table_width = width[0] - document.leftMargin - document.rightMargin
        table = LongTable(table_rows, colWidths=[table_width / len(headers)] * len(headers),
                          repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16324f")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8fa")]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#dbe4eb")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(table)
    document.build(elements)
    return stream.getvalue()


_ONES = (
    "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight",
    "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen",
    "Sixteen", "Seventeen", "Eighteen", "Nineteen",
)
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")
_SCALES = ((10**11, "Kharab"), (10**9, "Arab"), (10**7, "Crore"),
           (10**5, "Lakh"), (10**3, "Thousand"), (100, "Hundred"))
_STATE_NAMES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "25": "Daman and Diu", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "28": "Andhra Pradesh", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman and Nicobar Islands", "36": "Telangana",
    "37": "Andhra Pradesh", "38": "Ladakh",
}


def _words_under_hundred(number):
    if number < 20:
        return _ONES[number]
    tens, ones = divmod(number, 10)
    return _TENS[tens] + (" " + _ONES[ones] if ones else "")


def _number_words(number):
    if number < 100:
        return _words_under_hundred(number)
    for scale, name in _SCALES:
        if number >= scale:
            high, low = divmod(number, scale)
            prefix = _number_words(high) + " " + name
            return prefix + (" " + _number_words(low) if low else "")
    return "Zero"


def amount_in_words(value):
    """Write a non-negative INR amount using Indian number groups."""
    rounded = money(value)
    if rounded < 0:
        return "Minus " + amount_in_words(-rounded)
    rupees = int(rounded)
    paise = int((rounded - rupees) * 100)
    result = "Rupees " + _number_words(rupees)
    if paise:
        result += " and Paise " + _number_words(paise)
    return result + " Only"


def _field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _invoice_unit(item):
    unit = _field(item, "unit_symbol") or _field(item, "unit")
    if unit:
        return unit.symbol if hasattr(unit, "symbol") else str(unit)
    unit_id = _field(item, "unit_id")
    db = object_session(item) if not isinstance(item, dict) else None
    if db is not None and unit_id is not None:
        record = db.get(Unit, unit_id)
        if record is not None:
            return record.symbol
    product = _field(item, "product")
    base_unit = _field(product, "base_unit") if product else None
    return _field(base_unit, "symbol", "") if base_unit else ""


def invoice_pdf(sale, items, settings, payments):
    """Render one posted sale as an A4 GST/retail invoice PDF.

    The document values come from the sale and its immutable line snapshots.
    This method does not register an e-invoice or make a tax filing.
    """
    payment_type = str(_field(sale, "payment_type", "") or "")
    payment_label = {
        "cash": "Cash", "upi": "UPI", "card": "Debit / credit card",
        "bank_transfer": "Bank transfer", "credit": "Customer credit",
        "split": "Split payments",
    }.get(payment_type, payment_type.replace("_", " ").title() or "Unspecified")
    try:
        import reportlab
    except ModuleNotFoundError:
        from simplepdf import table_pdf
        invoice_type = _field(sale, "invoice_type", "tax_invoice")
        labels = {"tax_invoice": "GST TAX INVOICE", "bill_of_supply": "BILL OF SUPPLY", "retail": "RETAIL BILL"}
        title = labels.get(invoice_type, str(invoice_type).replace("_", " ").upper())
        headers = ["Item", "HSN", "Qty", "Unit", "Rate", "Taxable", "GST", "Total"]
        rows = [[_field(item, "description", ""), _field(item, "hsn_code", ""),
                 _field(item, "quantity", ""), _invoice_unit(item), _number(_field(item, "unit_price", 0)),
                 _number(_field(item, "taxable_value", 0)),
                 _number(dec(_field(item, "cgst", 0)) + dec(_field(item, "sgst", 0)) + dec(_field(item, "igst", 0))),
                 _number(_field(item, "line_total", 0))] for item in items]
        lines = [_field(settings, "store_name", "HARITHA ENTERPRISES"), _field(settings, "address", ""),
                 f"GSTIN: {_field(settings, 'gstin', '')}  |  State: {_field(settings, 'state', '')} ({_field(settings, 'state_code', '')})",
                 f"Invoice: {_field(sale, 'invoice_no', '')}  |  Date: {str(_field(sale, 'sale_date', ''))[:10]}",
                 f"Customer: {_field(sale, 'customer_name', '')}  |  GSTIN: {_field(sale, 'customer_gstin', '')}  |  Place of supply: {_field(sale, 'place_of_supply_code', '')}"]
        footer = [f"Taxable: INR {_number(_field(sale, 'taxable_total', 0))}   CGST: INR {_number(_field(sale, 'cgst_total', 0))}   SGST: INR {_number(_field(sale, 'sgst_total', 0))}   IGST: INR {_number(_field(sale, 'igst_total', 0))}",
                  f"Grand total: INR {_number(_field(sale, 'grand_total', 0))}",
                  amount_in_words(_field(sale, "grand_total", 0)),
                  f"Payment type: {payment_label}"]
        if payments:
            footer.append("Received: " + "; ".join(
                f"{str(_field(p, 'method', '')).upper()} INR {_number(_field(p, 'amount', 0))}"
                for p in payments))
        return table_pdf(title, headers, rows, lines, footer, landscape=False)
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import Image, LongTable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    invoice_type = _field(sale, "invoice_type", "tax_invoice")
    labels = {"tax_invoice": "GST TAX INVOICE", "bill_of_supply": "BILL OF SUPPLY",
              "retail": "RETAIL BILL", "retail_bill": "RETAIL BILL"}
    title = labels.get(invoice_type, str(invoice_type).replace("_", " ").upper())
    stream = BytesIO()
    document = SimpleDocTemplate(stream, pagesize=A4, leftMargin=34, rightMargin=34,
                                 topMargin=32, bottomMargin=32)
    usable = A4[0] - document.leftMargin - document.rightMargin
    styles = getSampleStyleSheet()
    normal = ParagraphStyle("InvoiceNormal", parent=styles["Normal"], fontName="Helvetica", fontSize=8, leading=11)
    small = ParagraphStyle("InvoiceSmall", parent=normal, fontSize=7, leading=9)
    heading = ParagraphStyle("InvoiceHeading", parent=normal, fontName="Helvetica-Bold", fontSize=17, leading=20)
    label = ParagraphStyle("InvoiceLabel", parent=normal, fontName="Helvetica-Bold", fontSize=8)
    table_heading = ParagraphStyle("InvoiceTableHeading", parent=label, fontSize=7, leading=9, textColor=colors.white)

    def p(value, style=normal):
        return Paragraph(escape(str(value if value is not None else "")).replace("\n", "<br/>"), style)

    def amt(value):
        return "INR " + _number(value)

    store_name = _field(settings, "store_name", "HARITHA ENTERPRISES")
    store_state = _field(settings, "state", "")
    store_code = str(_field(settings, "state_code", "") or "")
    store_lines = [store_name, _field(settings, "address", "")]
    contact = " | ".join(part for part in [_field(settings, "phone", ""), _field(settings, "email", "")] if part)
    if contact:
        store_lines.append(contact)
    if _field(settings, "gstin", ""):
        store_lines.append("GSTIN: " + str(_field(settings, "gstin")))
    if store_state:
        store_lines.append(f"State: {store_state} ({store_code})" if store_code else f"State: {store_state}")
    logo_path = _field(settings, "logo_path", "")
    logo = None
    logo_source = None
    if isinstance(logo_path, str) and logo_path.startswith("s3://"):
        bucket, _, key = logo_path[5:].partition("/")
        if bucket and key:
            try:
                import boto3
                response = boto3.client("s3").get_object(Bucket=bucket, Key=key)
                logo_bytes = response["Body"].read(5_000_001)
                if len(logo_bytes) <= 5_000_000:
                    logo_source = BytesIO(logo_bytes)
            except Exception:
                logo_source = None
    elif logo_path and os.path.isfile(logo_path):
        logo_source = logo_path
    if logo_source is not None:
        try:
            source = ImageReader(logo_source)
            w, h = source.getSize()
            scale = min(52 / w, 52 / h)
            if isinstance(logo_source, BytesIO):
                logo_source.seek(0)
            logo = Image(logo_source, width=w * scale, height=h * scale)
        except Exception:
            logo = None
    store_cell = [p(store_name, heading)] + [p(value, small) for value in store_lines[1:] if value]
    header_cells = [[logo or "", store_cell, p(title, label)]]
    header_table = Table(header_cells, colWidths=[58, usable - 190, 132])
    header_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                      ("LINEBELOW", (0, 0), (-1, -1), 1.2, colors.HexColor("#16324f")),
                                      ("BOTTOMPADDING", (0, 0), (-1, -1), 10)]))

    sale_date = _field(sale, "sale_date")
    if isinstance(sale_date, datetime):
        if sale_date.tzinfo is None:
            sale_date = sale_date.replace(tzinfo=timezone.utc)
        date_text = sale_date.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
    else:
        date_text = str(sale_date or "")
    place_code = str(_field(sale, "place_of_supply_code", "") or "")
    place_name = _STATE_NAMES.get(place_code, "")
    place_text = (place_name + " (" + place_code + ")") if place_name else place_code
    buyer = ["Bill to: " + str(_field(sale, "customer_name", "Walk-in Customer")),
             _field(sale, "customer_address", "")]
    if _field(sale, "customer_gstin", ""):
        buyer.append("GSTIN: " + str(_field(sale, "customer_gstin")))
    if _field(sale, "customer_state_code", ""):
        code = str(_field(sale, "customer_state_code"))
        buyer.append("State: " + _STATE_NAMES.get(code, "") + " (" + code + ")")
    invoice_meta = ["Invoice No: " + str(_field(sale, "invoice_no", "")),
                    "Date: " + date_text,
                    "Financial year: " + str(_field(sale, "financial_year", ""))]
    if place_text:
        invoice_meta.append("Place of supply: " + place_text)
    details = Table([[[p(line, small) for line in buyer if line],
                      [p(line, small) for line in invoice_meta if line]]], colWidths=[usable * 0.52, usable * 0.48])
    details.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#dbe4eb")),
                                 ("INNERGRID", (0, 0), (-1, -1), .5, colors.HexColor("#dbe4eb")),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                 ("TOPPADDING", (0, 0), (-1, -1), 8),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))

    headers = ["#", "Description / HSN", "Qty / Unit", "Rate", "Discount", "Taxable", "GST breakup", "Total"]
    item_rows = [[p(value, table_heading) for value in headers]]
    for index, item in enumerate(items, 1):
        gst_rate = _field(item, "gst_rate", ZERO)
        cgst, sgst, igst = (dec(_field(item, name, ZERO)) for name in ("cgst", "sgst", "igst"))
        tax_lines = [f"{_quantity(gst_rate)}%"]
        if cgst:
            tax_lines.append("CGST " + _number(cgst))
        if sgst:
            tax_lines.append("SGST " + _number(sgst))
        if igst:
            tax_lines.append("IGST " + _number(igst))
        description = _field(item, "description") or _field(_field(item, "product"), "name", "Item")
        hsn = _field(item, "hsn_code", "")
        item_rows.append([
            p(index, small), p(str(description) + ("\nHSN: " + str(hsn) if hsn else ""), small),
            p(_quantity(_field(item, "quantity", ZERO)) + " " + _invoice_unit(item), small),
            p(_number(_field(item, "unit_price", ZERO)), small), p(_number(_field(item, "discount", ZERO)), small),
            p(_number(_field(item, "taxable_value", ZERO)), small), p("\n".join(tax_lines), small),
            p(_number(_field(item, "line_total", ZERO)), small),
        ])
    widths = [19, 125, 55, 50, 48, 60, 83, usable - 440]
    line_table = LongTable(item_rows, colWidths=widths, repeatRows=1)
    line_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16324f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8fa")]),
        ("LINEBELOW", (0, 0), (-1, -1), .3, colors.HexColor("#dbe4eb")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    totals = [
        ("Subtotal", _field(sale, "subtotal", ZERO)),
        ("Discount", _field(sale, "discount_total", ZERO)),
        ("Taxable value", _field(sale, "taxable_total", ZERO)),
        ("CGST", _field(sale, "cgst_total", ZERO)),
        ("SGST", _field(sale, "sgst_total", ZERO)),
        ("IGST", _field(sale, "igst_total", ZERO)),
        ("Round off", _field(sale, "round_off", ZERO)),
        ("Grand total", _field(sale, "grand_total", ZERO)),
        ("Amount received", _field(sale, "paid_total", ZERO)),
        ("Balance due", dec(_field(sale, "grand_total", ZERO)) - dec(_field(sale, "paid_total", ZERO))),
    ]
    total_rows = [[p(name, label if name == "Grand total" else small),
                   p(amt(value), label if name == "Grand total" else small)] for name, value in totals]
    totals_table = Table(total_rows, colWidths=[usable - 130, 130])
    totals_table.setStyle(TableStyle([("ALIGN", (1, 0), (1, -1), "RIGHT"),
                                      ("LINEABOVE", (0, 7), (-1, 7), .8, colors.HexColor("#16324f")),
                                      ("BACKGROUND", (0, 7), (-1, 7), colors.HexColor("#edf5f8")),
                                      ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                                      ("TOPPADDING", (0, 0), (-1, -1), 4)]))
    payment_lines = [f"{str(_field(payment, 'method', '')).upper()}: {amt(_field(payment, 'amount', ZERO))}"
                     for payment in payments]
    footer = [p(amount_in_words(_field(sale, "grand_total", ZERO)), label),
              p("Payment type: " + payment_label, small)]
    if payment_lines:
        footer.append(p("Payments: " + "; ".join(payment_lines), small))
    terms = _field(settings, "terms", "")
    if terms:
        footer.extend([Spacer(1, 8), p("Terms and conditions", label), p(terms, small)])
    footer.extend([Spacer(1, 18), p("For " + str(store_name), label), Spacer(1, 18),
                   p("Authorised signatory", small)])

    content = [header_table, Spacer(1, 10), details, Spacer(1, 14), line_table,
               Spacer(1, 12), totals_table, Spacer(1, 12)] + footer
    document.build(content)
    return stream.getvalue()
