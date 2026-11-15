"""Acceptance tests for posted stock, tax, credit, returns and permissions."""

from __future__ import annotations

from decimal import Decimal

from conftest import sign_in


def D(value):
    return Decimal(str(value))


def stock(api, product_id):
    products = api("GET", "/api/products")
    return D(next(item["current_stock"] for item in products if item["id"] == product_id))


def checkout(api, product_id, *, quantity="1", payments=None, **extra):
    payload = {"items": [{"product_id": product_id, "quantity": quantity}],
               "payment_type": "cash",
               "payments": payments if payments is not None else [{"method": "cash", "amount": str(D(quantity) * D("118"))}]}
    payload.update(extra)
    return api("POST", "/api/sales/checkout", payload, expected=201)


def test_opening_stock_purchase_confirmation_and_cost_snapshot(api_client, make_product, make_supplier):
    product = make_product()
    assert stock(api_client, product["id"]) == D("10")
    movements = api_client("GET", f"/api/products/{product['id']}/movements")
    assert [(row["movement_type"], D(row["quantity_delta"])) for row in movements] == [
        ("opening", D("10"))]

    supplier = make_supplier()
    draft = api_client("POST", "/api/purchases", {
        "supplier_id": supplier["id"], "supplier_invoice_no": "SUP-1",
        "items": [{"product_id": product["id"], "quantity": "100", "unit_price": "60", "gst_rate": "18"}],
    }, expected=201)
    assert stock(api_client, product["id"]) == D("10")  # Draft does not receive stock.
    posted = api_client("POST", f"/api/purchases/{draft['id']}/confirm", {}, expected=200)
    assert posted["status"] == "confirmed"
    assert D(posted["grand_total"]) == D("7080")
    assert stock(api_client, product["id"]) == D("110")
    assert D(api_client("GET", "/api/suppliers")[0]["outstanding_balance"]) == D("7080")

    # Retrying confirmation cannot post the same stock or supplier charge twice.
    api_client("POST", f"/api/purchases/{draft['id']}/confirm", {}, expected=200)
    assert stock(api_client, product["id"]) == D("110")
    movements = api_client("GET", f"/api/products/{product['id']}/movements")
    assert sum(row["movement_type"] == "purchase" for row in movements) == 1

    sale = checkout(api_client, product["id"], quantity="2", payments=[{"method": "cash", "amount": "236"}])
    assert stock(api_client, product["id"]) == D("108")
    expected_avg = (D("10") * D("50") + D("100") * D("60")) / D("110")
    assert abs(D(sale["items"][0]["unit_cost"]) - expected_avg) < D("0.00001")


def test_gst_intrastate_and_interstate_are_frozen_on_invoice(api_client, make_product):
    product = make_product(opening_stock="10")
    local = checkout(api_client, product["id"], quantity="1")
    assert D(local["taxable_total"]) == D("100")
    assert D(local["cgst_total"]) == D("9")
    assert D(local["sgst_total"]) == D("9")
    assert D(local["igst_total"]) == 0
    assert D(local["grand_total"]) == D("118")

    interstate = checkout(api_client, product["id"], quantity="1", place_of_supply_code="29")
    assert D(interstate["cgst_total"]) == 0
    assert D(interstate["sgst_total"]) == 0
    assert D(interstate["igst_total"]) == D("18")
    assert interstate["invoice_no"] != local["invoice_no"]


def test_credit_payment_expense_and_sale_return_reconcile(api_client, make_product, make_customer):
    product = make_product()
    customer = make_customer()
    sale = checkout(api_client, product["id"], quantity="2", payments=[],
                    payment_type="credit", customer_id=customer["id"])
    assert stock(api_client, product["id"]) == D("8")
    assert D(api_client("GET", "/api/customers")[0]["outstanding_balance"]) == D("236")

    payment = api_client("POST", f"/api/customers/{customer['id']}/payments", {
        "amount": "100", "method": "upi", "sale_id": sale["id"],
    }, expected=201)
    assert D(payment["outstanding_balance"]) == D("136")
    detail = api_client("GET", f"/api/sales/{sale['id']}")
    assert D(detail["outstanding"]) == D("136")

    before = api_client("GET", "/api/dashboard")
    api_client("POST", "/api/expenses", {"category": "Rent", "amount": "20"}, expected=201)
    after = api_client("GET", "/api/dashboard")
    assert D(after["gross_profit"]) == D(before["gross_profit"])
    assert D(after["net_profit"]) == D(before["net_profit"]) - D("20")

    returned = api_client("POST", f"/api/sales/{sale['id']}/return", {
        "items": [{"sale_item_id": sale["items"][0]["id"], "quantity": "1"}],
        "reason": "Wrong size", "refund_method": "upi",
    }, expected=201)
    assert D(returned["grand_total"]) == D("118")
    assert stock(api_client, product["id"]) == D("9")
    assert D(api_client("GET", "/api/customers")[0]["outstanding_balance"]) == D("18")
    movements = api_client("GET", f"/api/products/{product['id']}/movements")
    assert any(row["movement_type"] == "sale_return" and D(row["quantity_delta"]) == 1 for row in movements)


def test_purchase_price_edit_does_not_change_historical_cogs_or_profit(api_client, make_product):
    product = make_product()
    sale = checkout(api_client, product["id"])
    old_cost = D(sale["items"][0]["unit_cost"])
    old_profit = D(api_client("GET", "/api/dashboard")["gross_profit"])

    api_client("PATCH", f"/api/products/{product['id']}", {
        "purchase_price": "999", "price_change_reason": "New supplier quote",
    })
    detail = api_client("GET", f"/api/sales/{sale['id']}")
    assert D(detail["items"][0]["unit_cost"]) == old_cost
    assert D(api_client("GET", "/api/dashboard")["gross_profit"]) == old_profit
    history = api_client("GET", f"/api/products/{product['id']}/price-history")
    assert any(row["kind"] == "purchase" and D(row["new_price"]) == 999 for row in history)


def test_hold_quotation_conversion_and_idempotent_checkout(api_client, make_product):
    product = make_product(opening_stock="10")
    held = api_client("POST", "/api/held-bills", {
        "cart": [{"product_id": product["id"], "quantity": "1"}], "label": "Counter 2",
    }, expected=201)
    assert stock(api_client, product["id"]) == D("10")
    assert api_client("GET", "/api/held-bills")[0]["id"] == held["id"]

    api_client("DELETE", f"/api/held-bills/{held['id']}")
    first = checkout(api_client, product["id"], idempotency_key="test-held-bill-001")
    second = checkout(api_client, product["id"], idempotency_key="test-held-bill-001")
    assert first["id"] == second["id"]
    assert stock(api_client, product["id"]) == D("9")

    quote = api_client("POST", "/api/quotations", {
        "customer_name": "Builder", "items": [{"product_id": product["id"], "quantity": "2"}],
    }, expected=201)
    assert stock(api_client, product["id"]) == D("9")
    rejected = api_client("POST", f"/api/quotations/{quote['id']}/convert", {
        "payment_type": "cash",
        "payments": [{"method": "cash", "amount": "236"}],
    }, expected=400)
    assert "Approve" in rejected["error"]
    assert stock(api_client, product["id"]) == D("9")
    api_client("PATCH", f"/api/quotations/{quote['id']}", {"status": "approved"})
    converted = api_client("POST", f"/api/quotations/{quote['id']}/convert", {
        "payment_type": "cash",
        "payments": [{"method": "cash", "amount": "236"}],
    }, expected=201)
    converted_again = api_client("POST", f"/api/quotations/{quote['id']}/convert", {}, expected=201)
    assert converted_again["id"] == converted["id"]
    assert stock(api_client, product["id"]) == D("7")


def test_reports_export_and_cashier_cannot_read_profit(api_client, make_product):
    product = make_product()
    checkout(api_client, product["id"])
    for kind in ("sales", "inventory"):
        response = api_client("GET", f"/api/reports/{kind}?format=xlsx")
        assert response.status_code == 200
        assert response.data[:2] == b"PK"
        assert response.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    api_client("POST", "/api/users", {
        "username": "cashier", "full_name": "Counter Cashier", "role": "cashier",
        "password": "CashierTestPassword123!",
    }, expected=201)
    cashier = api_client.application.app.test_client()
    login = sign_in(cashier, "cashier", "CashierTestPassword123!")
    assert login.status_code == 302
    csrf = cashier.get("/api/bootstrap").get_json()["csrf_token"]
    api_client("GET", "/api/reports/profit", expected=403, client_override=cashier, token=csrf)
    api_client("GET", "/api/dashboard", expected=403, client_override=cashier, token=csrf)
    cashier_products = api_client("GET", "/api/products", client_override=cashier, token=csrf)
    assert "purchase_price" not in cashier_products[0]
    assert "weighted_avg_cost" not in cashier_products[0]


def test_unit_conversion_purchase_order_and_box_pricing(api_client, make_product, make_supplier):
    product = make_product(opening_stock="20", purchase_price="20", selling_price="40")
    supplier = make_supplier()
    units = api_client("GET", "/api/bootstrap")["units"]
    box_id = next(unit["id"] for unit in units if unit["symbol"] == "box")
    api_client("POST", f"/api/products/{product['id']}/conversions", {
        "unit_id": box_id, "factor_to_base": "50",
    })

    api_client("POST", "/api/purchase-orders", {
        "supplier_id": supplier["id"],
        "items": [{"product_id": product["id"], "unit_id": box_id,
                   "quantity": "2", "unit_price": "1500"}],
    }, expected=201)
    assert stock(api_client, product["id"]) == D("20")

    purchase = api_client("POST", "/api/purchases", {
        "supplier_id": supplier["id"],
        "items": [{"product_id": product["id"], "unit_id": box_id,
                   "quantity": "2", "unit_price": "1500"}],
    }, expected=201)
    api_client("POST", f"/api/purchases/{purchase['id']}/confirm", {})
    assert stock(api_client, product["id"]) == D("120")

    preview = api_client("POST", "/api/sales/preview", {
        "items": [{"product_id": product["id"], "unit_id": box_id,
                   "quantity": "1", "unit_price": "2000"}],
    })
    assert D(preview["grand_total"]) == D("2360")
    assert stock(api_client, product["id"]) == D("120")
    sale = api_client("POST", "/api/sales/checkout", {
        "payment_type": "upi",
        "items": [{"product_id": product["id"], "unit_id": box_id,
                   "quantity": "1", "unit_price": "2000"}],
        "payments": [{"method": "upi", "amount": "2360"}],
    }, expected=201)
    assert D(sale["items"][0]["base_quantity"]) == D("50")
    assert D(sale["grand_total"]) == D(preview["grand_total"])
    assert stock(api_client, product["id"]) == D("70")


def test_partial_purchase_return_and_supplier_payment_allocation(api_client, make_product, make_supplier):
    product = make_product(opening_stock="0")
    supplier = make_supplier()
    draft = api_client("POST", "/api/purchases", {
        "supplier_id": supplier["id"], "supplier_invoice_no": "SUP-RETURN-1",
        "items": [{"product_id": product["id"], "quantity": "10", "unit_price": "60"}],
    }, expected=201)
    purchase = api_client("POST", f"/api/purchases/{draft['id']}/confirm", {})
    assert stock(api_client, product["id"]) == D("10")

    api_client("POST", f"/api/suppliers/{supplier['id']}/payments", {
        "amount": "200", "method": "bank_transfer", "purchase_id": purchase["id"],
    }, expected=201)
    returned = api_client("POST", f"/api/purchases/{purchase['id']}/return", {
        "items": [{"purchase_item_id": purchase["items"][0]["id"], "quantity": "2"}],
        "reason": "Damaged in transit",
    }, expected=201)
    assert D(returned["grand_total"]) == D("141.60")
    assert stock(api_client, product["id"]) == D("8")
    assert D(api_client("GET", "/api/suppliers")[0]["outstanding_balance"]) == D("366.40")
    with api_client.models.SessionLocal() as db:
        allocations = db.query(api_client.models.PaymentAllocation).all()
        assert len(allocations) == 1
        assert allocations[0].purchase_id == purchase["id"]
        assert D(allocations[0].amount) == D("200")


def test_failed_checkout_is_atomic_and_does_not_use_invoice_number(api_client, make_product):
    product = make_product(opening_stock="1")
    response = api_client("POST", "/api/sales/checkout", {
        "payment_type": "cash",
        "items": [{"product_id": product["id"], "quantity": "2"}],
        "payments": [{"method": "cash", "amount": "236"}],
    }, expected=400)
    assert "Insufficient stock" in response["error"]
    assert stock(api_client, product["id"]) == D("1")
    assert api_client("GET", "/api/sales") == []
    sale = checkout(api_client, product["id"])
    assert int(sale["invoice_no"].rsplit("/", 1)[-1]) == 1


def test_sales_profit_and_gst_report_reconcile_with_return(api_client, make_product):
    product = make_product(opening_stock="10", purchase_price="50", selling_price="100")
    sale = checkout(api_client, product["id"], quantity="2")
    api_client("POST", f"/api/sales/{sale['id']}/return", {
        "items": [{"sale_item_id": sale["items"][0]["id"], "quantity": "1"}],
    }, expected=201)
    sales_report = api_client("GET", "/api/reports/sales")
    profit_report = api_client("GET", "/api/reports/profit")
    gst_report = api_client("GET", "/api/reports/gst")
    dashboard = api_client("GET", "/api/dashboard")
    assert D(sales_report["summary"]["net_sales"]) == D("118")
    assert D(sales_report["summary"]["gross_profit"]) == D("50")
    assert D(profit_report["summary"]["gross_profit"]) == D(dashboard["gross_profit"])
    assert D(gst_report["summary"]["gst_collected"]) == D("18")
    assert any(row["Flow"] == "Outward return" for row in gst_report["rows"] if isinstance(row, dict)) or \
        any(row[2] == "Outward return" for row in gst_report["rows"])
