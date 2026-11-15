"""An invoice must record a deliberately selected payment type."""

from __future__ import annotations

from test_business_workflows import D, stock


def test_checkout_requires_payment_type_before_numbering_or_stock_change(api_client, make_product):
    product = make_product(opening_stock="3")
    payload = {
        "items": [{"product_id": product["id"], "quantity": "1"}],
        "payments": [{"method": "cash", "amount": "118"}],
    }
    for missing in (None, ""):
        attempt = dict(payload)
        if missing is not None:
            attempt["payment_type"] = missing
        rejected = api_client("POST", "/api/sales/checkout", attempt, expected=400)
        assert "payment type" in rejected["error"].lower()
        assert stock(api_client, product["id"]) == D("3")
        assert api_client("GET", "/api/sales") == []

    sale = api_client("POST", "/api/sales/checkout", {
        **payload, "payment_type": "cash",
    }, expected=201)
    assert sale["payment_type"] == "cash"
    assert int(sale["invoice_no"].rsplit("/", 1)[-1]) == 1
    assert stock(api_client, product["id"]) == D("2")


def test_checkout_rejects_payment_type_that_disagrees_with_payment_rows(api_client, make_product):
    product = make_product(opening_stock="3")
    items = [{"product_id": product["id"], "quantity": "1"}]
    for payment_type, payments in (
        ("cash", [{"method": "upi", "amount": "118"}]),
        ("split", [{"method": "cash", "amount": "118"}]),
        ("credit", [{"method": "cash", "amount": "118"}]),
    ):
        api_client("POST", "/api/sales/checkout", {
            "items": items, "payment_type": payment_type, "payments": payments,
        }, expected=400)
        assert stock(api_client, product["id"]) == D("3")
    assert api_client("GET", "/api/sales") == []


def test_customer_credit_and_split_payments_record_selected_type(api_client, make_product, make_customer):
    product = make_product(opening_stock="4")
    items = [{"product_id": product["id"], "quantity": "1"}]
    api_client("POST", "/api/sales/checkout", {
        "items": items, "payment_type": "credit", "payments": [],
    }, expected=400)
    assert stock(api_client, product["id"]) == D("4")

    customer = make_customer()
    credit = api_client("POST", "/api/sales/checkout", {
        "items": items, "customer_id": customer["id"],
        "payment_type": "credit", "payments": [],
    }, expected=201)
    assert credit["payment_type"] == "credit"
    assert credit["payments"] == []
    assert D(credit["balance_due"]) == D("118")
    printed = api_client("GET", f"/invoices/{credit['id']}/print").get_data(as_text=True)
    assert "Customer credit" in printed

    split = api_client("POST", "/api/sales/checkout", {
        "items": items, "payment_type": "split",
        "payments": [{"method": "cash", "amount": "50"},
                     {"method": "upi", "amount": "68"}],
    }, expected=201)
    assert split["payment_type"] == "split"
    assert D(split["paid_total"]) == D("118")
    assert {entry["method"] for entry in split["payments"]} == {"cash", "upi"}
    assert stock(api_client, product["id"]) == D("2")


def test_quotation_conversion_requires_payment_type(api_client, make_product):
    product = make_product(opening_stock="3")
    quote = api_client("POST", "/api/quotations", {
        "customer_name": "Contractor",
        "items": [{"product_id": product["id"], "quantity": "1"}],
    }, expected=201)
    api_client("PATCH", f"/api/quotations/{quote['id']}", {"status": "approved"})
    payment = {"payments": [{"method": "upi", "amount": "118"}]}
    rejected = api_client("POST", f"/api/quotations/{quote['id']}/convert", payment,
                          expected=400)
    assert "payment type" in rejected["error"].lower()
    assert stock(api_client, product["id"]) == D("3")
    assert api_client("GET", "/api/sales") == []

    sale = api_client("POST", f"/api/quotations/{quote['id']}/convert", {
        **payment, "payment_type": "upi",
    }, expected=201)
    assert sale["payment_type"] == "upi"
    assert int(sale["invoice_no"].rsplit("/", 1)[-1]) == 1
    assert stock(api_client, product["id"]) == D("2")
