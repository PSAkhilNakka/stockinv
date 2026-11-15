"""Shared posting primitives. Callers commit the entire document as one transaction."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select, func, text

from models import (
    AuditLog, InventoryBalance, InvoiceSeries, LedgerEntry, Product,
    StockMovement, Unit, UnitConversion,
)
from tax import dec, financial_year, money, quantity


def require_positive(value, label):
    result = dec(value)
    if result <= 0:
        raise ValueError(f"{label} must be greater than zero")
    return result


def get_product(db, branch_id, product_id, active=True):
    product = db.get(Product, int(product_id))
    if product is None or product.branch_id != branch_id or (active and product.archived):
        raise ValueError("Product not found or archived")
    return product


def unit_factor(db, product, unit):
    """Resolve an id, symbol or name to (Unit, base-unit multiplier)."""
    if unit is None or unit == "":
        target = db.get(Unit, product.base_unit_id)
    elif isinstance(unit, int) or str(unit).isdigit():
        target = db.get(Unit, int(unit))
    else:
        target = db.execute(select(Unit).where((Unit.symbol == str(unit)) | (Unit.name == str(unit)))).scalar_one_or_none()
    if target is None:
        raise ValueError("Unknown unit")
    if target.id == product.base_unit_id:
        return target, Decimal("1")
    conversion = db.execute(select(UnitConversion).where(
        UnitConversion.product_id == product.id, UnitConversion.unit_id == target.id
    )).scalar_one_or_none()
    if conversion is None:
        raise ValueError(f"No conversion from {target.symbol} for {product.name}")
    return target, dec(conversion.factor_to_base)


def base_quantity(db, product, amount, unit):
    selected, factor = unit_factor(db, product, unit)
    raw_qty = require_positive(amount, "Quantity")
    unit_step = Decimal(1).scaleb(-selected.decimal_places)
    if raw_qty != raw_qty.quantize(unit_step):
        raise ValueError(f"{selected.name} permits {selected.decimal_places} decimal places")
    qty = quantity(raw_qty)
    if qty * factor != quantity(qty * factor):
        raise ValueError("Converted quantity exceeds supported precision")
    base = quantity(qty * factor)
    if base <= 0:
        raise ValueError("Converted quantity must be greater than zero")
    return selected, factor, base


def balance_locked(db, branch_id, product_id):
    row = db.execute(select(InventoryBalance).where(
        InventoryBalance.branch_id == branch_id,
        InventoryBalance.product_id == product_id,
    ).with_for_update()).scalar_one_or_none()
    if row is None:
        row = InventoryBalance(branch_id=branch_id, product_id=product_id,
                               quantity=Decimal("0"), weighted_avg_cost=Decimal("0"))
        db.add(row)
        db.flush()
    return row


def stock_post(db, branch_id, product_id, qty_delta, unit_cost, movement_type,
               source_type, source_id, user_id, note="", allow_negative=False):
    qty_delta, unit_cost = quantity(qty_delta), dec(unit_cost)
    if qty_delta == 0:
        raise ValueError("Stock movement cannot be zero")
    if unit_cost < 0:
        raise ValueError("Stock cost cannot be negative")
    balance = balance_locked(db, branch_id, product_id)
    old_qty, old_cost = dec(balance.quantity), dec(balance.weighted_avg_cost)
    new_qty = quantity(old_qty + qty_delta)
    if new_qty < 0 and not allow_negative:
        raise ValueError("Insufficient stock")
    if qty_delta > 0:
        new_cost = ((old_qty * old_cost + qty_delta * unit_cost) / new_qty) if new_qty > 0 else unit_cost
    else:
        new_cost = old_cost
    new_cost = new_cost.quantize(Decimal("0.000001"))
    balance.quantity = new_qty
    balance.weighted_avg_cost = new_cost
    balance.version += 1
    movement = StockMovement(
        branch_id=branch_id, product_id=product_id, movement_type=movement_type,
        quantity_delta=qty_delta, value_delta=qty_delta * unit_cost,
        unit_cost=unit_cost, balance_after=new_qty, avg_cost_after=new_cost,
        source_type=source_type, source_id=source_id, note=note, created_by=user_id,
    )
    db.add(movement)
    db.flush()
    return balance, movement


def next_document_no(db, branch_id, settings, document_type, when=None):
    when = when or date.today()
    fy = financial_year(when, settings.financial_year_start_month)
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                   {"key": f"invoice:{branch_id}:{fy}:{document_type}"})
    prefix = ("DC" if document_type == "delivery_challan" else
              settings.invoice_prefix if document_type in ("sale", "sale_return", "purchase_return")
              else settings.quotation_prefix)
    suffix = {"sale": "", "sale_return": "C", "purchase_return": "D", "quotation": "Q", "purchase_order": "P"}.get(document_type, "")
    series = db.execute(select(InvoiceSeries).where(
        InvoiceSeries.branch_id == branch_id,
        InvoiceSeries.financial_year == fy,
        InvoiceSeries.document_type == document_type,
    ).with_for_update()).scalar_one_or_none()
    if series is None:
        series = InvoiceSeries(branch_id=branch_id, financial_year=fy,
                               document_type=document_type, prefix=prefix, next_number=1)
        db.add(series)
        db.flush()
    number = f"{series.prefix}{suffix}/{fy}/{series.next_number:05d}"
    if document_type in ("sale", "sale_return", "purchase_return") and len(number) > 16:
        raise ValueError("GST document number exceeds 16 characters; shorten invoice prefix")
    series.next_number += 1
    return number, fy


def ledger_post(db, branch_id, party_type, party_id, amount, entry_type,
                source_type, source_id, user_id, description="", due_date=None):
    amount = money(amount)
    if amount == 0:
        return None
    item = LedgerEntry(branch_id=branch_id, party_type=party_type,
                       customer_id=party_id if party_type == "customer" else None,
                       supplier_id=party_id if party_type == "supplier" else None,
                       amount=amount, entry_type=entry_type,
                       source_type=source_type, source_id=source_id,
                       created_by=user_id, description=description, due_date=due_date)
    db.add(item)
    return item


def party_balance(db, party_type, party):
    column = LedgerEntry.customer_id if party_type == "customer" else LedgerEntry.supplier_id
    posted = db.execute(select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(column == party.id)).scalar_one()
    return money(dec(party.opening_balance) + dec(posted))


def audit(db, branch_id, user_id, action, entity_type, entity_id=None, before=None, after=None, ip=None):
    db.add(AuditLog(branch_id=branch_id, user_id=user_id, action=action,
                    entity_type=entity_type, entity_id=entity_id,
                    before_json=before, after_json=after, ip_address=ip))
