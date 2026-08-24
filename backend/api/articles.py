"""
Articles API routes — CRUD, status transitions, and Markdown preview.

Delegates business logic to ArticleRepo (from csv_data_layer) and
MarkdownService (from markdown_service).

Uses StatsService.get_article_stats() for read counts.
"""
import re
from flask import Blueprint, request

from backend.api import success, validation_error, not_found, internal_error
from backend.api.auth_helpers import require_auth
from backend.storage.schemas import ArticleStatus


def _slugify(title: str) -> str:
    """Generate a URL-friendly slug from a title."""
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-") or "article"


def _resolve_tag_ids(tag_repo, tag_names: list[str] | None) -> list[str] | None:
    """Resolve tag names to tag UUIDs, creating tags if they don't exist."""
    if tag_names is None:
        return None
    ids = []
    for name in tag_names:
        name = name.strip()
        if not name:
            continue
        existing = tag_repo.get_by_name(name)
        if existing:
            ids.append(existing["id"])
        else:
            tag = tag_repo.create(name)
            ids.append(tag["id"])
    return ids


def _enrich_tags(article: dict, tag_repo) -> dict:
    """Resolve tag IDs in article to tag name strings for frontend."""
    result = dict(article)
    if "tags" in result and isinstance(result["tags"], list):
        # tags are already [{id, name}] from ArticleRepo
        result["tags"] = [t["name"] for t in result["tags"] if isinstance(t, dict)]
    else:
        result["tags"] = []
    return result


def _get_read_count(stats_service, article_id: str) -> int:
    """Get read count for an article using StatsService.get_article_stats()."""
    try:
        stats = stats_service.get_article_stats(article_id)
        if stats:
            return int(stats.get("totalViews", 0))
    except Exception:
        pass
    return 0


def create_articles_bp(api_prefix: str, article_repo, tag_repo,
                        markdown_service, stats_service, auth_service):
    """Create articles Blueprint with injected services."""
    bp = Blueprint("articles", __name__, url_prefix=f"{api_prefix}/articles")
    auth = require_auth(auth_service)

    # ------------------------------------------------------------------
    # GET /api/v1/articles — list articles
    # ------------------------------------------------------------------
    @bp.route("", methods=["GET"])
    def list_articles():
        page = request.args.get("page", 1, type=int)
        page_size = request.args.get("per_page", 10, type=int)
        tag = request.args.get("tag")
        order_by = request.args.get("order_by", "created_at")
        descending = request.args.get("descending", "true").lower() != "false"
        status = request.args.get("status")

        # Clamp pagination
        page = max(1, page)
        page_size = max(1, min(100, page_size))

        try:
            # Resolve tag name → UUID if needed
            tag_id = None
            if tag:
                # Check if it's already a UUID (32 hex chars)
                if re.match(r'^[0-9a-f]{32}$', tag.lower()):
                    tag_id = tag
                else:
                    resolved = tag_repo.get_by_name(tag)
                    if resolved:
                        tag_id = resolved["id"]

            result = article_repo.list_all(
                page=page,
                page_size=page_size,
                tag_id=tag_id,
                status=status,
                order_by=order_by,
                descending=descending,
            )
            # Convert tags to string list for each item
            enriched = []
            for item in result.get("items", []):
                enriched.append(_enrich_tags(item, tag_repo))
            return success({
                "articles": enriched,
                "total": result.get("total", 0),
                "page": result.get("page", page),
                "per_page": result.get("page_size", page_size),
            })
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # POST /api/v1/articles — create article (draft)
    # ------------------------------------------------------------------
    @bp.route("", methods=["POST"])
    @auth
    def create_article(current_user):
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        content = data.get("content", "")
        tag_names = data.get("tags") or data.get("tag_ids") or []

        if not title:
            return validation_error("title is required (1-200 characters)", code=40001)
        if len(title) > 200:
            return validation_error("title must be 1-200 characters")
        if not content:
            return validation_error("content is required", code=40001)

        try:
            tag_ids = _resolve_tag_ids(tag_repo, tag_names)
            slug = _slugify(title)

            article = article_repo.create(
                title=title,
                content=content,
                slug=slug,
                tag_ids=tag_ids,
            )
            # Enrich with stats and tag names
            article = _enrich_tags(article, tag_repo)
            article["read_count"] = 0
            return success(article, status=201)
        except ValueError as e:
            return validation_error(str(e))
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # GET /api/v1/articles/<id> — get article detail
    # ------------------------------------------------------------------
    @bp.route("/<article_id>", methods=["GET"])
    def get_article(article_id):
        try:
            article = article_repo.get_by_id(article_id)
            if article is None:
                return not_found(f"Article '{article_id}' not found")

            article = _enrich_tags(article, tag_repo)
            article["read_count"] = _get_read_count(stats_service, article_id)
            return success(article)
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # PUT /api/v1/articles/<id> — update article
    # ------------------------------------------------------------------
    @bp.route("/<article_id>", methods=["PUT"])
    @auth
    def update_article(current_user, article_id):
        data = request.get_json(silent=True) or {}
        title = data.get("title")
        content = data.get("content")
        tag_names = data.get("tags") or data.get("tag_ids")

        if title is not None:
            title = title.strip()
            if not title:
                return validation_error("title must not be empty")
            if len(title) > 200:
                return validation_error("title must be 1-200 characters")

        try:
            tag_ids = _resolve_tag_ids(tag_repo, tag_names) if tag_names is not None else None
            slug = _slugify(title) if title else None

            article = article_repo.update(
                article_id=article_id,
                title=title,
                content=content,
                slug=slug,
                tag_ids=tag_ids,
            )
            if article is None:
                return not_found(f"Article '{article_id}' not found")

            article = _enrich_tags(article, tag_repo)
            article["read_count"] = _get_read_count(stats_service, article_id)
            return success(article)
        except ValueError as e:
            return validation_error(str(e))
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # DELETE /api/v1/articles/<id> — soft delete
    # ------------------------------------------------------------------
    @bp.route("/<article_id>", methods=["DELETE"])
    @auth
    def delete_article(current_user, article_id):
        try:
            deleted = article_repo.delete(article_id)
            if not deleted:
                return not_found(f"Article '{article_id}' not found")
            return success({"message": "deleted", "id": article_id})
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # PATCH /api/v1/articles/<id>/publish — publish article
    # ------------------------------------------------------------------
    @bp.route("/<article_id>/publish", methods=["PATCH"])
    @auth
    def publish_article(current_user, article_id):
        try:
            article = article_repo.transition_status(
                article_id, ArticleStatus.PUBLISHED.value
            )
            if article is None:
                return not_found(f"Article '{article_id}' not found")
            article = _enrich_tags(article, tag_repo)
            article["read_count"] = _get_read_count(stats_service, article_id)
            return success(article)
        except ValueError as e:
            return validation_error(str(e))
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # PATCH /api/v1/articles/<id>/archive — archive article
    # ------------------------------------------------------------------
    @bp.route("/<article_id>/archive", methods=["PATCH"])
    @auth
    def archive_article(current_user, article_id):
        try:
            article = article_repo.transition_status(
                article_id, ArticleStatus.ARCHIVED.value
            )
            if article is None:
                return not_found(f"Article '{article_id}' not found")
            article = _enrich_tags(article, tag_repo)
            article["read_count"] = _get_read_count(stats_service, article_id)
            return success(article)
        except ValueError as e:
            return validation_error(str(e))
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # POST /api/v1/articles/preview — preview arbitrary markdown
    # ------------------------------------------------------------------
    @bp.route("/preview", methods=["POST"])
    def preview_any_markdown():
        data = request.get_json(silent=True) or {}
        content = data.get("content", "")

        if not content:
            return validation_error("content is required", code=40001)

        try:
            html = markdown_service.render_to_html(content)
            return success({"html": html})
        except Exception as e:
            return internal_error(str(e))

    return bp
