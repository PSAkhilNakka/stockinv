"""Real HTTP/API tests with an isolated SQLite database per test.

The production transaction code is exercised through Flask, including login,
CSRF, permissions, and committed database records. PostgreSQL concurrency and
locking still require a separate deployment test.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from werkzeug.security import generate_password_hash


APP_ROOT = Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "InitialTestPassword123!"


def sign_in(client, username, password):
    page = client.get("/login")
    assert page.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
    assert token, "Login form is missing its CSRF token"
    return client.post("/login", data={
        "username": username, "password": password, "csrf_token": token.group(1),
    })


@pytest.fixture(scope="session")
def modules():
    # Keep test data in the workspace because the host's global temp directory
    # can be outside the filesystem sandbox.
    database = APP_ROOT / "tests" / f".integration-{os.getpid()}.sqlite"
    os.environ["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    os.environ["SECRET_KEY"] = "integration-test-secret-key"
    sys.path.insert(0, str(APP_ROOT))
    models = importlib.import_module("models")
    application = importlib.import_module("app")
    application.app.config.update(TESTING=True)
    bootstrap_engine = models.engine
    yield application, models
    models.engine.dispose()
    bootstrap_engine.dispose()
    database.unlink(missing_ok=True)


@pytest.fixture
def api_client(modules):
    application, models = modules
    database = APP_ROOT / "tests" / f".integration-{os.getpid()}-{uuid4().hex}.sqlite"
    test_engine = models.make_engine(f"sqlite:///{database.as_posix()}")
    models.engine = test_engine
    models.SessionLocal.configure(bind=test_engine)
    models.init_db()
    with models.SessionLocal() as db:
        branch = models.bootstrap_store(db)
        db.add(models.User(
            branch_id=branch.id, username="admin", full_name="Test Administrator",
            role="admin", password_hash=generate_password_hash(ADMIN_PASSWORD),
        ))
        db.commit()

    client = application.app.test_client()
    login = sign_in(client, "admin", ADMIN_PASSWORD)
    assert login.status_code == 302
    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    csrf_token = bootstrap.get_json()["csrf_token"]

    def request(method, path, payload=None, expected=200, client_override=None, token=None):
        active_client = client_override or client
        headers = {"X-CSRF-Token": token or csrf_token}
        response = active_client.open(path, method=method, json=payload, headers=headers)
        assert response.status_code == expected, (
            f"{method} {path}: expected {expected}, got {response.status_code}: "
            f"{response.get_data(as_text=True)[:600]}"
        )
        return response.get_json() if response.is_json else response

    request.client = client
    request.models = models
    request.application = application
    request.csrf_token = csrf_token

    # A registered Maharashtra store makes the GST expectations deterministic.
    request("PUT", "/api/settings", {
        "store_name": "Test Hardware", "state": "Maharashtra", "state_code": "27",
        "gstin": "27ABCDE1234F1Z5", "is_gst_registered": True,
    })
    yield request
    test_engine.dispose()
    database.unlink(missing_ok=True)


@pytest.fixture
def make_product(api_client):
    def create(**overrides):
        payload = {
            "name": "Steel Bolt", "sku": "BOLT-001", "unit": "pcs",
            "purchase_price": "50", "selling_price": "100", "gst_rate": "18",
            "hsn_code": "7318", "opening_stock": "10", "minimum_stock": "2",
        }
        payload.update(overrides)
        return api_client("POST", "/api/products", payload, expected=201)
    return create


@pytest.fixture
def make_customer(api_client):
    def create(**overrides):
        payload = {"name": "Builder One", "phone": "9000000000", "state_code": "27"}
        payload.update(overrides)
        return api_client("POST", "/api/customers", payload, expected=201)
    return create


@pytest.fixture
def make_supplier(api_client):
    def create(**overrides):
        payload = {"name": "Fasteners Supplier", "state_code": "27"}
        payload.update(overrides)
        return api_client("POST", "/api/suppliers", payload, expected=201)
    return create
