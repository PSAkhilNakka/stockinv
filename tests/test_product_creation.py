"""Product creation in a fresh desktop database and identifier conflicts."""


def product_payload(sku, *, barcode="", brand=""):
    return {
        "name": f"Product {sku}",
        "sku": sku,
        "barcode": barcode,
        "category_id": "",
        "subcategory_id": "",
        "brand": brand,
        "unit": "pcs",
        "hsn_code": "",
        "purchase_price": 10,
        "selling_price": 20,
        "wholesale_price": 0,
        "mrp": 0,
        "gst_rate": 0,
        "opening_stock": 2,
        "min_stock": 0,
        "rack_location": "",
        "preferred_supplier_id": "",
        "description": "",
    }


def test_fresh_store_accepts_products_without_category_or_barcode(api_client):
    assert api_client("GET", "/api/bootstrap")["categories"] == []
    first = api_client("POST", "/api/products", product_payload("FIRST-001"), expected=201)
    second = api_client("POST", "/api/products", product_payload("SECOND-001"), expected=201)

    assert first["id"] != second["id"]
    assert first["category_id"] is None and second["category_id"] is None
    assert first["barcode"] is None and second["barcode"] is None
    assert {row["sku"] for row in api_client("GET", "/api/products")} == {"FIRST-001", "SECOND-001"}


def test_new_brand_does_not_flush_an_incomplete_product(api_client):
    product = api_client("POST", "/api/products", product_payload("BRAND-001", brand="Haritha Tools"), expected=201)

    assert product["brand"] == "Haritha Tools"
    assert product["unit"] == "pcs"
    assert product["current_stock"] == "2.000000"
    assert [brand["name"] for brand in api_client("GET", "/api/bootstrap")["brands"]] == ["Haritha Tools"]


def test_duplicate_sku_reports_field_and_rolls_back_brand(api_client):
    original = api_client("POST", "/api/products", product_payload("SAME-001"), expected=201)
    rejected = api_client("POST", "/api/products", product_payload("SAME-001", brand="Unsaved Brand"), expected=409)

    assert "SKU" in rejected["error"]
    assert "another product" in rejected["error"]
    assert [row["id"] for row in api_client("GET", "/api/products")] == [original["id"]]
    assert api_client("GET", "/api/bootstrap")["brands"] == []


def test_archived_product_keeps_sku_and_barcode_reserved(api_client):
    original = api_client("POST", "/api/products", product_payload("OLD-001", barcode="BAR-001"), expected=201)
    api_client("PATCH", f"/api/products/{original['id']}", {"archived": True})

    duplicate_sku = api_client("POST", "/api/products", product_payload("OLD-001"), expected=409)
    duplicate_barcode = api_client("POST", "/api/products", product_payload("NEW-001", barcode="BAR-001"), expected=409)

    assert "SKU" in duplicate_sku["error"] and "archived product" in duplicate_sku["error"]
    assert "Barcode" in duplicate_barcode["error"] and "archived product" in duplicate_barcode["error"]
    assert api_client("GET", "/api/products") == []
    archived = api_client("GET", "/api/products?include_archived=1")
    assert len(archived) == 1 and archived[0]["id"] == original["id"]


def test_other_unique_conflicts_still_return_409(api_client):
    api_client("POST", "/api/units", {"name": "Cartons", "symbol": "ctn", "decimal_places": 0}, expected=201)
    rejected = api_client("POST", "/api/units", {"name": "Cartons", "symbol": "ctn2", "decimal_places": 0}, expected=409)

    assert "already exists" in rejected["error"]


def test_nonunique_integrity_failure_is_logged_without_duplicate_claim(api_client, caplog):
    with caplog.at_level("ERROR", logger="app"):
        rejected = api_client("POST", "/api/products", product_payload("BAD-CATEGORY", brand="Unsaved Brand") | {"category_id": "99999"}, expected=500)

    assert "duplicate" not in rejected["error"].lower()
    assert any("FOREIGN KEY constraint failed" in record.message for record in caplog.records)
    assert api_client("GET", "/api/products") == []
    assert api_client("GET", "/api/bootstrap")["brands"] == []
