"""Focused tests for document lifecycle, effective tax and stored files."""

from __future__ import annotations

import io
from importlib.util import find_spec
import shutil
import struct
import tempfile
import zlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from conftest import sign_in
from test_business_workflows import D, checkout, stock


def test_revised_quotation_posts_only_revised_items(api_client, make_product):
    product = make_product(opening_stock="10")
    original = api_client("POST", "/api/quotations", {
        "customer_name": "Contractor", "items": [{"product_id": product["id"], "quantity": "1"}],
    }, expected=201)
    assert D(original["grand_total"]) == D("118")
    assert stock(api_client, product["id"]) == D("10")

    revised = api_client("PATCH", f"/api/quotations/{original['id']}", {
        "items": [{"product_id": product["id"], "quantity": "3"}],
        "notes": "Three items requested",
    })
    assert revised["number"] == original["number"]
    assert len(revised["items"]) == 1
    assert D(revised["items"][0]["quantity"]) == D("3")
    assert D(revised["grand_total"]) == D("354")
    assert stock(api_client, product["id"]) == D("10")

    api_client("PATCH", f"/api/quotations/{original['id']}", {"status": "approved"})
    sale = api_client("POST", f"/api/quotations/{original['id']}/convert", {
        "payment_type": "cash",
        "payments": [{"method": "cash", "amount": "354"}],
    }, expected=201)
    assert D(sale["items"][0]["quantity"]) == D("3")
    assert stock(api_client, product["id"]) == D("7")
    api_client("PATCH", f"/api/quotations/{original['id']}", {
        "items": [{"product_id": product["id"], "quantity": "1"}],
    }, expected=400)
    assert stock(api_client, product["id"]) == D("7")


def test_cashier_cannot_approve_quote_but_can_convert_approved_quote(api_client, make_product):
    product = make_product(opening_stock="4")
    quote = api_client("POST", "/api/quotations", {
        "customer_name": "Contractor", "items": [{"product_id": product["id"], "quantity": "1"}],
    }, expected=201)
    api_client("POST", "/api/users", {
        "username": "quote_cashier", "full_name": "Quote Cashier", "role": "cashier",
        "password": "CashierTestPassword123!",
    }, expected=201)
    cashier = api_client.application.app.test_client()
    assert sign_in(cashier, "quote_cashier", "CashierTestPassword123!").status_code == 302
    cashier_token = cashier.get("/api/bootstrap").get_json()["csrf_token"]
    denied = api_client("PATCH", f"/api/quotations/{quote['id']}", {"status": "approved"},
                        expected=403, client_override=cashier, token=cashier_token)
    assert "approval" in denied["error"].lower()
    api_client("PATCH", f"/api/quotations/{quote['id']}", {"status": "approved"})
    converted = api_client("POST", f"/api/quotations/{quote['id']}/convert", {
        "payment_type": "cash",
        "payments": [{"method": "cash", "amount": "118"}],
    }, expected=201, client_override=cashier, token=cashier_token)
    assert D(converted["grand_total"]) == D("118")
    assert stock(api_client, product["id"]) == D("3")


def test_purchase_order_receipt_is_exactly_once(api_client, make_product, make_supplier):
    product = make_product(opening_stock="0")
    supplier = make_supplier()
    order = api_client("POST", "/api/purchase-orders", {
        "supplier_id": supplier["id"],
        "items": [{"product_id": product["id"], "quantity": "5", "unit_price": "40"}],
    }, expected=201)
    assert stock(api_client, product["id"]) == 0
    first = api_client("POST", f"/api/purchase-orders/{order['id']}/receive", {
        "supplier_invoice_no": "PO-RECEIPT-1",
    }, expected=201)
    assert first["status"] == "confirmed"
    assert stock(api_client, product["id"]) == D("5")
    second = api_client("POST", f"/api/purchase-orders/{order['id']}/receive", {}, expected=200)
    assert second["id"] == first["id"]
    assert stock(api_client, product["id"]) == D("5")
    assert D(api_client("GET", "/api/suppliers")[0]["outstanding_balance"]) == D("236")
    movements = api_client("GET", f"/api/products/{product['id']}/movements")
    assert sum(row["movement_type"] == "purchase" for row in movements) == 1


def test_purchase_order_discount_survives_receipt(api_client, make_product, make_supplier):
    product = make_product(opening_stock="0")
    supplier = make_supplier()
    order = api_client("POST", "/api/purchase-orders", {
        "supplier_id": supplier["id"],
        "items": [{"product_id": product["id"], "quantity": "10", "unit_price": "60",
                   "discount_percent": "10"}],
    }, expected=201)
    assert D(order["items"][0]["discount_percent"]) == D("10")
    assert D(order["grand_total"]) == D("637.20")
    purchase = api_client("POST", f"/api/purchase-orders/{order['id']}/receive", {
        "supplier_invoice_no": "DISCOUNTED-PO-1",
    }, expected=201)
    assert D(purchase["items"][0]["discount"]) == D("60")
    assert D(purchase["items"][0]["taxable_value"]) == D("540")
    assert D(purchase["grand_total"]) == D(order["grand_total"])
    assert stock(api_client, product["id"]) == D("10")
    balance = next(row for row in api_client("GET", "/api/products") if row["id"] == product["id"])
    assert D(balance["weighted_avg_cost"]) == D("54")


def test_quote_exact_discount_percent_survives_approval_and_conversion(api_client, make_product):
    product = make_product(opening_stock="10", selling_price="99.99")
    quote = api_client("POST", "/api/quotations", {
        "customer_name": "Contractor",
        "items": [{"product_id": product["id"], "quantity": "3",
                   "discount_percent": "12.3456"}],
    }, expected=201)
    assert D(quote["items"][0]["discount_percent"]) == D("12.3456")
    assert D(quote["items"][0]["discount"]) == D("37.03")
    assert D(quote["grand_total"]) == D("310.27")
    api_client("PATCH", f"/api/quotations/{quote['id']}", {"status": "approved"})
    sale = api_client("POST", f"/api/quotations/{quote['id']}/convert", {
        "payment_type": "cash",
        "payments": [{"method": "cash", "amount": "310.27"}],
    }, expected=201)
    assert D(sale["items"][0]["discount"]) == D(quote["items"][0]["discount"])
    assert D(sale["grand_total"]) == D(quote["grand_total"])
    assert stock(api_client, product["id"]) == D("7")


def test_effective_dated_product_tax_rate_overrides_future_and_hsn(api_client, make_product):
    product = make_product(opening_stock="10", gst_rate="18", hsn_code="7318")
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    for payload in (
        {"hsn_code": "7318", "rate": "5", "effective_from": yesterday},
        {"hsn_code": "7318", "product_id": product["id"], "rate": "12", "effective_from": yesterday},
        {"hsn_code": "7318", "product_id": product["id"], "rate": "28", "effective_from": tomorrow},
    ):
        api_client("POST", "/api/tax-rates", payload, expected=201)
    preview = api_client("POST", "/api/sales/preview", {
        "items": [{"product_id": product["id"], "quantity": "1"}],
    })
    assert D(preview["grand_total"]) == D("112")
    assert D(preview["cgst_total"]) == D("6")
    sale = checkout(api_client, product["id"], payments=[{"method": "cash", "amount": "112"}])
    assert D(sale["items"][0]["gst_rate"]) == D("12")
    assert D(sale["grand_total"]) == D("112")


def test_invoice_quote_challan_and_report_pdf_endpoints(api_client, make_product):
    product = make_product(opening_stock="10")
    quote = api_client("POST", "/api/quotations", {
        "customer_name": "Contractor", "items": [{"product_id": product["id"], "quantity": "1"}],
    }, expected=201)
    sale = checkout(api_client, product["id"])
    challan = api_client("POST", f"/api/sales/{sale['id']}/challan", {
        "notes": "Deliver to building site",
    }, expected=201)
    assert api_client("GET", f"/api/sales/{sale['id']}/challan")["id"] == challan["id"]
    assert api_client("POST", f"/api/sales/{sale['id']}/challan", {}, expected=200)["id"] == challan["id"]
    assert stock(api_client, product["id"]) == D("9")

    for path in (
        f"/invoices/{sale['id']}/pdf",
        f"/quotations/{quote['id']}/pdf",
        f"/challans/{challan['id']}/pdf",
        "/api/reports/sales?format=pdf",
    ):
        pdf = api_client("GET", path)
        assert pdf.mimetype == "application/pdf"
        assert pdf.data.startswith(b"%PDF-"), path
    printable = api_client("GET", f"/challans/{challan['id']}/print")
    assert "Delivery Challan" in printable.get_data(as_text=True)
    assert "Steel Bolt" in printable.get_data(as_text=True)


def test_attachment_upload_list_download_and_product_link(api_client, make_product, monkeypatch):
    product = make_product()
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as upload_dir:
        monkeypatch.setenv("UPLOAD_DIR", upload_dir)
        content = b"%PDF-1.4\nTest supplier specification\n%%EOF\n"
        upload = api_client.client.post("/api/attachments", data={
            "owner_type": "product", "owner_id": str(product["id"]),
            "file": (io.BytesIO(content), "specification.pdf"),
        }, headers={"X-CSRF-Token": api_client.csrf_token}, content_type="multipart/form-data")
        assert upload.status_code == 201, upload.get_data(as_text=True)
        record = upload.get_json()
        assert record["original_name"] == "specification.pdf"
        assert record["mime_type"] == "application/pdf"
        assert record["stored_path"].startswith("upload://")
        assert upload_dir not in record["stored_path"]
        listed = api_client("GET", f"/api/attachments?owner_type=product&owner_id={product['id']}")
        assert len(listed) == 1 and listed[0]["id"] == record["id"]
        downloaded = api_client("GET", record["url"])
        assert downloaded.data == content
        downloaded.close()  # Release send_file's handle before Windows removes the test directory.
        updated_product = next(row for row in api_client("GET", "/api/products") if row["id"] == product["id"])
        assert updated_product["image_path"] == record["url"]


def _sample_png():
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    pixels = b"\x00\xff\x00\x00\x00\xff\x00\x00\x00\x00\xff\xff\xff\x00"
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels))
            + chunk(b"IEND", b""))


def test_local_logo_download_and_invoice_pdf_survive_upload_directory_move(api_client, make_product, monkeypatch):
    product = make_product(opening_stock="10")
    sale = checkout(api_client, product["id"])
    settings = api_client("GET", "/api/settings")
    image = _sample_png()

    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
        original = Path(root) / "first-device" / "uploads"
        restored = Path(root) / "second-device" / "uploads"
        monkeypatch.setenv("UPLOAD_DIR", str(original))
        response = api_client.client.post("/api/attachments", data={
            "owner_type": "settings", "owner_id": str(settings["id"]),
            "file": (io.BytesIO(image), "logo.png"),
        }, headers={"X-CSRF-Token": api_client.csrf_token}, content_type="multipart/form-data")
        assert response.status_code == 201, response.get_data(as_text=True)
        record = response.get_json()
        assert record["stored_path"].startswith("upload://")
        assert str(original) not in record["stored_path"]

        restored.parent.mkdir(parents=True)
        shutil.move(str(original), str(restored))
        monkeypatch.setenv("UPLOAD_DIR", str(restored))

        downloaded = api_client("GET", record["url"])
        assert downloaded.data == image
        downloaded.close()
        pdf = api_client("GET", f"/invoices/{sale['id']}/pdf")
        assert pdf.data.startswith(b"%PDF-")
        if find_spec("reportlab") is not None:
            assert b"/Subtype /Image" in pdf.data


def test_legacy_absolute_logo_path_resolves_after_restore(api_client, make_product, monkeypatch):
    product = make_product(opening_stock="10")
    sale = checkout(api_client, product["id"])
    settings = api_client("GET", "/api/settings")
    image = _sample_png()

    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
        original = Path(root) / "first-device" / "uploads"
        restored = Path(root) / "second-device" / "uploads"
        monkeypatch.setenv("UPLOAD_DIR", str(original))
        response = api_client.client.post("/api/attachments", data={
            "owner_type": "settings", "owner_id": str(settings["id"]),
            "file": (io.BytesIO(image), "logo.png"),
        }, headers={"X-CSRF-Token": api_client.csrf_token}, content_type="multipart/form-data")
        assert response.status_code == 201, response.get_data(as_text=True)
        record = response.get_json()
        legacy_path = original.joinpath(*record["stored_path"].removeprefix("upload://").split("/"))
        with api_client.models.SessionLocal() as db:
            db.get(api_client.models.Attachment, record["id"]).stored_path = str(legacy_path)
            db.get(api_client.models.StoreSettings, settings["id"]).logo_path = str(legacy_path)
            db.commit()

        direct = api_client("GET", record["url"])
        assert direct.data == image
        direct.close()
        restored.parent.mkdir(parents=True)
        shutil.move(str(original), str(restored))
        monkeypatch.setenv("UPLOAD_DIR", str(restored))
        moved = api_client("GET", record["url"])
        assert moved.data == image
        moved.close()
        pdf = api_client("GET", f"/invoices/{sale['id']}/pdf")
        assert pdf.data.startswith(b"%PDF-")
        if find_spec("reportlab") is not None:
            assert b"/Subtype /Image" in pdf.data


def test_public_pdf_share_can_be_revoked_and_expires(api_client, make_product):
    product = make_product(opening_stock="10")
    sale = checkout(api_client, product["id"])
    quote = api_client("POST", "/api/quotations", {
        "customer_name": "Contractor", "items": [{"product_id": product["id"], "quantity": "1"}],
    }, expected=201)
    anonymous = api_client.application.app.test_client()

    sale_link = api_client("POST", f"/api/sales/{sale['id']}/share", {}, expected=200)
    sale_path = urlsplit(sale_link["url"]).path
    sale_pdf = anonymous.get(sale_path)
    assert sale_pdf.status_code == 200
    assert sale_pdf.mimetype == "application/pdf"
    assert sale_pdf.data.startswith(b"%PDF-")
    sale_pdf.close()
    api_client("POST", f"/api/share-links/{sale_link['id']}/revoke", {}, expected=200)
    assert anonymous.get(sale_path).status_code == 404

    quote_link = api_client("POST", f"/api/quotations/{quote['id']}/share", {}, expected=200)
    quote_path = urlsplit(quote_link["url"]).path
    quote_pdf = anonymous.get(quote_path)
    assert quote_pdf.status_code == 200
    assert quote_pdf.data.startswith(b"%PDF-")
    quote_pdf.close()
    with api_client.models.SessionLocal() as db:
        link_record = db.get(api_client.models.ShareLink, quote_link["id"])
        assert link_record.token_hash not in quote_path
        link_record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert anonymous.get(quote_path).status_code == 404


def test_sale_credit_note_and_purchase_debit_note_documents(api_client, make_product, make_supplier):
    product = make_product(opening_stock="10")
    sale = checkout(api_client, product["id"], quantity="2")
    credit_note = api_client("POST", f"/api/sales/{sale['id']}/return", {
        "items": [{"sale_item_id": sale["items"][0]["id"], "quantity": "1"}],
        "reason": "Wrong size",
    }, expected=201)
    credit_pdf = api_client("GET", f"/credit-notes/{credit_note['id']}/pdf")
    assert credit_pdf.mimetype == "application/pdf"
    assert credit_pdf.data.startswith(b"%PDF-")
    credit_print = api_client("GET", f"/credit-notes/{credit_note['id']}/print")
    text = credit_print.get_data(as_text=True)
    assert "Credit Note" in text and sale["invoice_no"] in text and "Steel Bolt" in text

    supplier = make_supplier()
    draft = api_client("POST", "/api/purchases", {
        "supplier_id": supplier["id"], "supplier_invoice_no": "SUP-DEBIT-001",
        "items": [{"product_id": product["id"], "quantity": "5", "unit_price": "60"}],
    }, expected=201)
    purchase = api_client("POST", f"/api/purchases/{draft['id']}/confirm", {})
    debit_note = api_client("POST", f"/api/purchases/{purchase['id']}/return", {
        "items": [{"purchase_item_id": purchase["items"][0]["id"], "quantity": "1"}],
        "reason": "Damaged stock",
    }, expected=201)
    debit_pdf = api_client("GET", f"/debit-notes/{debit_note['id']}/pdf")
    assert debit_pdf.mimetype == "application/pdf"
    assert debit_pdf.data.startswith(b"%PDF-")
    debit_print = api_client("GET", f"/debit-notes/{debit_note['id']}/print")
    text = debit_print.get_data(as_text=True)
    assert "Debit Note" in text and "SUP-DEBIT-001" in text and "Steel Bolt" in text


def test_taxable_retail_bill_rejected_without_stock_change(api_client, make_product):
    product = make_product(opening_stock="10", gst_rate="18")
    taxable_retail = api_client("POST", "/api/sales/checkout", {
        "payment_type": "cash",
        "invoice_type": "retail", "items": [{"product_id": product["id"], "quantity": "1"}],
        "payments": [{"method": "cash", "amount": "118"}],
    }, expected=400)
    assert "tax invoice" in taxable_retail["error"].lower()
    assert stock(api_client, product["id"]) == D("10")
    assert api_client("GET", "/api/sales") == []

    non_taxable = make_product(name="Non taxable item", sku="ZERO-001", gst_rate="0", opening_stock="1")
    retail = api_client("POST", "/api/sales/checkout", {
        "payment_type": "cash",
        "invoice_type": "retail", "items": [{"product_id": non_taxable["id"], "quantity": "1"}],
        "payments": [{"method": "cash", "amount": "100"}],
    }, expected=201)
    assert D(retail["grand_total"]) == D("100")
    assert retail["invoice_type"] == "retail"


def test_taxable_gst_sale_requires_product_hsn(api_client, make_product):
    product = make_product(opening_stock="2", gst_rate="18", hsn_code="")
    response = api_client("POST", "/api/sales/checkout", {
        "payment_type": "cash",
        "items": [{"product_id": product["id"], "quantity": "1"}],
        "payments": [{"method": "cash", "amount": "118"}],
    }, expected=400)
    assert "HSN" in response["error"]
    assert stock(api_client, product["id"]) == D("2")
    assert api_client("GET", "/api/sales") == []


def test_gstin_format_and_state_code_validation(api_client, make_customer):
    malformed = api_client("PUT", "/api/settings", {"gstin": "BAD"}, expected=400)
    assert "GSTIN" in malformed["error"]
    mismatch = api_client("PUT", "/api/settings", {"gstin": "29ABCDE1234F1Z5"}, expected=400)
    assert "state code" in mismatch["error"].lower()
    assert api_client("GET", "/api/settings")["gstin"] == "27ABCDE1234F1Z5"

    api_client("POST", "/api/customers", {
        "name": "Invalid GSTIN", "state_code": "27", "gstin": "27BAD",
    }, expected=400)
    api_client("POST", "/api/customers", {
        "name": "Wrong State", "state_code": "27", "gstin": "29ABCDE1234F1Z5",
    }, expected=400)
    customer = make_customer(gstin="27abcde1234f1z5")
    assert customer["gstin"] == "27ABCDE1234F1Z5"
