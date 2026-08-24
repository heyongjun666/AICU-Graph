"""
Auth helper — decorator and utility for JWT authentication on API endpoints.
"""
from functools import wraps
from flask import request

from backend.api import unauthorized


def require_auth(auth_service):
    """Decorator that requires a valid JWT Bearer token.

    Injects ``current_user`` as a kwarg into the decorated view function.

    Usage::

        @bp.route("/articles", methods=["POST"])
        @require_auth(auth_service)
        def create_article(current_user, ...):
            ...
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = auth_service.authenticate(request)
            if user is None:
                return unauthorized(
                    "Authentication required — provide a valid Bearer token",
                    code=40100,
                )
            kwargs["current_user"] = user
            return f(*args, **kwargs)
        return wrapper
    return decorator
