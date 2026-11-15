"""Password, permission and request guards."""
from functools import wraps
from flask import abort, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash


ROLE_PERMISSIONS = {
    "admin": {"*"},
    "manager": {
        "dashboard", "products.read", "products.write", "inventory.adjust",
        "purchases.read", "purchases.write", "sales.read", "sales.own", "sales.write", "sales.return",
        "contacts.read", "contacts.write", "payments.write", "quotes.write",
        "expenses.read", "expenses.write", "reports.operational", "settings.read",
    },
    "cashier": {
        "products.read", "sales.write", "sales.own", "contacts.read",
        "contacts.write", "payments.write", "quotes.write",
    },
}


def can(user, permission):
    return bool(user and user.active and (
        "*" in ROLE_PERMISSIONS.get(user.role, set())
        or permission in ROLE_PERMISSIONS.get(user.role, set())
    ))


def requires(permission):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not getattr(g, "user", None):
                return jsonify(error="Authentication required"), 401
            if not can(g.user, permission):
                return jsonify(error="Permission denied"), 403
            return fn(*args, **kwargs)
        return wrapped
    return decorator


def csrf_guard():
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
        expected = session.get("csrf_token")
        actual = request.headers.get("X-CSRF-Token")
        if not expected or not actual or expected != actual:
            return jsonify(error="Invalid CSRF token"), 403


__all__ = ["check_password_hash", "generate_password_hash", "can", "requires", "csrf_guard"]
