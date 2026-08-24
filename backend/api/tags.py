"""
Tags API routes — CRUD + merge.

Delegates business logic to TagRepo (from csv_data_layer).
"""
from flask import Blueprint, request

from backend.api import success, validation_error, not_found, conflict, internal_error
from backend.api.auth_helpers import require_auth


def create_tags_bp(api_prefix: str, tag_repo, article_repo, auth_service):
    """Create tags Blueprint with injected TagRepo and ArticleRepo."""
    bp = Blueprint("tags", __name__, url_prefix=f"{api_prefix}/tags")
    auth = require_auth(auth_service)

    # ------------------------------------------------------------------
    # GET /api/v1/tags — list all tags
    # ------------------------------------------------------------------
    @bp.route("", methods=["GET"])
    def list_tags():
        try:
            tags = tag_repo.list_all()
            return success(tags)
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # POST /api/v1/tags — create tag
    # ------------------------------------------------------------------
    @bp.route("", methods=["POST"])
    @auth
    def create_tag(current_user):
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()

        if not name:
            return validation_error("name is required", code=40001)
        if len(name) > 50:
            return validation_error("name must be 1-50 characters")

        try:
            result = tag_repo.create(name)
            return success(result, status=201)
        except ValueError as e:
            return conflict(str(e))

    # ------------------------------------------------------------------
    # PUT /api/v1/tags/<id> — update tag
    # ------------------------------------------------------------------
    @bp.route("/<tag_id>", methods=["PUT"])
    @auth
    def update_tag(current_user, tag_id):
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()

        if not name:
            return validation_error("name is required", code=40001)
        if len(name) > 50:
            return validation_error("name must be 1-50 characters")

        try:
            result = tag_repo.update(tag_id, name)
            if result is None:
                return not_found(f"Tag '{tag_id}' not found")
            return success(result)
        except ValueError as e:
            return conflict(str(e))

    # ------------------------------------------------------------------
    # DELETE /api/v1/tags/<id> — delete tag
    # ------------------------------------------------------------------
    @bp.route("/<tag_id>", methods=["DELETE"])
    @auth
    def delete_tag(current_user, tag_id):
        try:
            # Step 1: Delete tag from tags table
            deleted = tag_repo.delete(tag_id)
            if not deleted:
                return not_found(f"Tag '{tag_id}' not found")

            # Step 2: Remove tag from all articles (API layer orchestration)
            article_repo.remove_tag(tag_id)

            return success({"message": "deleted", "id": tag_id})
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # POST /api/v1/tags/merge — merge source tag into target tag
    # ------------------------------------------------------------------
    @bp.route("/merge", methods=["POST"])
    @auth
    def merge_tags(current_user):
        data = request.get_json(silent=True) or {}
        source_id = data.get("source_id")
        target_id = data.get("target_id")

        if not source_id or not target_id:
            return validation_error("source_id and target_id are required", code=40001)
        if source_id == target_id:
            return validation_error("source_id and target_id must be different")

        try:
            source_tag = tag_repo.get_by_id(source_id)
            target_tag = tag_repo.get_by_id(target_id)
            if not source_tag:
                return not_found(f"Source tag '{source_id}' not found")
            if not target_tag:
                return not_found(f"Target tag '{target_id}' not found")

            # Delegate to TagRepo public method
            tag_repo.transfer_articles(source_id, target_id)

            # Remove source tag from all articles
            article_repo.remove_tag(source_id)

            # Delete source tag
            tag_repo.delete(source_id)

            return success({
                "source_id": source_id,
                "target_id": target_id,
                "message": "Tags merged",
            })
        except Exception as e:
            return internal_error(str(e))

    return bp
