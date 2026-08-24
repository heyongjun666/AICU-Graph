"""
Health check endpoints — liveness, readiness, and general health.
"""
from flask import Blueprint
from datetime import datetime, timezone

from backend.api import success


def create_health_bp(api_prefix: str):
    """Create health check Blueprint."""
    bp = Blueprint("health", __name__, url_prefix=api_prefix)

    @bp.route("/health", methods=["GET"])
    def health():
        return success({
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })

    @bp.route("/liveness", methods=["GET"])
    def liveness():
        return success({"status": "alive"})

    @bp.route("/readiness", methods=["GET"])
    def readiness():
        return success({"status": "ready"})

    return bp
