"""
Auth routes — register, login, token refresh, password reset.

Delegates business logic to AuthService (from auth_service dependency).
"""
from flask import Blueprint, request

from backend.api import success, validation_error, unauthorized, not_found, conflict, internal_error


def create_auth_bp(api_prefix: str, auth_service):
    """Create auth Blueprint with injected AuthService."""
    bp = Blueprint("auth", __name__, url_prefix=f"{api_prefix}/auth")

    # ------------------------------------------------------------------
    # POST /api/v1/auth/register
    # ------------------------------------------------------------------
    @bp.route("/register", methods=["POST"])
    def register():
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        email = (data.get("email") or "").strip()
        password = data.get("password", "")

        if not username:
            return validation_error("username is required", code=40001)
        if not email:
            return validation_error("email is required", code=40001)
        if not password or len(password) < 6:
            return validation_error("password must be at least 6 characters")

        try:
            result = auth_service.register(username, email, password)
            return success(result, status=201)
        except ValueError as e:
            msg = str(e)
            if "already exists" in msg:
                return conflict(msg)
            return validation_error(msg)

    # ------------------------------------------------------------------
    # POST /api/v1/auth/login
    # ------------------------------------------------------------------
    @bp.route("/login", methods=["POST"])
    def login():
        data = request.get_json(silent=True) or {}
        username = (data.get("username") or "").strip()
        password = data.get("password", "")

        if not username or not password:
            return validation_error("username and password are required", code=40001)

        try:
            result = auth_service.login(username, password)
            return success(result)
        except ValueError:
            return unauthorized("invalid username or password", code=40101)

    # ------------------------------------------------------------------
    # POST /api/v1/auth/refresh
    # ------------------------------------------------------------------
    @bp.route("/refresh", methods=["POST"])
    def refresh():
        data = request.get_json(silent=True) or {}
        refresh_token = data.get("refresh_token", "")

        if not refresh_token:
            return validation_error("refresh_token is required", code=40001)

        try:
            result = auth_service.refresh_access_token(refresh_token)
            return success(result)
        except ValueError as e:
            return unauthorized(str(e), code=40101)

    # ------------------------------------------------------------------
    # POST /api/v1/auth/password-reset-request
    # ------------------------------------------------------------------
    @bp.route("/password-reset-request", methods=["POST"])
    def password_reset_request():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()

        if not email:
            return validation_error("email is required", code=40001)

        try:
            result = auth_service.request_password_reset(email)
            return success(result)
        except ValueError:
            return not_found("email not found")

    # ------------------------------------------------------------------
    # POST /api/v1/auth/password-reset-confirm
    # ------------------------------------------------------------------
    @bp.route("/password-reset-confirm", methods=["POST"])
    def password_reset_confirm():
        data = request.get_json(silent=True) or {}
        reset_token = data.get("reset_token", "")
        new_password = data.get("new_password", "")

        if not reset_token:
            return validation_error("reset_token is required", code=40001)
        if not new_password or len(new_password) < 6:
            return validation_error("new_password must be at least 6 characters")

        try:
            result = auth_service.confirm_password_reset(reset_token, new_password)
            return success(result)
        except ValueError as e:
            return validation_error(str(e))

    return bp
