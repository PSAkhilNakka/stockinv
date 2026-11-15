"""HARITHA ENTERPRISES web application and JSON API."""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for
from sqlalchemy import inspect, or_, select, func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import models as m
from domain import audit, base_quantity, get_product, ledger_post, next_document_no, party_balance, require_positive, stock_post
from security import can, csrf_guard, requires
from selling import checkout, create_quotation, preview_sale, replace_quotation_items, return_sale
from storage import resolve_local_upload, upload_reference
from tax import dec, money, quantity


app = Flask(__name__)
if os.environ.get("DATABASE_URL", "").startswith("postgresql") and not os.environ.get("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY is required for PostgreSQL deployments")
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=12 * 1024 * 1024)
m.init_db()
with m.SessionLocal() as setup_db:
    m.bootstrap_store(setup_db)
    setup_db.commit()


def as_json(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: as_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [as_json(item) for item in value]
    return value


def row_json(row, **extras):
    data = {column.key: as_json(getattr(row, column.key)) for column in inspect(row).mapper.column_attrs}
    data.update(as_json(extras))
    return data


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def settings_row():
    return g.db.execute(select(m.StoreSettings).where(m.StoreSettings.branch_id == g.user.branch_id)).scalar_one()


def validate_gstin(gstin, state_code=""):
    gstin = str(gstin or "").strip().upper()
    if not gstin:
        return ""
    if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]", gstin):
        raise ValueError("GSTIN must have the 15-character Indian GSTIN format")
    if state_code and gstin[:2] != str(state_code).zfill(2):
        raise ValueError("GSTIN state code does not match the selected state")
    return gstin


def current_user():
    if not g.user:
        return None
    return {"id": g.user.id, "username": g.user.username, "full_name": g.user.full_name,
            "role": g.user.role, "branch_id": g.user.branch_id}


@app.before_request
def open_request_db():
    g.db = m.SessionLocal()
    g.user = g.db.get(m.User, session.get("user_id")) if session.get("user_id") else None
    if g.user and not g.user.active:
        session.clear()
        g.user = None
    result = csrf_guard()
    if result is not None:
        return result


@app.teardown_request
def close_request_db(error):
    db = getattr(g, "db", None)
    if db:
        if error:
            db.rollback()
        db.close()


@app.errorhandler(ValueError)
def invalid_value(error):
    g.db.rollback()
    return jsonify(error=str(error)), 400


@app.errorhandler(IntegrityError)
def conflict(error):
    g.db.rollback()
    return jsonify(error="A duplicate or conflicting record already exists"), 409


@app.route("/")
def home():
    if not g.user:
        return redirect(url_for("login"))
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        session["login_csrf"] = secrets.token_urlsafe(32)
        return render_template("login.html", error=None, csrf_token=session["login_csrf"])
    if not session.get("login_csrf") or request.form.get("csrf_token") != session["login_csrf"]:
        return render_template("login.html", error="Please reload the sign-in page and try again",
                               csrf_token=session.get("login_csrf", "")), 403
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = g.db.execute(select(m.User).where(m.User.username == username)).scalar_one_or_none()
    if not user or not user.active or not check_password_hash(user.password_hash, password):
        return render_template("login.html", error="Invalid username or password",
                               csrf_token=session["login_csrf"]), 401
    session.clear()
    session["user_id"] = user.id
    session["csrf_token"] = secrets.token_urlsafe(32)
    user.last_login_at = datetime.now(timezone.utc)
    audit(g.db, user.branch_id, user.id, "auth.login", "user", user.id, ip=request.remote_addr)
    g.db.commit()
    return redirect(url_for("home"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/api/bootstrap")
@requires("products.read")
def bootstrap():
    settings = settings_row()
    return jsonify({
        "user": current_user(), "csrf_token": session["csrf_token"],
        "settings": row_json(settings),
        "categories": [row_json(c) for c in g.db.scalars(select(m.Category).where(m.Category.active == True).order_by(m.Category.name))],
        "brands": [row_json(b) for b in g.db.scalars(select(m.Brand).where(m.Brand.active == True).order_by(m.Brand.name))],
        "units": [row_json(u) for u in g.db.scalars(select(m.Unit).order_by(m.Unit.name))],
        "products": list_products_data(limit=5000),
        "customers": [contact_json(c, "customer") for c in g.db.scalars(select(m.Customer).where(m.Customer.branch_id == g.user.branch_id, m.Customer.active == True).order_by(m.Customer.name))],
        "suppliers": [contact_json(s, "supplier") for s in g.db.scalars(select(m.Supplier).where(m.Supplier.branch_id == g.user.branch_id, m.Supplier.active == True).order_by(m.Supplier.name))] if can(g.user, "purchases.read") else [],
    })


def product_json(product, balance=None):
    if balance is None:
        balance = g.db.execute(select(m.InventoryBalance).where(
            m.InventoryBalance.branch_id == product.branch_id,
            m.InventoryBalance.product_id == product.id,
        )).scalar_one_or_none()
    unit = g.db.get(m.Unit, product.base_unit_id)
    data = row_json(product, current_stock=balance.quantity if balance else 0,
                    weighted_avg_cost=balance.weighted_avg_cost if balance else 0,
                    unit=unit.symbol if unit else "",
                    category_name=product.category.name if product.category else "",
                    brand=product.brand.name if product.brand else "")
    conversions = g.db.scalars(select(m.UnitConversion).where(m.UnitConversion.product_id == product.id)).all()
    data["conversions"] = [row_json(c, unit=g.db.get(m.Unit, c.unit_id).symbol,
                                     symbol=g.db.get(m.Unit, c.unit_id).symbol) for c in conversions]
    if not can(g.user, "purchases.read") and g.user.role == "cashier":
        data.pop("purchase_price", None)
        data.pop("weighted_avg_cost", None)
    return data


def list_products_data(limit=5000):
    query = select(m.Product).where(m.Product.branch_id == g.user.branch_id)
    if request.args.get("include_archived") != "1":
        query = query.where(m.Product.archived == False)
    term = (request.args.get("search") or "").strip()
    if term:
        like = f"%{term}%"
        query = query.outerjoin(m.Brand).where(or_(m.Product.name.ilike(like), m.Product.sku.ilike(like),
                                                   m.Product.barcode.ilike(like), m.Brand.name.ilike(like)))
    if request.args.get("category_id"):
        query = query.where(m.Product.category_id == int(request.args["category_id"]))
    if request.args.get("brand_id"):
        query = query.where(m.Product.brand_id == int(request.args["brand_id"]))
    products = g.db.scalars(query.order_by(m.Product.name).limit(limit)).all()
    return [product_json(item) for item in products]


@app.get("/api/products")
@requires("products.read")
def list_products():
    return jsonify(list_products_data())


def resolve_brand(name_or_id):
    if not name_or_id:
        return None
    if str(name_or_id).isdigit():
        brand = g.db.get(m.Brand, int(name_or_id))
        if not brand:
            raise ValueError("Brand not found")
        return brand.id
    name = str(name_or_id).strip()
    brand = g.db.execute(select(m.Brand).where(func.lower(m.Brand.name) == name.lower())).scalar_one_or_none()
    if not brand:
        brand = m.Brand(name=name)
        g.db.add(brand)
        g.db.flush()
    return brand.id


def resolve_unit(name_or_id):
    if not name_or_id:
        raise ValueError("Select a base unit")
    if str(name_or_id).isdigit():
        unit = g.db.get(m.Unit, int(name_or_id))
    else:
        unit = g.db.execute(select(m.Unit).where(or_(m.Unit.symbol == str(name_or_id), m.Unit.name == str(name_or_id)))).scalar_one_or_none()
    if not unit:
        raise ValueError("Unit not found")
    return unit.id


def upsert_product(data, product=None):
    is_new = product is None
    if is_new:
        product = m.Product(branch_id=g.user.branch_id)
        g.db.add(product)
    before = row_json(product) if not is_new else None
    for field in ("name", "sku", "barcode", "category_id", "subcategory_id", "description",
                  "purchase_price", "selling_price", "wholesale_price", "mrp", "gst_rate", "hsn_code",
                  "preferred_supplier_id", "rack_location", "image_path", "minimum_stock"):
        key = "min_stock" if field == "minimum_stock" and "min_stock" in data else field
        if key in data:
            setattr(product, field, data[key] or None if field in ("category_id", "subcategory_id", "preferred_supplier_id", "barcode", "image_path") else data[key])
    if "brand" in data or "brand_id" in data:
        product.brand_id = resolve_brand(data.get("brand_id") or data.get("brand"))
    if "base_unit_id" in data or "unit" in data:
        product.base_unit_id = resolve_unit(data.get("base_unit_id") or data.get("unit"))
    if not product.name or not product.sku or not product.base_unit_id:
        raise ValueError("Product name, SKU and base unit are required")
    for field in ("purchase_price", "selling_price", "wholesale_price", "mrp", "gst_rate", "minimum_stock"):
        if dec(getattr(product, field) or 0) < 0:
            raise ValueError(f"{field} cannot be negative")
    if dec(product.gst_rate or 0) > 100:
        raise ValueError("GST percentage cannot exceed 100")
    if "archived" in data:
        product.archived = bool(data["archived"])
    g.db.flush()
    if is_new:
        opening = quantity(data.get("opening_stock") or 0)
        if opening < 0:
            raise ValueError("Opening stock cannot be negative")
        product.opening_stock = opening
        if opening:
            stock_post(g.db, g.user.branch_id, product.id, opening, dec(product.purchase_price),
                       "opening", "product", product.id, g.user.id)
        else:
            g.db.add(m.InventoryBalance(branch_id=g.user.branch_id, product_id=product.id,
                                        quantity=Decimal("0"), weighted_avg_cost=dec(product.purchase_price)))
            g.db.flush()
    else:
        for kind in ("purchase_price", "selling_price", "wholesale_price", "mrp"):
            if dec(before[kind]) != dec(getattr(product, kind)):
                kind_name = {"purchase_price": "purchase", "selling_price": "selling",
                             "wholesale_price": "wholesale", "mrp": "mrp"}[kind]
                g.db.add(m.PriceHistory(product_id=product.id, kind=kind_name,
                                        old_price=before[kind], new_price=getattr(product, kind),
                                        reason=data.get("price_change_reason") or "", changed_by=g.user.id))
    audit(g.db, g.user.branch_id, g.user.id, "product.create" if is_new else "product.update",
          "product", product.id, before=before, after=row_json(product))
    return product


@app.post("/api/products")
@requires("products.write")
def create_product():
    product = upsert_product(body())
    g.db.commit()
    return jsonify(product_json(product)), 201


@app.patch("/api/products/<int:product_id>")
@requires("products.write")
def update_product(product_id):
    product = get_product(g.db, g.user.branch_id, product_id, active=False)
    updated = upsert_product(body(), product)
    g.db.commit()
    return jsonify(product_json(updated))


@app.post("/api/products/<int:product_id>/adjust")
@requires("inventory.adjust")
def adjust_stock(product_id):
    data = body()
    product = get_product(g.db, g.user.branch_id, product_id)
    delta = quantity(data.get("quantity_delta"))
    if not delta:
        raise ValueError("Stock adjustment quantity cannot be zero")
    reason = data.get("reason") or "manual"
    adjustment = m.StockAdjustment(branch_id=g.user.branch_id, product_id=product.id,
                                   quantity_delta=delta, reason=reason,
                                   note=data.get("note") or "", created_by=g.user.id)
    g.db.add(adjustment)
    g.db.flush()
    balance = g.db.execute(select(m.InventoryBalance).where(m.InventoryBalance.product_id == product.id,
                                                              m.InventoryBalance.branch_id == g.user.branch_id)).scalar_one_or_none()
    cost = dec(balance.weighted_avg_cost) if balance else dec(product.purchase_price)
    stock_post(g.db, g.user.branch_id, product.id, delta, cost,
               "adjustment", "stock_adjustment", adjustment.id, g.user.id,
               note=adjustment.note, allow_negative=settings_row().allow_negative_stock)
    audit(g.db, g.user.branch_id, g.user.id, "stock.adjust", "stock_adjustment", adjustment.id,
          after={"quantity_delta": str(delta), "reason": reason})
    g.db.commit()
    return jsonify(row_json(adjustment)), 201


@app.get("/api/stock-movements")
@requires("products.read")
def stock_movements():
    query = select(m.StockMovement).where(m.StockMovement.branch_id == g.user.branch_id)
    if request.args.get("product_id"):
        query = query.where(m.StockMovement.product_id == int(request.args["product_id"]))
    rows = g.db.scalars(query.order_by(m.StockMovement.id.desc()).limit(1000)).all()
    return jsonify([row_json(row, product_name=row.product.name) for row in rows])


@app.get("/api/products/<int:product_id>/movements")
@requires("products.read")
def product_movements(product_id):
    get_product(g.db, g.user.branch_id, product_id, active=False)
    rows = g.db.scalars(select(m.StockMovement).where(m.StockMovement.product_id == product_id).order_by(m.StockMovement.id.desc())).all()
    return jsonify([row_json(row) for row in rows])


@app.post("/api/categories")
@requires("products.write")
def create_category():
    data = body()
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("Category name required")
    category = m.Category(name=name, parent_id=data.get("parent_id") or None)
    g.db.add(category)
    g.db.flush()
    audit(g.db, g.user.branch_id, g.user.id, "category.create", "category", category.id)
    g.db.commit()
    return jsonify(row_json(category)), 201


@app.post("/api/units")
@requires("products.write")
def create_unit():
    data = body()
    unit = m.Unit(name=str(data.get("name") or "").strip(), symbol=str(data.get("symbol") or "").strip(),
                  decimal_places=int(data.get("decimal_places") or 0))
    if not unit.name or not unit.symbol or not 0 <= unit.decimal_places <= 6:
        raise ValueError("Valid unit name, symbol and decimal places required")
    g.db.add(unit)
    g.db.flush()
    audit(g.db, g.user.branch_id, g.user.id, "unit.create", "unit", unit.id)
    g.db.commit()
    return jsonify(row_json(unit)), 201


@app.post("/api/products/<int:product_id>/conversions")
@requires("products.write")
def create_conversion(product_id):
    data = body()
    product = get_product(g.db, g.user.branch_id, product_id)
    unit_id = resolve_unit(data.get("unit_id") or data.get("unit"))
    if unit_id == product.base_unit_id:
        raise ValueError("Base unit conversion is always one")
    factor = quantity(require_positive(data.get("factor_to_base"), "Conversion factor"))
    conversion = g.db.execute(select(m.UnitConversion).where(m.UnitConversion.product_id == product.id,
                                                              m.UnitConversion.unit_id == unit_id)).scalar_one_or_none()
    if conversion:
        conversion.factor_to_base = factor
    else:
        conversion = m.UnitConversion(product_id=product.id, unit_id=unit_id, factor_to_base=factor)
        g.db.add(conversion)
    g.db.flush()
    audit(g.db, g.user.branch_id, g.user.id, "unit_conversion.set", "unit_conversion", conversion.id)
    g.db.commit()
    return jsonify(row_json(conversion))


@app.route("/api/tax-rates", methods=["GET", "POST"])
@requires("settings.read")
def tax_rates_route():
    if request.method == "GET":
        rows = g.db.scalars(select(m.TaxRate).order_by(m.TaxRate.hsn_code, m.TaxRate.effective_from.desc())).all()
        return jsonify([row_json(row) for row in rows])
    if g.user.role != "admin":
        return jsonify(error="Permission denied"), 403
    data = body()
    hsn = str(data.get("hsn_code") or "").strip()
    rate = dec(data.get("rate"))
    if not hsn or not 0 <= rate <= 100:
        raise ValueError("HSN code and GST percentage from 0 to 100 required")
    start = date.fromisoformat(data["effective_from"])
    end = date.fromisoformat(data["effective_to"]) if data.get("effective_to") else None
    if end and end < start:
        raise ValueError("End date must follow start date")
    product_id = data.get("product_id") or None
    if product_id:
        get_product(g.db, g.user.branch_id, product_id, active=False)
    rate_row = m.TaxRate(hsn_code=hsn, product_id=product_id, rate=rate,
                         effective_from=start, effective_to=end,
                         notification_reference=data.get("notification_reference") or "")
    g.db.add(rate_row)
    g.db.flush()
    audit(g.db, g.user.branch_id, g.user.id, "tax_rate.create", "tax_rate", rate_row.id)
    g.db.commit()
    return jsonify(row_json(rate_row)), 201


@app.get("/api/reorder")
@requires("purchases.read")
def reorder_suggestions():
    products = g.db.scalars(select(m.Product).where(m.Product.branch_id == g.user.branch_id,
                                                    m.Product.archived == False)).all()
    balance_by_id = {row.product_id: row for row in g.db.scalars(select(m.InventoryBalance).where(
        m.InventoryBalance.branch_id == g.user.branch_id))}
    results = []
    for product in products:
        current = dec(balance_by_id[product.id].quantity) if product.id in balance_by_id else Decimal("0")
        minimum = dec(product.minimum_stock)
        if current <= minimum:
            results.append({"product_id": product.id, "sku": product.sku, "name": product.name,
                            "current_stock": str(current), "minimum_stock": str(minimum),
                            "suggested_quantity": str(max(Decimal("0"), minimum * 2 - current)),
                            "supplier_id": product.preferred_supplier_id})
    return jsonify(results)


@app.get("/api/products/<int:product_id>/price-history")
@requires("purchases.read")
def price_history(product_id):
    get_product(g.db, g.user.branch_id, product_id, active=False)
    rows = g.db.scalars(select(m.PriceHistory).where(m.PriceHistory.product_id == product_id).order_by(m.PriceHistory.id.desc())).all()
    return jsonify([row_json(row) for row in rows])


def contact_json(contact, kind):
    balance = party_balance(g.db, kind, contact)
    if kind == "customer":
        total, last = g.db.execute(select(func.coalesce(func.sum(m.Sale.grand_total), 0),
                                          func.max(m.Sale.sale_date)).where(
            m.Sale.customer_id == contact.id, m.Sale.status == "confirmed")).one()
        returned = g.db.execute(select(func.coalesce(func.sum(m.SaleReturn.grand_total), 0)).join(
            m.Sale, m.Sale.id == m.SaleReturn.sale_id).where(
            m.Sale.customer_id == contact.id, m.SaleReturn.status == "confirmed")).scalar_one()
        return row_json(contact, outstanding_balance=balance,
                        total_purchases=money(dec(total) - dec(returned)), last_purchase_date=last)
    total, last = g.db.execute(select(func.coalesce(func.sum(m.Purchase.grand_total), 0),
                                      func.max(m.Purchase.purchase_date)).where(
        m.Purchase.supplier_id == contact.id, m.Purchase.status == "confirmed")).one()
    returned = g.db.execute(select(func.coalesce(func.sum(m.PurchaseReturn.grand_total), 0)).where(
        m.PurchaseReturn.supplier_id == contact.id, m.PurchaseReturn.status == "confirmed")).scalar_one()
    return row_json(contact, outstanding_balance=balance,
                    total_purchases=money(dec(total) - dec(returned)), last_purchase_date=last)


def contact_list(model, kind):
    rows = g.db.scalars(select(model).where(model.branch_id == g.user.branch_id).order_by(model.name)).all()
    return jsonify([contact_json(row, kind) for row in rows])


@app.route("/api/customers", methods=["GET", "POST"])
@requires("contacts.read")
def customers():
    if request.method == "GET":
        return contact_list(m.Customer, "customer")
    if not can(g.user, "contacts.write"):
        return jsonify(error="Permission denied"), 403
    data = body()
    if not str(data.get("name") or "").strip():
        raise ValueError("Customer name required")
    customer = m.Customer(branch_id=g.user.branch_id)
    for field in ("name", "phone", "email", "address", "gstin", "state_code", "category",
                  "credit_limit", "credit_days", "opening_balance"):
        if field in data:
            setattr(customer, field, data[field])
    customer.gstin = validate_gstin(customer.gstin, customer.state_code)
    if dec(customer.credit_limit or 0) < 0:
        raise ValueError("Credit limit cannot be negative")
    g.db.add(customer)
    g.db.flush()
    audit(g.db, g.user.branch_id, g.user.id, "customer.create", "customer", customer.id)
    g.db.commit()
    return jsonify(contact_json(customer, "customer")), 201


@app.patch("/api/customers/<int:customer_id>")
@requires("contacts.write")
def update_customer(customer_id):
    customer = g.db.get(m.Customer, customer_id)
    if not customer or customer.branch_id != g.user.branch_id:
        abort(404)
    data = body()
    before = row_json(customer)
    for field in ("name", "phone", "email", "address", "gstin", "state_code", "category",
                  "credit_limit", "credit_days", "active"):
        if field in data:
            setattr(customer, field, data[field])
    customer.gstin = validate_gstin(customer.gstin, customer.state_code)
    if "opening_balance" in data:
        raise ValueError("Opening balance is immutable; use a ledger adjustment")
    audit(g.db, g.user.branch_id, g.user.id, "customer.update", "customer", customer.id,
          before=before, after=row_json(customer))
    g.db.commit()
    return jsonify(contact_json(customer, "customer"))


@app.route("/api/suppliers", methods=["GET", "POST"])
@requires("purchases.read")
def suppliers():
    if request.method == "GET":
        return contact_list(m.Supplier, "supplier")
    if not can(g.user, "contacts.write"):
        return jsonify(error="Permission denied"), 403
    data = body()
    if not str(data.get("name") or "").strip():
        raise ValueError("Supplier name required")
    supplier = m.Supplier(branch_id=g.user.branch_id)
    for field in ("name", "company_name", "phone", "email", "address", "gstin", "state_code",
                  "opening_balance", "credit_days"):
        if field in data:
            setattr(supplier, field, data[field])
    supplier.gstin = validate_gstin(supplier.gstin, supplier.state_code)
    g.db.add(supplier)
    g.db.flush()
    audit(g.db, g.user.branch_id, g.user.id, "supplier.create", "supplier", supplier.id)
    g.db.commit()
    return jsonify(contact_json(supplier, "supplier")), 201


@app.patch("/api/suppliers/<int:supplier_id>")
@requires("contacts.write")
def update_supplier(supplier_id):
    supplier = g.db.get(m.Supplier, supplier_id)
    if not supplier or supplier.branch_id != g.user.branch_id:
        abort(404)
    data = body()
    before = row_json(supplier)
    for field in ("name", "company_name", "phone", "email", "address", "gstin", "state_code",
                  "credit_days", "active"):
        if field in data:
            setattr(supplier, field, data[field])
    supplier.gstin = validate_gstin(supplier.gstin, supplier.state_code)
    if "opening_balance" in data:
        raise ValueError("Opening balance is immutable; use a ledger adjustment")
    audit(g.db, g.user.branch_id, g.user.id, "supplier.update", "supplier", supplier.id,
          before=before, after=row_json(supplier))
    g.db.commit()
    return jsonify(contact_json(supplier, "supplier"))


@app.get("/api/customers/<int:customer_id>/ledger")
@requires("contacts.read")
def customer_ledger(customer_id):
    customer = g.db.get(m.Customer, customer_id)
    if not customer or customer.branch_id != g.user.branch_id:
        abort(404)
    rows = g.db.scalars(select(m.LedgerEntry).where(m.LedgerEntry.customer_id == customer_id).order_by(m.LedgerEntry.id)).all()
    return jsonify({"customer": contact_json(customer, "customer"), "entries": [row_json(row) for row in rows]})


@app.get("/api/suppliers/<int:supplier_id>/ledger")
@requires("purchases.read")
def supplier_ledger(supplier_id):
    supplier = g.db.get(m.Supplier, supplier_id)
    if not supplier or supplier.branch_id != g.user.branch_id:
        abort(404)
    rows = g.db.scalars(select(m.LedgerEntry).where(m.LedgerEntry.supplier_id == supplier_id).order_by(m.LedgerEntry.id)).all()
    return jsonify({"supplier": contact_json(supplier, "supplier"), "entries": [row_json(row) for row in rows]})


def statement_pdf(kind, party):
    from reporting import export_pdf
    column = m.LedgerEntry.customer_id if kind == "customer" else m.LedgerEntry.supplier_id
    entries = g.db.scalars(select(m.LedgerEntry).where(column == party.id).order_by(m.LedgerEntry.occurred_at, m.LedgerEntry.id)).all()
    running = dec(party.opening_balance)
    rows = [["Opening", "Opening balance", "", str(money(max(running, 0))),
             str(money(max(-running, 0))), str(money(running))]]
    for entry in entries:
        amount = dec(entry.amount)
        running += amount
        rows.append([entry.occurred_at.date().isoformat(), entry.entry_type,
                     f"{entry.source_type} #{entry.source_id}",
                     str(money(max(amount, 0))), str(money(max(-amount, 0))), str(money(running))])
    title = f"{kind.title()} statement — {party.name}"
    output = export_pdf(["Date", "Type", "Reference", "Debit", "Credit", "Balance"], rows, title,
                        f"Outstanding: INR {money(running)}")
    return send_file(io.BytesIO(output), mimetype="application/pdf", as_attachment=True,
                     download_name=f"{kind}-{party.id}-statement.pdf")


@app.get("/api/customers/<int:customer_id>/statement")
@requires("contacts.read")
def customer_statement(customer_id):
    customer = g.db.get(m.Customer, customer_id)
    if not customer or customer.branch_id != g.user.branch_id:
        abort(404)
    return statement_pdf("customer", customer)


@app.get("/api/suppliers/<int:supplier_id>/statement")
@requires("purchases.read")
def supplier_statement(supplier_id):
    supplier = g.db.get(m.Supplier, supplier_id)
    if not supplier or supplier.branch_id != g.user.branch_id:
        abort(404)
    return statement_pdf("supplier", supplier)


@app.post("/api/customers/<int:customer_id>/payments")
@requires("payments.write")
def customer_payment(customer_id):
    customer = g.db.execute(select(m.Customer).where(m.Customer.id == customer_id,
                                                         m.Customer.branch_id == g.user.branch_id).with_for_update()).scalar_one_or_none()
    if not customer:
        abort(404)
    data = body()
    amount = money(require_positive(data.get("amount"), "Payment amount"))
    if amount > party_balance(g.db, "customer", customer) and not data.get("allow_advance"):
        raise ValueError("Payment exceeds customer outstanding; enable advance to record a deposit")
    payment = m.PartyPayment(branch_id=g.user.branch_id, party_type="customer", customer_id=customer.id,
                             amount=amount, method=data.get("method") or "cash", reference=data.get("reference") or "",
                             note=data.get("note") or "", created_by=g.user.id)
    g.db.add(payment)
    g.db.flush()
    ledger_post(g.db, g.user.branch_id, "customer", customer.id, -amount,
                "customer_payment", "party_payment", payment.id, g.user.id)
    allocate_customer_payment(g.db, payment, data.get("sale_id"))
    audit(g.db, g.user.branch_id, g.user.id, "customer.payment", "party_payment", payment.id,
          after={"amount": str(amount)})
    g.db.commit()
    return jsonify(row_json(payment, outstanding_balance=party_balance(g.db, "customer", customer))), 201


def allocate_customer_payment(db, payment, sale_id=None):
    amount_left = dec(payment.amount)
    query = select(m.Sale).where(m.Sale.branch_id == payment.branch_id,
                                 m.Sale.customer_id == payment.customer_id,
                                 m.Sale.status == "confirmed", m.Sale.paid_total < m.Sale.grand_total)
    if sale_id:
        query = query.where(m.Sale.id == int(sale_id))
    else:
        query = query.order_by(m.Sale.sale_date, m.Sale.id)
    for sale in db.scalars(query.with_for_update()).all():
        returned = db.execute(select(func.coalesce(func.sum(m.SaleReturn.grand_total), 0)).where(
            m.SaleReturn.sale_id == sale.id, m.SaleReturn.status == "confirmed")).scalar_one()
        due = money(dec(sale.grand_total) - dec(returned) - dec(sale.paid_total))
        if due <= 0:
            continue
        allocated = min(amount_left, due)
        db.add(m.PaymentAllocation(party_payment_id=payment.id, sale_id=sale.id, amount=allocated))
        sale.paid_total = money(dec(sale.paid_total) + allocated)
        amount_left -= allocated
        if amount_left <= 0:
            break


def purchase_json(purchase):
    return row_json(purchase,
                    supplier_name=purchase.supplier.name if purchase.supplier else "",
                    outstanding=money(dec(purchase.grand_total) - dec(purchase.paid_total)),
                    items=[row_json(item, product_name=item.product.name) for item in purchase.items],
                    payments=[row_json(pay) for pay in purchase.payments])


@app.route("/api/purchases", methods=["GET", "POST"])
@requires("purchases.read")
def purchases():
    if request.method == "GET":
        rows = g.db.scalars(select(m.Purchase).where(m.Purchase.branch_id == g.user.branch_id).order_by(m.Purchase.id.desc()).limit(1000)).all()
        return jsonify([purchase_json(row) for row in rows])
    if not can(g.user, "purchases.write"):
        return jsonify(error="Permission denied"), 403
    from purchasing import create_purchase
    data = body()
    purchase = create_purchase(g.db, g.user.branch_id, g.user.id, data, settings_row())
    g.db.commit()
    return jsonify(purchase_json(purchase)), 201


@app.get("/api/purchases/<int:purchase_id>")
@requires("purchases.read")
def purchase_detail(purchase_id):
    purchase = g.db.get(m.Purchase, purchase_id)
    if not purchase or purchase.branch_id != g.user.branch_id:
        abort(404)
    return jsonify(purchase_json(purchase))


@app.post("/api/purchases/<int:purchase_id>/confirm")
@requires("purchases.write")
def confirm_purchase_route(purchase_id):
    purchase = g.db.get(m.Purchase, purchase_id)
    if not purchase or purchase.branch_id != g.user.branch_id:
        abort(404)
    from purchasing import confirm_purchase
    data = body()
    posted = confirm_purchase(g.db, purchase, g.user.id, data.get("payments"))
    g.db.commit()
    return jsonify(purchase_json(posted))


@app.post("/api/purchases/<int:purchase_id>/payments")
@requires("payments.write")
def purchase_payment_route(purchase_id):
    purchase = g.db.get(m.Purchase, purchase_id)
    if not purchase or purchase.branch_id != g.user.branch_id:
        abort(404)
    from purchasing import purchase_payment
    payment = purchase_payment(g.db, purchase_id, g.user.id, body())
    g.db.commit()
    return jsonify(row_json(payment)), 201


@app.post("/api/purchases/<int:purchase_id>/return")
@requires("purchases.write")
def purchase_return_route(purchase_id):
    purchase = g.db.get(m.Purchase, purchase_id)
    if not purchase or purchase.branch_id != g.user.branch_id:
        abort(404)
    from purchasing import purchase_return
    result = purchase_return(g.db, purchase_id, g.user.id, body(), settings_row())
    g.db.commit()
    return jsonify(row_json(result, items=[row_json(item) for item in result.items])), 201


@app.get("/api/purchases/<int:purchase_id>/returns")
@requires("purchases.read")
def purchase_returns_list(purchase_id):
    purchase = g.db.get(m.Purchase, purchase_id)
    if not purchase or purchase.branch_id != g.user.branch_id:
        abort(404)
    rows = g.db.scalars(select(m.PurchaseReturn).where(m.PurchaseReturn.purchase_id == purchase.id).order_by(m.PurchaseReturn.id.desc())).all()
    return jsonify([row_json(row, items=[row_json(item) for item in row.items]) for row in rows])


@app.get("/debit-notes/<int:return_id>/<fmt>")
@requires("purchases.read")
def debit_note_document(return_id, fmt):
    from reporting import export_pdf, printable_html
    ret = g.db.get(m.PurchaseReturn, return_id)
    purchase = g.db.get(m.Purchase, ret.purchase_id) if ret else None
    if not ret or not purchase or purchase.branch_id != g.user.branch_id:
        abort(404)
    rows = [[g.db.get(m.Product, item.product_id).name, str(item.base_quantity),
             str(item.taxable_value), str(item.cgst), str(item.sgst),
             str(item.igst), str(item.line_total)] for item in ret.items]
    title = f"Debit Note {ret.number}"
    subtitle = f"{settings_row().store_name} | Against supplier invoice {purchase.supplier_invoice_no} | Supplier: {purchase.supplier.name} | Reason: {ret.reason}"
    headers = ["Product", "Qty", "Taxable", "CGST", "SGST", "IGST", "Total"]
    if fmt == "print":
        return Response(printable_html(title, headers, rows, subtitle), mimetype="text/html")
    if fmt == "pdf":
        output = export_pdf(headers, rows, title, subtitle)
        return send_file(io.BytesIO(output), mimetype="application/pdf", as_attachment=True,
                         download_name=f"{ret.number.replace('/', '-')}.pdf")
    abort(404)


@app.route("/api/purchase-orders", methods=["GET", "POST"])
@requires("purchases.read")
def purchase_orders():
    if request.method == "GET":
        rows = g.db.scalars(select(m.PurchaseOrder).where(m.PurchaseOrder.branch_id == g.user.branch_id).order_by(m.PurchaseOrder.id.desc())).all()
        return jsonify([row_json(row, items=[row_json(item) for item in row.items]) for row in rows])
    if not can(g.user, "purchases.write"):
        return jsonify(error="Permission denied"), 403
    from purchasing import create_purchase_order
    order = create_purchase_order(g.db, g.user.branch_id, g.user.id, body(), settings_row())
    g.db.commit()
    return jsonify(row_json(order, items=[row_json(item) for item in order.items])), 201


@app.post("/api/purchase-orders/<int:order_id>/receive")
@requires("purchases.write")
def receive_purchase_order(order_id):
    from purchasing import create_purchase, confirm_purchase
    order = g.db.execute(select(m.PurchaseOrder).where(m.PurchaseOrder.id == order_id,
                                                          m.PurchaseOrder.branch_id == g.user.branch_id).with_for_update()).scalar_one_or_none()
    if not order:
        abort(404)
    existing = g.db.execute(select(m.Purchase).where(m.Purchase.purchase_order_id == order.id)).scalar_one_or_none()
    if existing:
        return jsonify(purchase_json(existing))
    if order.status not in ("draft", "approved", "sent"):
        raise ValueError("Purchase order cannot be received")
    data = body()
    purchase = create_purchase(g.db, g.user.branch_id, g.user.id, {
        "supplier_id": order.supplier_id,
        "purchase_order_id": order.id,
        "supplier_invoice_no": data.get("supplier_invoice_no") or "",
        "purchase_date": data.get("purchase_date") or date.today().isoformat(),
        "place_of_supply_code": data.get("place_of_supply_code") or settings_row().state_code,
        "items": [{"product_id": item.product_id, "quantity": str(item.quantity),
                   "unit_id": item.unit_id, "unit_price": str(item.unit_price),
                   "gst_rate": str(item.gst_rate), "discount_percent": str(item.discount_percent)} for item in order.items],
        "notes": data.get("notes") or f"Received against {order.number}",
    }, settings_row())
    confirm_purchase(g.db, purchase, g.user.id, data.get("payments"))
    order.status = "received"
    audit(g.db, g.user.branch_id, g.user.id, "purchase_order.receive", "purchase_order", order.id,
          after={"purchase_id": purchase.id})
    g.db.commit()
    return jsonify(purchase_json(purchase)), 201


@app.post("/api/suppliers/<int:supplier_id>/payments")
@requires("payments.write")
def supplier_payment_route(supplier_id):
    from purchasing import supplier_payment
    payment = supplier_payment(g.db, supplier_id, g.user.branch_id, g.user.id, body())
    g.db.commit()
    return jsonify(row_json(payment)), 201


def sale_json(sale):
    items = []
    for item in sale.items:
        unit = g.db.get(m.Unit, item.unit_id)
        items.append(row_json(item, unit=unit.symbol if unit else ""))
    returned = g.db.execute(select(func.coalesce(func.sum(m.SaleReturn.grand_total), 0)).where(
        m.SaleReturn.sale_id == sale.id, m.SaleReturn.status == "confirmed")).scalar_one()
    refund = g.db.execute(select(func.coalesce(func.sum(m.LedgerEntry.amount), 0)).join(
        m.SaleReturn, (m.SaleReturn.id == m.LedgerEntry.source_id)
    ).where(m.LedgerEntry.source_type == "sale_return", m.LedgerEntry.entry_type == "refund",
            m.SaleReturn.sale_id == sale.id)).scalar_one()
    due = max(Decimal("0"), money(dec(sale.grand_total) - dec(returned) - dec(sale.paid_total) + dec(refund)))
    return row_json(sale, items=items, payments=[row_json(row) for row in sale.payments],
                    returned_total=returned, refunded_total=refund,
                    outstanding=due, balance_due=due)


@app.route("/api/sales", methods=["GET"])
@requires("sales.own")
def sales():
    query = select(m.Sale).where(m.Sale.branch_id == g.user.branch_id)
    if g.user.role == "cashier":
        query = query.where(m.Sale.created_by == g.user.id)
    rows = g.db.scalars(query.order_by(m.Sale.id.desc()).limit(1000)).all()
    return jsonify([sale_json(row) for row in rows])


@app.post("/api/sales/preview")
@requires("sales.write")
def preview_sale_route():
    data = body()
    approved_quote = False
    if data.get("quotation_id"):
        quote = g.db.get(m.Quotation, int(data["quotation_id"]))
        if not quote or quote.branch_id != g.user.branch_id or quote.status != "approved":
            raise ValueError("Approved quotation not found")
        approved_quote = True
    result = preview_sale(g.db, g.user.branch_id, data, settings_row(),
                          can_override=approved_quote or g.user.role in ("admin", "manager"))
    return jsonify(as_json(result))


@app.post("/api/sales/checkout")
@requires("sales.write")
def checkout_route():
    sale = checkout(g.db, g.user.branch_id, g.user.id, body(), settings_row(),
                    can_override=g.user.role in ("admin", "manager"))
    g.db.commit()
    return jsonify(sale_json(sale)), 201


@app.get("/api/sales/<int:sale_id>")
@requires("sales.own")
def sale_detail(sale_id):
    sale = g.db.get(m.Sale, sale_id)
    if not sale or sale.branch_id != g.user.branch_id or (g.user.role == "cashier" and sale.created_by != g.user.id):
        abort(404)
    return jsonify(sale_json(sale))


@app.post("/api/sales/<int:sale_id>/return")
@requires("sales.return")
def sale_return_route(sale_id):
    result = return_sale(g.db, g.user.branch_id, sale_id, g.user.id, body(), settings_row())
    g.db.commit()
    return jsonify(row_json(result, items=[row_json(row) for row in result.items])), 201


@app.get("/api/sales/<int:sale_id>/returns")
@requires("sales.own")
def sale_returns_list(sale_id):
    sale = g.db.get(m.Sale, sale_id)
    if not sale or sale.branch_id != g.user.branch_id or (g.user.role == "cashier" and sale.created_by != g.user.id):
        abort(404)
    rows = g.db.scalars(select(m.SaleReturn).where(m.SaleReturn.sale_id == sale.id).order_by(m.SaleReturn.id.desc())).all()
    return jsonify([row_json(row, items=[row_json(item) for item in row.items]) for row in rows])


@app.get("/credit-notes/<int:return_id>/<fmt>")
@requires("sales.own")
def credit_note_document(return_id, fmt):
    from reporting import export_pdf, printable_html
    ret = g.db.get(m.SaleReturn, return_id)
    sale = g.db.get(m.Sale, ret.sale_id) if ret else None
    if not ret or not sale or sale.branch_id != g.user.branch_id or (g.user.role == "cashier" and sale.created_by != g.user.id):
        abort(404)
    rows = [[g.db.get(m.Product, item.product_id).name,
             str(item.base_quantity), str(item.taxable_value), str(item.cgst),
             str(item.sgst), str(item.igst), str(item.line_total)] for item in ret.items]
    title = f"GST Credit Note {ret.number}"
    subtitle = f"{settings_row().store_name} | Against invoice {sale.invoice_no} | Recipient: {sale.customer_name} | GSTIN: {sale.customer_gstin} | Reason: {ret.reason}"
    headers = ["Product", "Qty", "Taxable", "CGST", "SGST", "IGST", "Total"]
    if fmt == "print":
        return Response(printable_html(title, headers, rows, subtitle), mimetype="text/html")
    if fmt == "pdf":
        output = export_pdf(headers, rows, title, subtitle)
        return send_file(io.BytesIO(output), mimetype="application/pdf", as_attachment=True,
                         download_name=f"{ret.number.replace('/', '-')}.pdf")
    abort(404)


@app.post("/api/sales/<int:sale_id>/cancel")
@requires("sales.cancel")
def cancel_sale_route(sale_id):
    sale = g.db.get(m.Sale, sale_id)
    if not sale or sale.branch_id != g.user.branch_id or sale.cancelled_at:
        raise ValueError("Sale not found or already cancelled")
    prior = g.db.execute(select(func.coalesce(func.sum(m.SaleReturnItem.base_quantity), 0)).join(
        m.SaleReturn, m.SaleReturn.id == m.SaleReturnItem.return_id
    ).where(m.SaleReturn.sale_id == sale.id)).scalar_one()
    if dec(prior) > 0:
        raise ValueError("Partially returned invoices must be resolved through further returns")
    data = body()
    result = return_sale(g.db, g.user.branch_id, sale_id, g.user.id,
                         {"items": [{"sale_item_id": item.id, "quantity": str(item.quantity)} for item in sale.items],
                          "reason": data.get("reason") or "Cancelled invoice",
                          "refund_method": data.get("refund_method") or "cash"}, settings_row())
    sale.cancelled_at = datetime.now(timezone.utc)
    sale.cancelled_by = g.user.id
    sale.cancellation_reason = data.get("reason") or "Cancelled invoice"
    audit(g.db, g.user.branch_id, g.user.id, "sale.cancel", "sale", sale.id,
          after={"credit_note": result.number})
    g.db.commit()
    return jsonify(sale_json(sale))


@app.post("/api/sales/<int:sale_id>/challan")
@requires("sales.return")
def create_delivery_challan(sale_id):
    sale = g.db.get(m.Sale, sale_id)
    if not sale or sale.branch_id != g.user.branch_id or sale.status != "confirmed" or sale.cancelled_at:
        raise ValueError("Confirmed sale not found")
    existing = g.db.execute(select(m.DeliveryChallan).where(m.DeliveryChallan.sale_id == sale.id)).scalar_one_or_none()
    if existing:
        return jsonify(row_json(existing))
    number, _ = next_document_no(g.db, g.user.branch_id, settings_row(), "delivery_challan")
    data = body()
    challan = m.DeliveryChallan(branch_id=g.user.branch_id, sale_id=sale.id, number=number,
                                challan_date=date.today(), place_of_supply_code=sale.place_of_supply_code,
                                customer_name=sale.customer_name, customer_address=sale.customer_address,
                                notes=data.get("notes") or "", created_by=g.user.id)
    g.db.add(challan)
    g.db.flush()
    audit(g.db, g.user.branch_id, g.user.id, "challan.create", "delivery_challan", challan.id)
    g.db.commit()
    return jsonify(row_json(challan)), 201


@app.get("/api/sales/<int:sale_id>/challan")
@requires("sales.own")
def get_delivery_challan(sale_id):
    sale = g.db.get(m.Sale, sale_id)
    if not sale or sale.branch_id != g.user.branch_id or (g.user.role == "cashier" and sale.created_by != g.user.id):
        abort(404)
    challan = g.db.execute(select(m.DeliveryChallan).where(m.DeliveryChallan.sale_id == sale.id)).scalar_one_or_none()
    if not challan:
        abort(404)
    return jsonify(row_json(challan))


@app.get("/challans/<int:challan_id>/<fmt>")
@requires("sales.own")
def delivery_challan_document(challan_id, fmt):
    from reporting import export_pdf, printable_html
    challan = g.db.get(m.DeliveryChallan, challan_id)
    if not challan or challan.branch_id != g.user.branch_id or (g.user.role == "cashier" and challan.sale.created_by != g.user.id):
        abort(404)
    sale = challan.sale
    rows = [[item.description, item.hsn_code, str(item.quantity),
             g.db.get(m.Unit, item.unit_id).symbol] for item in sale.items]
    title = f"Delivery Challan {challan.number}"
    subtitle = f"{settings_row().store_name} | Dated {challan.challan_date} | Against invoice {sale.invoice_no} | Recipient: {challan.customer_name}, {challan.customer_address} | Place of supply: {challan.place_of_supply_code} | {challan.notes}"
    if fmt == "print":
        return Response(printable_html(title, ["Item", "HSN", "Quantity", "Unit"], rows, subtitle), mimetype="text/html")
    if fmt == "pdf":
        output = export_pdf(["Item", "HSN", "Quantity", "Unit"], rows, title, subtitle)
        return send_file(io.BytesIO(output), mimetype="application/pdf", as_attachment=True,
                         download_name=f"{challan.number.replace('/', '-')}.pdf")
    abort(404)


@app.route("/api/quotations", methods=["GET", "POST"])
@requires("quotes.write")
def quotations():
    if request.method == "GET":
        rows = g.db.scalars(select(m.Quotation).where(m.Quotation.branch_id == g.user.branch_id).order_by(m.Quotation.id.desc())).all()
        return jsonify([row_json(row, items=[row_json(item, product_name=item.product.name) for item in row.items]) for row in rows])
    quote = create_quotation(g.db, g.user.branch_id, g.user.id, body(), settings_row())
    g.db.commit()
    return jsonify(row_json(quote, items=[row_json(item, product_name=item.product.name) for item in quote.items])), 201


@app.patch("/api/quotations/<int:quotation_id>")
@requires("quotes.write")
def update_quotation(quotation_id):
    quote = g.db.get(m.Quotation, quotation_id)
    if not quote or quote.branch_id != g.user.branch_id:
        abort(404)
    if quote.converted_sale_id:
        raise ValueError("Converted quotation cannot be edited")
    data = body()
    if data.get("status") == "approved" and g.user.role not in ("admin", "manager"):
        return jsonify(error="Manager approval required"), 403
    before = row_json(quote)
    if "items" in data:
        replace_quotation_items(g.db, quote, data, settings_row(), g.user.id)
    for field in ("status", "valid_until", "terms", "notes", "customer_name"):
        if field in data:
            setattr(quote, field, date.fromisoformat(data[field]) if field == "valid_until" and data[field] else data[field])
    if quote.status not in ("draft", "sent", "approved", "rejected"):
        raise ValueError("Invalid quotation status")
    audit(g.db, g.user.branch_id, g.user.id, "quotation.update", "quotation", quote.id,
          before=before, after=row_json(quote))
    g.db.commit()
    return jsonify(row_json(quote, items=[row_json(item) for item in quote.items]))


@app.post("/api/quotations/<int:quotation_id>/convert")
@requires("sales.write")
def convert_quotation(quotation_id):
    quote = g.db.get(m.Quotation, quotation_id)
    if not quote or quote.branch_id != g.user.branch_id:
        abort(404)
    data = body()
    data["quotation_id"] = quotation_id
    sale = checkout(g.db, g.user.branch_id, g.user.id, data, settings_row(),
                    can_override=True)
    g.db.commit()
    return jsonify(sale_json(sale)), 201


@app.get("/quotations/<int:quotation_id>/print")
@requires("quotes.write")
def print_quotation(quotation_id):
    from reporting import amount_in_words
    quote = g.db.get(m.Quotation, quotation_id)
    if not quote or quote.branch_id != g.user.branch_id:
        abort(404)
    return render_template("quotation.html", quote=quote, items=quote.items,
                           settings=settings_row(), amount_in_words=amount_in_words(quote.grand_total),
                           units={u.id: u.symbol for u in g.db.scalars(select(m.Unit))})


@app.get("/quotations/<int:quotation_id>/pdf")
@requires("quotes.write")
def pdf_quotation(quotation_id):
    from reporting import export_pdf, amount_in_words
    quote = g.db.get(m.Quotation, quotation_id)
    if not quote or quote.branch_id != g.user.branch_id:
        abort(404)
    output = quotation_pdf_bytes(quote)
    return send_file(io.BytesIO(output), mimetype="application/pdf", as_attachment=True,
                     download_name=f"{quote.number.replace('/', '-')}.pdf")


def quotation_pdf_bytes(quote):
    from reporting import export_pdf, amount_in_words
    settings = g.db.execute(select(m.StoreSettings).where(m.StoreSettings.branch_id == quote.branch_id)).scalar_one()
    rows = [[item.product.name, str(item.quantity), g.db.get(m.Unit, item.unit_id).symbol,
             str(item.unit_price), str(item.taxable_value), str(item.tax_total), str(item.line_total)]
            for item in quote.items]
    return export_pdf(["Product", "Quantity", "Unit", "Rate", "Taxable", "GST", "Total"], rows,
                      f"Quotation {quote.number}",
                      f"{settings.store_name} | {quote.customer_name} | Valid until {quote.valid_until or '—'} | INR {quote.grand_total} | {amount_in_words(quote.grand_total)}")


def create_share(entity_type, entity_id):
    token = secrets.token_urlsafe(32)
    record = m.ShareLink(branch_id=g.user.branch_id, entity_type=entity_type, entity_id=entity_id,
                         token_hash=hashlib.sha256(token.encode()).hexdigest(),
                         expires_at=datetime.now(timezone.utc) + timedelta(days=7), created_by=g.user.id)
    g.db.add(record)
    g.db.flush()
    audit(g.db, g.user.branch_id, g.user.id, "document.share_link", entity_type, entity_id,
          after={"share_id": record.id, "expires_at": record.expires_at.isoformat()})
    g.db.commit()
    return jsonify({"id": record.id, "url": url_for("public_shared_document", token=token, _external=True),
                    "expires_at": record.expires_at.isoformat()})


@app.post("/api/sales/<int:sale_id>/share")
@requires("sales.own")
def share_sale(sale_id):
    sale = g.db.get(m.Sale, sale_id)
    if not sale or sale.branch_id != g.user.branch_id or (g.user.role == "cashier" and sale.created_by != g.user.id):
        abort(404)
    return create_share("sale", sale.id)


@app.post("/api/quotations/<int:quote_id>/share")
@requires("quotes.write")
def share_quotation(quote_id):
    quote = g.db.get(m.Quotation, quote_id)
    if not quote or quote.branch_id != g.user.branch_id:
        abort(404)
    return create_share("quotation", quote.id)


@app.get("/share/<token>/pdf")
def public_shared_document(token):
    from reporting import invoice_pdf
    if len(token) > 100:
        abort(404)
    digest = hashlib.sha256(token.encode()).hexdigest()
    record = g.db.execute(select(m.ShareLink).where(m.ShareLink.token_hash == digest)).scalar_one_or_none()
    if not record or record.revoked_at:
        abort(404)
    expires = record.expires_at.replace(tzinfo=timezone.utc) if record.expires_at.tzinfo is None else record.expires_at
    if expires < datetime.now(timezone.utc):
        abort(404)
    if record.entity_type == "sale":
        sale = g.db.get(m.Sale, record.entity_id)
        if not sale or sale.branch_id != record.branch_id:
            abort(404)
        settings = g.db.execute(select(m.StoreSettings).where(m.StoreSettings.branch_id == record.branch_id)).scalar_one()
        items = [row_json(item, unit=g.db.get(m.Unit, item.unit_id).symbol) for item in sale.items]
        output = invoice_pdf(sale, items, settings, sale.payments)
        filename = sale.invoice_no.replace("/", "-") + ".pdf"
    elif record.entity_type == "quotation":
        quote = g.db.get(m.Quotation, record.entity_id)
        if not quote or quote.branch_id != record.branch_id:
            abort(404)
        output = quotation_pdf_bytes(quote)
        filename = quote.number.replace("/", "-") + ".pdf"
    else:
        abort(404)
    return send_file(io.BytesIO(output), mimetype="application/pdf", as_attachment=True,
                     download_name=filename)


@app.post("/api/share-links/<int:share_id>/revoke")
@requires("sales.return")
def revoke_share_link(share_id):
    record = g.db.get(m.ShareLink, share_id)
    if not record or record.branch_id != g.user.branch_id:
        abort(404)
    record.revoked_at = datetime.now(timezone.utc)
    audit(g.db, g.user.branch_id, g.user.id, "document.share_revoke", record.entity_type, record.entity_id,
          after={"share_id": record.id})
    g.db.commit()
    return jsonify(ok=True)


@app.route("/api/held-bills", methods=["GET", "POST"])
@requires("sales.write")
def held_bills():
    if request.method == "GET":
        rows = g.db.scalars(select(m.HeldBill).where(m.HeldBill.branch_id == g.user.branch_id,
                                                      m.HeldBill.user_id == g.user.id).order_by(m.HeldBill.id.desc())).all()
        return jsonify([row_json(row) for row in rows])
    data = body()
    if not isinstance(data.get("cart"), list) or not data["cart"]:
        raise ValueError("Cannot hold an empty cart")
    held = m.HeldBill(branch_id=g.user.branch_id, user_id=g.user.id,
                      label=data.get("label") or "Held bill", cart_json=data["cart"],
                      customer_id=data.get("customer_id") or None)
    g.db.add(held)
    g.db.flush()
    audit(g.db, g.user.branch_id, g.user.id, "bill.hold", "held_bill", held.id)
    g.db.commit()
    return jsonify(row_json(held)), 201


@app.delete("/api/held-bills/<int:held_id>")
@requires("sales.write")
def delete_held_bill(held_id):
    held = g.db.get(m.HeldBill, held_id)
    if not held or held.branch_id != g.user.branch_id or held.user_id != g.user.id:
        abort(404)
    audit(g.db, g.user.branch_id, g.user.id, "bill.resume", "held_bill", held.id)
    g.db.delete(held)
    g.db.commit()
    return jsonify(ok=True)


@app.route("/api/expenses", methods=["GET", "POST"])
@requires("expenses.read")
def expenses():
    if request.method == "GET":
        rows = g.db.scalars(select(m.Expense).where(m.Expense.branch_id == g.user.branch_id).order_by(m.Expense.expense_date.desc(), m.Expense.id.desc()).limit(1000)).all()
        return jsonify([row_json(row) for row in rows])
    if not can(g.user, "expenses.write"):
        return jsonify(error="Permission denied"), 403
    data = body()
    amount = money(require_positive(data.get("amount"), "Expense amount"))
    expense = m.Expense(branch_id=g.user.branch_id,
                        expense_date=date.fromisoformat(data["expense_date"]) if data.get("expense_date") else date.today(),
                        category=data.get("category") or "Other", amount=amount,
                        payment_method=data.get("payment_method") or "cash",
                        description=data.get("description") or "", created_by=g.user.id)
    g.db.add(expense)
    g.db.flush()
    audit(g.db, g.user.branch_id, g.user.id, "expense.create", "expense", expense.id)
    g.db.commit()
    return jsonify(row_json(expense)), 201


@app.patch("/api/expenses/<int:expense_id>")
@requires("expenses.write")
def update_expense(expense_id):
    expense = g.db.get(m.Expense, expense_id)
    if not expense or expense.branch_id != g.user.branch_id:
        abort(404)
    data = body()
    before = row_json(expense)
    for field in ("category", "amount", "payment_method", "description"):
        if field in data:
            setattr(expense, field, data[field])
    if "expense_date" in data:
        expense.expense_date = date.fromisoformat(data["expense_date"])
    if dec(expense.amount) <= 0:
        raise ValueError("Expense amount must be positive")
    audit(g.db, g.user.branch_id, g.user.id, "expense.update", "expense", expense.id,
          before=before, after=row_json(expense))
    g.db.commit()
    return jsonify(row_json(expense))


@app.get("/api/dashboard")
@requires("dashboard")
def dashboard_route():
    from reporting import dashboard
    values = dashboard(g.db, request.args.get("start"), request.args.get("end"), g.user.branch_id)
    if g.user.role != "admin":
        for key in ("gross_profit", "net_profit", "purchase_return_variance",
                    "inventory_cost_value", "gst_paid", "monthly_profit"):
            values.pop(key, None)
    return jsonify(values)


@app.get("/api/reports/<kind>")
@requires("reports.operational")
def report_route(kind):
    from reporting import report_rows, export_xlsx, export_pdf, printable_html
    if kind in ("profit", "gst", "gstr1_prep", "gstr3b_prep", "purchase_price_trends") and g.user.role != "admin":
        return jsonify(error="Permission denied"), 403
    report = report_rows(g.db, kind, request.args.get("start"), request.args.get("end"), g.user.branch_id)
    fmt = request.args.get("format")
    if fmt == "xlsx":
        output = export_xlsx(report["headers"], report["rows"], report["title"])
        return send_file(io.BytesIO(output), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         as_attachment=True, download_name=f"{kind}-report.xlsx")
    if fmt == "pdf":
        output = export_pdf(report["headers"], report["rows"], report["title"])
        return send_file(io.BytesIO(output), mimetype="application/pdf", as_attachment=True,
                         download_name=f"{kind}-report.pdf")
    if fmt == "print":
        return Response(printable_html(report["title"], report["headers"], report["rows"]), mimetype="text/html")
    return jsonify(report)


@app.route("/api/settings", methods=["GET", "PUT"])
@requires("settings.read")
def settings_route():
    settings = settings_row()
    if request.method == "GET":
        return jsonify(row_json(settings))
    if g.user.role != "admin":
        return jsonify(error="Permission denied"), 403
    data = body()
    before = row_json(settings)
    allowed = ("store_name", "address", "phone", "email", "gstin", "state", "state_code",
               "financial_year_start_month", "invoice_prefix", "quotation_prefix", "tax_inclusive",
               "allow_negative_stock", "default_payment_method", "terms", "receipt_footer",
               "default_paper", "is_gst_registered", "e_invoice_applicable")
    for field in allowed:
        if field in data:
            setattr(settings, field, data[field])
    if not str(settings.store_name).strip():
        raise ValueError("Store name required")
    if settings.state_code and (len(settings.state_code) != 2 or not settings.state_code.isdigit()):
        raise ValueError("State code must have two digits")
    settings.gstin = validate_gstin(settings.gstin, settings.state_code)
    if settings.is_gst_registered and (not settings.gstin or not settings.state_code):
        raise ValueError("GST registered stores require a GSTIN and state code")
    if len(settings.invoice_prefix) > 3:
        raise ValueError("Invoice prefix must be at most 3 characters for 16-character GST numbering")
    if settings.default_paper not in ("a4", "80mm"):
        raise ValueError("Default paper must be a4 or 80mm")
    if settings.e_invoice_applicable and not settings.is_gst_registered:
        raise ValueError("E-invoicing requires GST registration")
    audit(g.db, g.user.branch_id, g.user.id, "settings.update", "settings", settings.id,
          before=before, after=row_json(settings))
    g.db.commit()
    return jsonify(row_json(settings))


@app.route("/api/users", methods=["GET", "POST"])
@requires("users.manage")
def users_route():
    if request.method == "GET":
        rows = g.db.scalars(select(m.User).where(m.User.branch_id == g.user.branch_id).order_by(m.User.id)).all()
        return jsonify([{key: value for key, value in row_json(row).items() if key != "password_hash"} for row in rows])
    data = body()
    if data.get("role") not in ("admin", "manager", "cashier"):
        raise ValueError("Role must be admin, manager or cashier")
    if len(str(data.get("password") or "")) < 12:
        raise ValueError("Password must contain at least 12 characters")
    user = m.User(branch_id=g.user.branch_id, username=str(data.get("username") or "").strip(),
                  full_name=str(data.get("full_name") or "").strip(), role=data["role"],
                  password_hash=generate_password_hash(data["password"]))
    if not user.username or not user.full_name:
        raise ValueError("Username and full name required")
    g.db.add(user)
    g.db.flush()
    audit(g.db, g.user.branch_id, g.user.id, "user.create", "user", user.id,
          after={"username": user.username, "role": user.role})
    g.db.commit()
    return jsonify({key: value for key, value in row_json(user).items() if key != "password_hash"}), 201


@app.patch("/api/users/<int:user_id>")
@requires("users.manage")
def update_user_route(user_id):
    user = g.db.get(m.User, user_id)
    if not user or user.branch_id != g.user.branch_id:
        abort(404)
    data = body()
    before = {"full_name": user.full_name, "role": user.role, "active": user.active}
    if "role" in data:
        if data["role"] not in ("admin", "manager", "cashier"):
            raise ValueError("Invalid role")
        user.role = data["role"]
    if "full_name" in data:
        user.full_name = str(data["full_name"]).strip()
    if "active" in data:
        user.active = bool(data["active"])
    if "password" in data:
        if len(str(data["password"])) < 12:
            raise ValueError("Password must contain at least 12 characters")
        user.password_hash = generate_password_hash(data["password"])
    if user.id == g.user.id and (not user.active or user.role != "admin"):
        raise ValueError("Cannot remove your own administrator access")
    audit(g.db, g.user.branch_id, g.user.id, "user.update", "user", user.id,
          before=before, after={"full_name": user.full_name, "role": user.role, "active": user.active})
    g.db.commit()
    return jsonify({key: value for key, value in row_json(user).items() if key != "password_hash"})


@app.get("/api/audit")
@requires("users.manage")
def audit_route():
    rows = g.db.scalars(select(m.AuditLog).where(m.AuditLog.branch_id == g.user.branch_id).order_by(m.AuditLog.id.desc()).limit(1000)).all()
    return jsonify([row_json(row) for row in rows])


@app.get("/api/products/export")
@requires("products.read")
def export_products():
    from reporting import export_xlsx
    rows = list_products_data(limit=100000)
    if g.user.role == "cashier":
        return jsonify(error="Permission denied"), 403
    headers = ["ID", "SKU", "Name", "Category", "Brand", "Unit", "Stock", "Purchase price", "Selling price", "GST %", "HSN", "Minimum stock"]
    values = [[r.get("id"), r.get("sku"), r.get("name"), r.get("category_name"), r.get("brand"), r.get("unit"),
               r.get("current_stock"), r.get("purchase_price"), r.get("selling_price"), r.get("gst_rate"),
               r.get("hsn_code"), r.get("minimum_stock")] for r in rows]
    output = export_xlsx(headers, values, "Inventory")
    return send_file(io.BytesIO(output), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name="inventory.xlsx")


@app.post("/api/products/import")
@requires("products.write")
def import_products():
    uploaded = request.files.get("file")
    if not uploaded:
        raise ValueError("Choose a CSV or Excel file")
    name = uploaded.filename.lower()
    if name.endswith(".csv"):
        rows = list(csv.DictReader(io.StringIO(uploaded.read().decode("utf-8-sig"))))
    elif name.endswith(".xlsx"):
        from openpyxl import load_workbook
        workbook = load_workbook(uploaded, read_only=True, data_only=True)
        sheet = workbook.active
        iterator = sheet.values
        headers = [str(x or "").strip() for x in next(iterator)]
        rows = [dict(zip(headers, values)) for values in iterator]
    else:
        raise ValueError("Only .csv and .xlsx imports are supported")
    if len(rows) > 10000:
        raise ValueError("Import is limited to 10,000 rows per file")
    imported = []
    for idx, row in enumerate(rows, start=2):
        if not row.get("name") and not row.get("sku"):
            continue
        try:
            imported.append(upsert_product(row).id)
        except Exception as exc:
            raise ValueError(f"Row {idx}: {exc}") from exc
    g.db.commit()
    return jsonify(imported=len(imported), product_ids=imported), 201


@app.get("/invoices/<int:sale_id>/print")
@requires("sales.own")
def print_invoice(sale_id):
    from reporting import amount_in_words
    sale = g.db.get(m.Sale, sale_id)
    if not sale or sale.branch_id != g.user.branch_id or (g.user.role == "cashier" and sale.created_by != g.user.id):
        abort(404)
    settings = settings_row()
    logo = g.db.execute(select(m.Attachment).where(m.Attachment.owner_type == "settings",
                                                  m.Attachment.owner_id == settings.id).order_by(m.Attachment.id.desc()).limit(1)).scalar_one_or_none()
    when = sale.sale_date.replace(tzinfo=timezone.utc) if sale.sale_date.tzinfo is None else sale.sale_date
    invoice_date_ist = when.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p")
    return render_template("invoice.html", sale=sale, items=sale.items, payments=sale.payments,
                           settings=settings, customer=sale.customer,
                           balance_due=sale_json(sale)["balance_due"],
                           logo_url=f"/api/attachments/{logo.id}" if logo else None,
                           invoice_date_ist=invoice_date_ist,
                           amount_in_words=amount_in_words(sale.grand_total),
                           paper=request.args.get("paper", settings.default_paper),
                           units={u.id: u.symbol for u in g.db.scalars(select(m.Unit))})


@app.get("/invoices/<int:sale_id>/pdf")
@requires("sales.own")
def pdf_invoice(sale_id):
    from reporting import invoice_pdf
    sale = g.db.get(m.Sale, sale_id)
    if not sale or sale.branch_id != g.user.branch_id or (g.user.role == "cashier" and sale.created_by != g.user.id):
        abort(404)
    item_rows = [row_json(item, unit=g.db.get(m.Unit, item.unit_id).symbol) for item in sale.items]
    output = invoice_pdf(sale, item_rows, settings_row(), sale.payments)
    return send_file(io.BytesIO(output), mimetype="application/pdf", as_attachment=True,
                     download_name=f"{sale.invoice_no.replace('/', '-')}.pdf")


def attachment_owner(kind, owner_id):
    owner_id = int(owner_id)
    owners = {"product": m.Product, "purchase": m.Purchase, "expense": m.Expense, "settings": m.StoreSettings}
    model = owners.get(kind)
    if not model:
        raise ValueError("Unsupported attachment owner")
    obj = g.db.get(model, owner_id)
    if not obj or obj.branch_id != g.user.branch_id:
        abort(404)
    permission = {"product": "products.write", "purchase": "purchases.write",
                  "expense": "expenses.write", "settings": "users.manage"}[kind]
    if not can(g.user, permission):
        abort(403)
    return obj


@app.post("/api/attachments")
@requires("products.read")
def upload_attachment():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        raise ValueError("Choose a file")
    owner_type = request.form.get("owner_type") or ""
    owner = attachment_owner(owner_type, request.form.get("owner_id"))
    name = secure_filename(uploaded.filename)
    suffix = Path(name).suffix.lower()
    mime_for_suffix = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
                       ".jpeg": "image/jpeg", ".webp": "image/webp"}
    if suffix not in mime_for_suffix:
        raise ValueError("Supported attachments: PDF, PNG, JPEG, WebP")
    mime = mime_for_suffix[suffix]
    content = uploaded.read()
    if not content:
        raise ValueError("File is empty")
    sha = hashlib.sha256(content).hexdigest()
    key = f"{g.user.branch_id}/{owner_type}/{owner.id}/{uuid.uuid4().hex}{suffix}"
    bucket = os.environ.get("S3_BUCKET")
    if bucket:
        import boto3
        boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=content,
                                      ContentType=mime)
        stored_path = f"s3://{bucket}/{key}"
    else:
        stored_path = upload_reference(key)
        destination = resolve_local_upload(stored_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    record = m.Attachment(branch_id=g.user.branch_id, owner_type=owner_type, owner_id=owner.id,
                          original_name=name, stored_path=stored_path,
                          mime_type=mime,
                          sha256=sha, uploaded_by=g.user.id)
    g.db.add(record)
    g.db.flush()
    if owner_type == "product":
        owner.image_path = f"/api/attachments/{record.id}"
    elif owner_type == "expense":
        owner.receipt_path = f"/api/attachments/{record.id}"
    elif owner_type == "settings":
        owner.logo_path = stored_path
    audit(g.db, g.user.branch_id, g.user.id, "attachment.upload", "attachment", record.id,
          after={"owner_type": owner_type, "owner_id": owner.id, "sha256": sha})
    g.db.commit()
    return jsonify(row_json(record, url=f"/api/attachments/{record.id}")), 201


@app.get("/api/attachments")
@requires("products.read")
def list_attachments():
    owner_type = request.args.get("owner_type") or ""
    owner_id = request.args.get("owner_id")
    models_by_type = {"product": m.Product, "purchase": m.Purchase,
                      "expense": m.Expense, "settings": m.StoreSettings}
    model = models_by_type.get(owner_type)
    if not model or not owner_id:
        raise ValueError("Valid owner_type and owner_id are required")
    owner = g.db.get(model, int(owner_id))
    if not owner or owner.branch_id != g.user.branch_id:
        abort(404)
    if owner_type != "product" and g.user.role == "cashier":
        abort(403)
    rows = g.db.scalars(select(m.Attachment).where(m.Attachment.branch_id == g.user.branch_id,
                                                      m.Attachment.owner_type == owner_type,
                                                      m.Attachment.owner_id == owner.id).order_by(m.Attachment.id.desc())).all()
    return jsonify([row_json(row, url=f"/api/attachments/{row.id}") for row in rows])


@app.get("/api/attachments/<int:attachment_id>")
@requires("products.read")
def download_attachment(attachment_id):
    record = g.db.get(m.Attachment, attachment_id)
    if not record or record.branch_id != g.user.branch_id:
        abort(404)
    if record.owner_type in ("purchase", "expense", "settings") and g.user.role == "cashier":
        abort(403)
    if record.stored_path.startswith("s3://"):
        import boto3
        bucket_key = record.stored_path[5:]
        bucket, key = bucket_key.split("/", 1)
        obj = boto3.client("s3").get_object(Bucket=bucket, Key=key)
        stream = io.BytesIO(obj["Body"].read())
    else:
        stream = resolve_local_upload(record.stored_path)
    return send_file(stream, mimetype=record.mime_type, download_name=record.original_name)


if __name__ == "__main__":
    app.run(host=os.environ.get("APP_HOST", "127.0.0.1"), port=int(os.environ.get("APP_PORT", "8000")), debug=False)
