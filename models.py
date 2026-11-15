"""Relational store data model.

Amounts are decimal INR values. Quantities are always stored in the product's
base unit. Document rows freeze prices, tax and costs at posting time.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.pool import StaticPool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


MONEY = Numeric(18, 2, asdecimal=True)
QTY = Numeric(18, 6, asdecimal=True)
RATE = Numeric(9, 4, asdecimal=True)
COST = Numeric(18, 6, asdecimal=True)
Base = declarative_base()


class TimestampMixin:
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Branch(Base, TimestampMixin):
    __tablename__ = "branches"
    id = Column(Integer, primary_key=True)
    code = Column(String(24), unique=True, nullable=False)
    name = Column(String(160), nullable=False)
    active = Column(Boolean, nullable=False, default=True)


class StoreSettings(Base, TimestampMixin):
    __tablename__ = "store_settings"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), unique=True, nullable=False)
    store_name = Column(String(180), nullable=False, default="HARITHA ENTERPRISES")
    logo_path = Column(String(500))
    address = Column(Text, nullable=False, default="")
    phone = Column(String(30), nullable=False, default="")
    email = Column(String(200), nullable=False, default="")
    gstin = Column(String(15), nullable=False, default="")
    state = Column(String(100), nullable=False, default="")
    state_code = Column(String(2), nullable=False, default="")
    financial_year_start_month = Column(Integer, nullable=False, default=4)
    invoice_prefix = Column(String(8), nullable=False, default="INV")
    quotation_prefix = Column(String(8), nullable=False, default="QUO")
    tax_inclusive = Column(Boolean, nullable=False, default=False)
    allow_negative_stock = Column(Boolean, nullable=False, default=False)
    default_payment_method = Column(String(24), nullable=False, default="cash")
    default_paper = Column(String(8), nullable=False, default="a4")
    terms = Column(Text, nullable=False, default="")
    receipt_footer = Column(Text, nullable=False, default="")
    currency = Column(String(3), nullable=False, default="INR")
    is_gst_registered = Column(Boolean, nullable=False, default=False)
    e_invoice_applicable = Column(Boolean, nullable=False, default=False)
    branch = relationship("Branch")
    __table_args__ = (
        CheckConstraint("financial_year_start_month >= 1 AND financial_year_start_month <= 12", name="ck_fy_month"),
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    username = Column(String(80), unique=True, nullable=False)
    full_name = Column(String(160), nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(24), nullable=False, default="cashier")
    active = Column(Boolean, nullable=False, default=True)
    last_login_at = Column(DateTime(timezone=True))
    branch = relationship("Branch")
    __table_args__ = (CheckConstraint("role IN ('admin','manager','cashier')", name="ck_user_role"),)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    id = Column(Integer, primary_key=True)
    role = Column(String(24), nullable=False)
    permission = Column(String(80), nullable=False)
    allowed = Column(Boolean, nullable=False, default=True)
    __table_args__ = (UniqueConstraint("role", "permission", name="uq_role_permission"),)


class Category(Base, TimestampMixin):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    parent_id = Column(Integer, ForeignKey("categories.id", ondelete="RESTRICT"))
    active = Column(Boolean, nullable=False, default=True)
    parent = relationship("Category", remote_side=[id])
    __table_args__ = (UniqueConstraint("name", "parent_id", name="uq_category_parent"),)


class Brand(Base, TimestampMixin):
    __tablename__ = "brands"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    active = Column(Boolean, nullable=False, default=True)


class Unit(Base, TimestampMixin):
    __tablename__ = "units"
    id = Column(Integer, primary_key=True)
    name = Column(String(80), unique=True, nullable=False)
    symbol = Column(String(16), unique=True, nullable=False)
    decimal_places = Column(Integer, nullable=False, default=0)
    __table_args__ = (CheckConstraint("decimal_places >= 0 AND decimal_places <= 6", name="ck_unit_places"),)


class Supplier(Base, TimestampMixin):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    name = Column(String(180), nullable=False)
    company_name = Column(String(180), nullable=False, default="")
    phone = Column(String(30), nullable=False, default="")
    email = Column(String(200), nullable=False, default="")
    address = Column(Text, nullable=False, default="")
    gstin = Column(String(15), nullable=False, default="")
    state_code = Column(String(2), nullable=False, default="")
    opening_balance = Column(MONEY, nullable=False, default=Decimal("0"))
    credit_days = Column(Integer, nullable=False, default=0)
    active = Column(Boolean, nullable=False, default=True)
    __table_args__ = (Index("ix_supplier_branch_name", "branch_id", "name"),)


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    name = Column(String(180), nullable=False)
    phone = Column(String(30), nullable=False, default="")
    email = Column(String(200), nullable=False, default="")
    address = Column(Text, nullable=False, default="")
    gstin = Column(String(15), nullable=False, default="")
    state_code = Column(String(2), nullable=False, default="")
    category = Column(String(32), nullable=False, default="retail")
    credit_limit = Column(MONEY, nullable=False, default=Decimal("0"))
    credit_days = Column(Integer, nullable=False, default=0)
    opening_balance = Column(MONEY, nullable=False, default=Decimal("0"))
    active = Column(Boolean, nullable=False, default=True)
    __table_args__ = (Index("ix_customer_branch_name", "branch_id", "name"),)


class Product(Base, TimestampMixin):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    name = Column(String(220), nullable=False)
    sku = Column(String(80), nullable=False)
    barcode = Column(String(120))
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="RESTRICT"))
    subcategory_id = Column(Integer, ForeignKey("categories.id", ondelete="RESTRICT"))
    brand_id = Column(Integer, ForeignKey("brands.id", ondelete="RESTRICT"))
    description = Column(Text, nullable=False, default="")
    base_unit_id = Column(Integer, ForeignKey("units.id", ondelete="RESTRICT"), nullable=False)
    purchase_price = Column(COST, nullable=False, default=Decimal("0"))
    selling_price = Column(MONEY, nullable=False, default=Decimal("0"))
    wholesale_price = Column(MONEY, nullable=False, default=Decimal("0"))
    mrp = Column(MONEY, nullable=False, default=Decimal("0"))
    gst_rate = Column(RATE, nullable=False, default=Decimal("0"))
    hsn_code = Column(String(12), nullable=False, default="")
    opening_stock = Column(QTY, nullable=False, default=Decimal("0"))
    minimum_stock = Column(QTY, nullable=False, default=Decimal("0"))
    preferred_supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="RESTRICT"))
    rack_location = Column(String(120), nullable=False, default="")
    image_path = Column(String(500))
    archived = Column(Boolean, nullable=False, default=False)
    category = relationship("Category", foreign_keys=[category_id])
    subcategory = relationship("Category", foreign_keys=[subcategory_id])
    brand = relationship("Brand")
    base_unit = relationship("Unit")
    preferred_supplier = relationship("Supplier")
    __table_args__ = (
        UniqueConstraint("branch_id", "sku", name="uq_product_branch_sku"),
        UniqueConstraint("branch_id", "barcode", name="uq_product_branch_barcode"),
        CheckConstraint("purchase_price >= 0 AND selling_price >= 0 AND wholesale_price >= 0 AND mrp >= 0", name="ck_product_prices"),
        CheckConstraint("gst_rate >= 0 AND gst_rate <= 100", name="ck_product_gst"),
        Index("ix_product_branch_name", "branch_id", "name"),
    )


class UnitConversion(Base):
    __tablename__ = "unit_conversions"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    unit_id = Column(Integer, ForeignKey("units.id", ondelete="RESTRICT"), nullable=False)
    factor_to_base = Column(QTY, nullable=False)
    __table_args__ = (
        UniqueConstraint("product_id", "unit_id", name="uq_conversion_product_unit"),
        CheckConstraint("factor_to_base > 0", name="ck_conversion_positive"),
    )


class PriceHistory(Base):
    __tablename__ = "price_history"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    kind = Column(String(24), nullable=False)
    old_price = Column(COST, nullable=False)
    new_price = Column(COST, nullable=False)
    reason = Column(Text, nullable=False, default="")
    changed_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        CheckConstraint("kind IN ('purchase','selling','wholesale','mrp')", name="ck_price_history_kind"),
        Index("ix_price_history_product_date", "product_id", "created_at"),
    )


class TaxRate(Base, TimestampMixin):
    __tablename__ = "tax_rates"
    id = Column(Integer, primary_key=True)
    hsn_code = Column(String(12), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="RESTRICT"))
    rate = Column(RATE, nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date)
    notification_reference = Column(String(200), nullable=False, default="")
    __table_args__ = (
        CheckConstraint("rate >= 0 AND rate <= 100", name="ck_tax_rate"),
        Index("ix_tax_rate_lookup", "hsn_code", "effective_from"),
    )


class InventoryBalance(Base, TimestampMixin):
    __tablename__ = "inventory_balances"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    quantity = Column(QTY, nullable=False, default=Decimal("0"))
    weighted_avg_cost = Column(COST, nullable=False, default=Decimal("0"))
    version = Column(Integer, nullable=False, default=0)
    product = relationship("Product")
    __table_args__ = (UniqueConstraint("branch_id", "product_id", name="uq_inventory_branch_product"),)


class StockMovement(Base):
    __tablename__ = "stock_movements"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    movement_type = Column(String(32), nullable=False)
    quantity_delta = Column(QTY, nullable=False)
    value_delta = Column(COST, nullable=False, default=Decimal("0"))
    unit_cost = Column(COST, nullable=False, default=Decimal("0"))
    balance_after = Column(QTY, nullable=False)
    avg_cost_after = Column(COST, nullable=False)
    source_type = Column(String(40), nullable=False)
    source_id = Column(Integer, nullable=False)
    note = Column(Text, nullable=False, default="")
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    product = relationship("Product")
    __table_args__ = (
        CheckConstraint("quantity_delta <> 0", name="ck_movement_nonzero"),
        Index("ix_movement_product_created", "product_id", "created_at"),
        Index("ix_movement_source", "source_type", "source_id"),
    )


class StockAdjustment(Base, TimestampMixin):
    __tablename__ = "stock_adjustments"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    quantity_delta = Column(QTY, nullable=False)
    reason = Column(String(32), nullable=False)
    note = Column(Text, nullable=False, default="")
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    __table_args__ = (CheckConstraint("quantity_delta <> 0", name="ck_adjustment_nonzero"),)


class InvoiceSeries(Base):
    __tablename__ = "invoice_series"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    financial_year = Column(String(9), nullable=False)
    document_type = Column(String(24), nullable=False)
    prefix = Column(String(8), nullable=False)
    next_number = Column(Integer, nullable=False, default=1)
    __table_args__ = (
        UniqueConstraint("branch_id", "financial_year", "document_type", name="uq_series_period_type"),
        CheckConstraint("next_number > 0", name="ck_series_positive"),
    )


class PurchaseOrder(Base, TimestampMixin):
    __tablename__ = "purchase_orders"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False)
    number = Column(String(40), unique=True, nullable=False)
    status = Column(String(24), nullable=False, default="draft")
    order_date = Column(Date, nullable=False)
    expected_date = Column(Date)
    subtotal = Column(MONEY, nullable=False, default=Decimal("0"))
    tax_total = Column(MONEY, nullable=False, default=Decimal("0"))
    grand_total = Column(MONEY, nullable=False, default=Decimal("0"))
    notes = Column(Text, nullable=False, default="")
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    supplier = relationship("Supplier")
    items = relationship("PurchaseOrderItem", back_populates="order", cascade="all, delete-orphan")


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    quantity = Column(QTY, nullable=False)
    unit_id = Column(Integer, ForeignKey("units.id", ondelete="RESTRICT"), nullable=False)
    base_quantity = Column(QTY, nullable=False)
    unit_price = Column(COST, nullable=False)
    discount_percent = Column(RATE, nullable=False, default=Decimal("0"))
    gst_rate = Column(RATE, nullable=False, default=Decimal("0"))
    line_total = Column(MONEY, nullable=False)
    order = relationship("PurchaseOrder", back_populates="items")
    product = relationship("Product")


class Purchase(Base, TimestampMixin):
    __tablename__ = "purchases"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id", ondelete="RESTRICT"), unique=True)
    supplier_invoice_no = Column(String(80), nullable=False, default="")
    purchase_date = Column(Date, nullable=False)
    status = Column(String(24), nullable=False, default="draft")
    subtotal = Column(MONEY, nullable=False, default=Decimal("0"))
    discount_total = Column(MONEY, nullable=False, default=Decimal("0"))
    taxable_total = Column(MONEY, nullable=False, default=Decimal("0"))
    cgst_total = Column(MONEY, nullable=False, default=Decimal("0"))
    sgst_total = Column(MONEY, nullable=False, default=Decimal("0"))
    igst_total = Column(MONEY, nullable=False, default=Decimal("0"))
    grand_total = Column(MONEY, nullable=False, default=Decimal("0"))
    paid_total = Column(MONEY, nullable=False, default=Decimal("0"))
    place_of_supply_code = Column(String(2), nullable=False, default="")
    notes = Column(Text, nullable=False, default="")
    confirmed_at = Column(DateTime(timezone=True))
    confirmed_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    supplier = relationship("Supplier")
    items = relationship("PurchaseItem", back_populates="purchase", cascade="all, delete-orphan")
    payments = relationship("PurchasePayment", back_populates="purchase")
    __table_args__ = (
        Index("ix_purchase_branch_date", "branch_id", "purchase_date"),
        Index("uq_supplier_invoice", "branch_id", "supplier_id", "supplier_invoice_no",
              unique=True, sqlite_where=(supplier_invoice_no != ""),
              postgresql_where=(supplier_invoice_no != "")),
    )


class PurchaseItem(Base):
    __tablename__ = "purchase_items"
    id = Column(Integer, primary_key=True)
    purchase_id = Column(Integer, ForeignKey("purchases.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    quantity = Column(QTY, nullable=False)
    unit_id = Column(Integer, ForeignKey("units.id", ondelete="RESTRICT"), nullable=False)
    conversion_factor = Column(QTY, nullable=False, default=Decimal("1"))
    base_quantity = Column(QTY, nullable=False)
    unit_price = Column(COST, nullable=False)
    discount = Column(MONEY, nullable=False, default=Decimal("0"))
    taxable_value = Column(MONEY, nullable=False)
    gst_rate = Column(RATE, nullable=False)
    cgst = Column(MONEY, nullable=False, default=Decimal("0"))
    sgst = Column(MONEY, nullable=False, default=Decimal("0"))
    igst = Column(MONEY, nullable=False, default=Decimal("0"))
    line_total = Column(MONEY, nullable=False)
    hsn_code = Column(String(12), nullable=False, default="")
    purchase = relationship("Purchase", back_populates="items")
    product = relationship("Product")
    __table_args__ = (CheckConstraint("quantity > 0 AND base_quantity > 0 AND unit_price >= 0", name="ck_purchase_item_positive"),)


class PurchasePayment(Base):
    __tablename__ = "purchase_payments"
    id = Column(Integer, primary_key=True)
    purchase_id = Column(Integer, ForeignKey("purchases.id", ondelete="RESTRICT"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False)
    amount = Column(MONEY, nullable=False)
    method = Column(String(32), nullable=False)
    reference = Column(String(120), nullable=False, default="")
    paid_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    purchase = relationship("Purchase", back_populates="payments")
    __table_args__ = (CheckConstraint("amount > 0", name="ck_purchase_payment_positive"),)


class PurchaseReturn(Base, TimestampMixin):
    __tablename__ = "purchase_returns"
    id = Column(Integer, primary_key=True)
    purchase_id = Column(Integer, ForeignKey("purchases.id", ondelete="RESTRICT"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False)
    number = Column(String(40), unique=True, nullable=False)
    status = Column(String(24), nullable=False, default="confirmed")
    return_date = Column(Date, nullable=False)
    taxable_total = Column(MONEY, nullable=False, default=Decimal("0"))
    tax_total = Column(MONEY, nullable=False, default=Decimal("0"))
    grand_total = Column(MONEY, nullable=False, default=Decimal("0"))
    reason = Column(Text, nullable=False, default="")
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    items = relationship("PurchaseReturnItem", back_populates="return_doc", cascade="all, delete-orphan")


class PurchaseReturnItem(Base):
    __tablename__ = "purchase_return_items"
    id = Column(Integer, primary_key=True)
    return_id = Column(Integer, ForeignKey("purchase_returns.id", ondelete="CASCADE"), nullable=False)
    purchase_item_id = Column(Integer, ForeignKey("purchase_items.id", ondelete="RESTRICT"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    base_quantity = Column(QTY, nullable=False)
    unit_cost = Column(COST, nullable=False)
    taxable_value = Column(MONEY, nullable=False)
    cgst = Column(MONEY, nullable=False, default=Decimal("0"))
    sgst = Column(MONEY, nullable=False, default=Decimal("0"))
    igst = Column(MONEY, nullable=False, default=Decimal("0"))
    tax_total = Column(MONEY, nullable=False)
    line_total = Column(MONEY, nullable=False)
    return_doc = relationship("PurchaseReturn", back_populates="items")
    __table_args__ = (CheckConstraint("base_quantity > 0", name="ck_purchase_return_qty"),)


class Quotation(Base, TimestampMixin):
    __tablename__ = "quotations"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    number = Column(String(40), unique=True, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="RESTRICT"))
    customer_name = Column(String(180), nullable=False, default="")
    status = Column(String(24), nullable=False, default="draft")
    quote_date = Column(Date, nullable=False)
    valid_until = Column(Date)
    subtotal = Column(MONEY, nullable=False, default=Decimal("0"))
    discount_total = Column(MONEY, nullable=False, default=Decimal("0"))
    tax_total = Column(MONEY, nullable=False, default=Decimal("0"))
    grand_total = Column(MONEY, nullable=False, default=Decimal("0"))
    terms = Column(Text, nullable=False, default="")
    notes = Column(Text, nullable=False, default="")
    converted_sale_id = Column(Integer, ForeignKey("sales.id", ondelete="RESTRICT"), unique=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    customer = relationship("Customer")
    items = relationship("QuotationItem", back_populates="quotation", cascade="all, delete-orphan")


class QuotationItem(Base):
    __tablename__ = "quotation_items"
    id = Column(Integer, primary_key=True)
    quotation_id = Column(Integer, ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    quantity = Column(QTY, nullable=False)
    unit_id = Column(Integer, ForeignKey("units.id", ondelete="RESTRICT"), nullable=False)
    conversion_factor = Column(QTY, nullable=False, default=Decimal("1"))
    unit_price = Column(MONEY, nullable=False)
    discount_percent = Column(RATE, nullable=False, default=Decimal("0"))
    discount = Column(MONEY, nullable=False, default=Decimal("0"))
    gst_rate = Column(RATE, nullable=False)
    taxable_value = Column(MONEY, nullable=False)
    tax_total = Column(MONEY, nullable=False)
    line_total = Column(MONEY, nullable=False)
    quotation = relationship("Quotation", back_populates="items")
    product = relationship("Product")


class HeldBill(Base, TimestampMixin):
    __tablename__ = "held_bills"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    label = Column(String(120), nullable=False, default="")
    cart_json = Column(JSON, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="RESTRICT"))
    __table_args__ = (Index("ix_held_bill_user", "branch_id", "user_id"),)


class DeliveryChallan(Base, TimestampMixin):
    __tablename__ = "delivery_challans"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    sale_id = Column(Integer, ForeignKey("sales.id", ondelete="RESTRICT"), nullable=False)
    number = Column(String(40), unique=True, nullable=False)
    challan_date = Column(Date, nullable=False)
    place_of_supply_code = Column(String(2), nullable=False)
    customer_name = Column(String(180), nullable=False)
    customer_address = Column(Text, nullable=False, default="")
    notes = Column(Text, nullable=False, default="")
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    sale = relationship("Sale")
    __table_args__ = (UniqueConstraint("sale_id", name="uq_challan_sale"),)


class Sale(Base, TimestampMixin):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    invoice_no = Column(String(40), unique=True, nullable=False)
    invoice_type = Column(String(24), nullable=False, default="tax_invoice")
    payment_type = Column(String(24), nullable=False)
    financial_year = Column(String(9), nullable=False)
    sale_date = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="RESTRICT"))
    customer_name = Column(String(180), nullable=False, default="Walk-in Customer")
    customer_address = Column(Text, nullable=False, default="")
    delivery_address = Column(Text, nullable=False, default="")
    customer_gstin = Column(String(15), nullable=False, default="")
    customer_state_code = Column(String(2), nullable=False, default="")
    place_of_supply_code = Column(String(2), nullable=False, default="")
    status = Column(String(24), nullable=False, default="confirmed")
    subtotal = Column(MONEY, nullable=False, default=Decimal("0"))
    discount_total = Column(MONEY, nullable=False, default=Decimal("0"))
    taxable_total = Column(MONEY, nullable=False, default=Decimal("0"))
    cgst_total = Column(MONEY, nullable=False, default=Decimal("0"))
    sgst_total = Column(MONEY, nullable=False, default=Decimal("0"))
    igst_total = Column(MONEY, nullable=False, default=Decimal("0"))
    round_off = Column(MONEY, nullable=False, default=Decimal("0"))
    grand_total = Column(MONEY, nullable=False, default=Decimal("0"))
    paid_total = Column(MONEY, nullable=False, default=Decimal("0"))
    cost_of_goods = Column(COST, nullable=False, default=Decimal("0"))
    quotation_id = Column(Integer, ForeignKey("quotations.id", ondelete="RESTRICT"), unique=True)
    idempotency_key = Column(String(120), unique=True)
    cancellation_reason = Column(Text)
    cancelled_at = Column(DateTime(timezone=True))
    cancelled_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    customer = relationship("Customer")
    items = relationship("SaleItem", back_populates="sale", cascade="all, delete-orphan")
    payments = relationship("SalePayment", back_populates="sale")
    __table_args__ = (
        Index("ix_sale_branch_date", "branch_id", "sale_date"),
        Index("ix_sale_customer_date", "customer_id", "sale_date"),
    )


class SaleItem(Base):
    __tablename__ = "sale_items"
    id = Column(Integer, primary_key=True)
    sale_id = Column(Integer, ForeignKey("sales.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    description = Column(String(220), nullable=False)
    hsn_code = Column(String(12), nullable=False, default="")
    quantity = Column(QTY, nullable=False)
    unit_id = Column(Integer, ForeignKey("units.id", ondelete="RESTRICT"), nullable=False)
    conversion_factor = Column(QTY, nullable=False, default=Decimal("1"))
    base_quantity = Column(QTY, nullable=False)
    unit_price = Column(MONEY, nullable=False)
    discount = Column(MONEY, nullable=False, default=Decimal("0"))
    taxable_value = Column(MONEY, nullable=False)
    gst_rate = Column(RATE, nullable=False)
    cgst = Column(MONEY, nullable=False, default=Decimal("0"))
    sgst = Column(MONEY, nullable=False, default=Decimal("0"))
    igst = Column(MONEY, nullable=False, default=Decimal("0"))
    line_total = Column(MONEY, nullable=False)
    unit_cost = Column(COST, nullable=False)
    cost_of_goods = Column(COST, nullable=False)
    sale = relationship("Sale", back_populates="items")
    product = relationship("Product")
    __table_args__ = (CheckConstraint("quantity > 0 AND base_quantity > 0 AND unit_price >= 0", name="ck_sale_item_positive"),)


class SalePayment(Base):
    __tablename__ = "sale_payments"
    id = Column(Integer, primary_key=True)
    sale_id = Column(Integer, ForeignKey("sales.id", ondelete="RESTRICT"), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="RESTRICT"))
    amount = Column(MONEY, nullable=False)
    method = Column(String(32), nullable=False)
    reference = Column(String(120), nullable=False, default="")
    paid_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    sale = relationship("Sale", back_populates="payments")
    __table_args__ = (CheckConstraint("amount > 0", name="ck_sale_payment_positive"),)


class SaleReturn(Base, TimestampMixin):
    __tablename__ = "sale_returns"
    id = Column(Integer, primary_key=True)
    sale_id = Column(Integer, ForeignKey("sales.id", ondelete="RESTRICT"), nullable=False)
    number = Column(String(40), unique=True, nullable=False)
    status = Column(String(24), nullable=False, default="confirmed")
    return_date = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    taxable_total = Column(MONEY, nullable=False, default=Decimal("0"))
    cgst_total = Column(MONEY, nullable=False, default=Decimal("0"))
    sgst_total = Column(MONEY, nullable=False, default=Decimal("0"))
    igst_total = Column(MONEY, nullable=False, default=Decimal("0"))
    grand_total = Column(MONEY, nullable=False, default=Decimal("0"))
    refunded_amount = Column(MONEY, nullable=False, default=Decimal("0"))
    refund_method = Column(String(32), nullable=False, default="cash")
    reason = Column(Text, nullable=False, default="")
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    items = relationship("SaleReturnItem", back_populates="return_doc", cascade="all, delete-orphan")


class SaleReturnItem(Base):
    __tablename__ = "sale_return_items"
    id = Column(Integer, primary_key=True)
    return_id = Column(Integer, ForeignKey("sale_returns.id", ondelete="CASCADE"), nullable=False)
    sale_item_id = Column(Integer, ForeignKey("sale_items.id", ondelete="RESTRICT"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    base_quantity = Column(QTY, nullable=False)
    unit_cost = Column(COST, nullable=False)
    taxable_value = Column(MONEY, nullable=False)
    cgst = Column(MONEY, nullable=False, default=Decimal("0"))
    sgst = Column(MONEY, nullable=False, default=Decimal("0"))
    igst = Column(MONEY, nullable=False, default=Decimal("0"))
    line_total = Column(MONEY, nullable=False)
    return_doc = relationship("SaleReturn", back_populates="items")
    __table_args__ = (CheckConstraint("base_quantity > 0", name="ck_sale_return_qty"),)


class PartyPayment(Base):
    """Unallocated or allocated direct customer/supplier payment."""
    __tablename__ = "party_payments"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    party_type = Column(String(12), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="RESTRICT"))
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="RESTRICT"))
    amount = Column(MONEY, nullable=False)
    method = Column(String(32), nullable=False)
    reference = Column(String(120), nullable=False, default="")
    paid_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    note = Column(Text, nullable=False, default="")
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_party_payment_positive"),
        CheckConstraint("(party_type = 'customer' AND customer_id IS NOT NULL AND supplier_id IS NULL) OR (party_type = 'supplier' AND supplier_id IS NOT NULL AND customer_id IS NULL)", name="ck_party_payment_party"),
    )


class PaymentAllocation(Base):
    """Application of a direct party payment to one invoice."""
    __tablename__ = "payment_allocations"
    id = Column(Integer, primary_key=True)
    party_payment_id = Column(Integer, ForeignKey("party_payments.id", ondelete="RESTRICT"), nullable=False)
    sale_id = Column(Integer, ForeignKey("sales.id", ondelete="RESTRICT"))
    purchase_id = Column(Integer, ForeignKey("purchases.id", ondelete="RESTRICT"))
    amount = Column(MONEY, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_allocation_positive"),
        CheckConstraint("(sale_id IS NOT NULL AND purchase_id IS NULL) OR (sale_id IS NULL AND purchase_id IS NOT NULL)", name="ck_payment_allocation_one_document"),
        Index("ix_allocation_purchase", "purchase_id"),
        Index("ix_allocation_sale", "sale_id"),
    )


class LedgerEntry(Base):
    """Positive amount increases receivable/payable; negative reduces it."""
    __tablename__ = "ledger_entries"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    party_type = Column(String(12), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="RESTRICT"))
    supplier_id = Column(Integer, ForeignKey("suppliers.id", ondelete="RESTRICT"))
    amount = Column(MONEY, nullable=False)
    entry_type = Column(String(32), nullable=False)
    source_type = Column(String(40), nullable=False)
    source_id = Column(Integer, nullable=False)
    due_date = Column(Date)
    description = Column(Text, nullable=False, default="")
    occurred_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_ledger_nonzero"),
        CheckConstraint("(party_type = 'customer' AND customer_id IS NOT NULL AND supplier_id IS NULL) OR (party_type = 'supplier' AND supplier_id IS NOT NULL AND customer_id IS NULL)", name="ck_ledger_party"),
        Index("ix_ledger_customer_date", "customer_id", "occurred_at"),
        Index("ix_ledger_supplier_date", "supplier_id", "occurred_at"),
        Index("ix_ledger_source", "source_type", "source_id"),
    )


class Expense(Base, TimestampMixin):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    expense_date = Column(Date, nullable=False)
    category = Column(String(60), nullable=False)
    amount = Column(MONEY, nullable=False)
    payment_method = Column(String(32), nullable=False)
    description = Column(Text, nullable=False, default="")
    receipt_path = Column(String(500))
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_expense_positive"),
        Index("ix_expense_branch_date", "branch_id", "expense_date"),
    )


class Attachment(Base):
    __tablename__ = "attachments"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    owner_type = Column(String(40), nullable=False)
    owner_id = Column(Integer, nullable=False)
    original_name = Column(String(255), nullable=False)
    stored_path = Column(String(500), nullable=False)
    mime_type = Column(String(120), nullable=False)
    sha256 = Column(String(64), nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_attachment_owner", "owner_type", "owner_id"),)


class ShareLink(Base):
    __tablename__ = "share_links"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    entity_type = Column(String(24), nullable=False)
    entity_id = Column(Integer, nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True))
    created_by = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_share_entity", "entity_type", "entity_id"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="RESTRICT"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"))
    action = Column(String(100), nullable=False)
    entity_type = Column(String(50), nullable=False)
    entity_id = Column(Integer)
    before_json = Column(JSON)
    after_json = Column(JSON)
    ip_address = Column(String(45))
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_audit_branch_created", "branch_id", "created_at"),)


@event.listens_for(StockMovement, "before_update")
@event.listens_for(StockMovement, "before_delete")
@event.listens_for(LedgerEntry, "before_update")
@event.listens_for(LedgerEntry, "before_delete")
@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def reject_immutable_changes(mapper, connection, target):
    raise ValueError(f"{type(target).__name__} is immutable; post a reversal instead")


def make_engine(url: str | None = None):
    url = url or os.environ.get("DATABASE_URL", "sqlite:///./hardware_store.db")
    options = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            options["poolclass"] = StaticPool
    result = create_engine(url, **options)
    if url.startswith("sqlite"):
        @event.listens_for(result, "connect")
        def enable_foreign_keys(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return result


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db(bind=None):
    target = bind or engine
    Base.metadata.create_all(target)

    # create_all does not add columns to an existing table. Keep prior invoices
    # readable when an established store is upgraded to explicit payment types.
    with target.begin() as connection:
        if "payment_type" not in {column["name"] for column in inspect(connection).get_columns("sales")}:
            connection.execute(text(
                "ALTER TABLE sales ADD COLUMN payment_type VARCHAR(24) NOT NULL DEFAULT 'credit'"
            ))
            connection.execute(text("""
                UPDATE sales SET payment_type = CASE
                    WHEN (SELECT COUNT(DISTINCT method) FROM sale_payments
                          WHERE sale_id = sales.id AND method <> 'credit') > 1 THEN 'split'
                    WHEN (SELECT COUNT(*) FROM sale_payments
                          WHERE sale_id = sales.id AND method <> 'credit') > 0
                        THEN (SELECT MIN(method) FROM sale_payments
                              WHERE sale_id = sales.id AND method <> 'credit')
                    ELSE 'credit'
                END
            """))
            if connection.dialect.name == "postgresql":
                connection.execute(text("ALTER TABLE sales ALTER COLUMN payment_type DROP DEFAULT"))


def bootstrap_store(session):
    """Create only non-secret baseline records; administrator is provisioned separately."""
    branch = session.query(Branch).filter_by(code="MAIN").one_or_none()
    if branch is None:
        branch = Branch(code="MAIN", name="Main Branch")
        session.add(branch)
        session.flush()
    if session.query(StoreSettings).filter_by(branch_id=branch.id).one_or_none() is None:
        session.add(StoreSettings(branch_id=branch.id))
    for name, symbol, places in (
        ("Pieces", "pcs", 0), ("Boxes", "box", 0), ("Packets", "pkt", 0),
        ("Kilograms", "kg", 3), ("Grams", "g", 3), ("Metres", "m", 3),
        ("Feet", "ft", 3), ("Litres", "L", 3), ("Bags", "bag", 0),
        ("Sets", "set", 0), ("Rolls", "roll", 0),
    ):
        if session.query(Unit).filter_by(symbol=symbol).one_or_none() is None:
            session.add(Unit(name=name, symbol=symbol, decimal_places=places))
    session.flush()
    return branch
