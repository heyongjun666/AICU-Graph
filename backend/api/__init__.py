"""
Unified API response helpers.

Success responses return data directly (no wrapper).
Error responses use {"code": <int>, "data": null, "message": <str>}
with the appropriate HTTP status code.

Error codes:
    40000  Validation error (generic)
    40001  Missing required field
    40002  Invalid field value
    40003  Resource not found
    40100  Authentication required
    40101  Invalid credentials
    40300  Permission denied
    50000  Internal server error
"""
from flask import jsonify
from typing import Any


def success(data: Any = None, status: int = 200):
    """Return a success response with data directly (no wrapper)."""
    if data is None:
        return "", status
    return jsonify(data), status


def error(message: str, code: int = 40000, status: int = 400):
    """Return an error response wrapped in {code, data, message}."""
    return jsonify({"code": code, "data": None, "message": message}), status


# ── Common error shortcuts ──

def validation_error(msg: str = "Validation error", code: int = 40000):
    """400 Bad Request — validation failure."""
    return error(msg, code=code, status=400)


def not_found(msg: str = "Resource not found", code: int = 40003):
    """404 Not Found."""
    return error(msg, code=code, status=404)


def unauthorized(msg: str = "Authentication required", code: int = 40100):
    """401 Unauthorized."""
    return error(msg, code=code, status=401)


def forbidden(msg: str = "Permission denied", code: int = 40300):
    """403 Forbidden."""
    return error(msg, code=code, status=403)


def conflict(msg: str = "Conflict", code: int = 40002):
    """409 Conflict (e.g. duplicate)."""
    return error(msg, code=code, status=409)


def internal_error(msg: str = "Internal server error"):
    """500 Internal Server Error."""
    return error(msg, code=50000, status=500)
