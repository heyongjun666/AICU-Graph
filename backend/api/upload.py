"""
Upload API routes — image upload (multipart → mock URL).
"""
import os
import uuid
from flask import Blueprint, request
from werkzeug.utils import secure_filename

from backend.api import success, validation_error, internal_error
from backend.api.auth_helpers import require_auth
from backend.config import UPLOAD_DIR, UPLOAD_URL_PREFIX


def create_upload_bp(api_prefix: str, auth_service):
    """Create upload Blueprint with injected AuthService."""
    bp = Blueprint("upload", __name__, url_prefix=f"{api_prefix}/upload")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    auth = require_auth(auth_service)

    ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

    # ------------------------------------------------------------------
    # POST /api/v1/upload/image
    # ------------------------------------------------------------------
    @bp.route("/image", methods=["POST"])
    @auth
    def upload_image(current_user):
        if "file" not in request.files:
            return validation_error("file is required in multipart form", code=40001)

        file = request.files["file"]
        if not file or not file.filename:
            return validation_error("file is required", code=40001)

        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return validation_error(
                f"Unsupported file extension '{ext}'. "
                f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )

        try:
            filename = f"{uuid.uuid4().hex}{ext}"
            filepath = os.path.join(UPLOAD_DIR, filename)
            file.save(filepath)
            url = f"{UPLOAD_URL_PREFIX}/{filename}"
            return success({"url": url}, status=201)
        except Exception as e:
            return internal_error(str(e))

    return bp
