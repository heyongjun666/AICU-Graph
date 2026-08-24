"""
Stats API routes — record reads, article stats, site overview, ranking, trends.

Delegates business logic to StatsService (from stats_service) and
StatsRepo (from csv_data_layer) for complementary queries.

Frontend expects a single GET /api/v1/stats endpoint returning:
    {total_articles, total_tags, total_reads, top_articles, reads_by_tag}
"""
from flask import Blueprint, request

from backend.api import success, validation_error, not_found, internal_error


def create_stats_bp(api_prefix: str, stats_repo, stats_service, article_repo,
                    tag_repo):
    """Create stats Blueprint with injected services."""
    bp = Blueprint("stats", __name__, url_prefix=api_prefix)

    # ------------------------------------------------------------------
    # GET /api/v1/stats — unified site-wide stats (frontend expects this)
    # ------------------------------------------------------------------
    @bp.route("/stats", methods=["GET"])
    def stats_overview():
        try:
            # Get site stats from StatsService
            site_stats = stats_service.get_site_stats()
            total_articles = site_stats.get("totalArticles", 0)
            total_reads = site_stats.get("totalViews", 0)
            total_tags = site_stats.get("totalTags", 0)

            # Get ranking from StatsRepo
            ranking = stats_repo.get_ranking(limit=10)
            top_articles = []
            for r in ranking:
                article = article_repo.get_by_id(r.get("article_id", ""))
                title = article.get("title", "") if article else "Unknown"
                top_articles.append({
                    "id": r.get("article_id", ""),
                    "title": title,
                    "read_count": int(r.get("read_count", 0)),
                })

            # Get reads_by_tag from tag_repo (uses article_count per tag)
            all_tags = tag_repo.list_all()
            reads_by_tag = [
                {"tag": t["name"], "count": t["article_count"]}
                for t in all_tags if t.get("article_count", 0) > 0
            ]

            return success({
                "total_articles": total_articles,
                "total_tags": total_tags,
                "total_reads": total_reads,
                "top_articles": top_articles,
                "reads_by_tag": reads_by_tag,
            })
        except Exception as e:
            return internal_error(str(e))

            return success({
                "total_articles": total_articles,
                "total_tags": total_tags,
                "total_reads": total_reads,
                "top_articles": top_articles,
                "reads_by_tag": reads_by_tag,
            })
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # POST /api/v1/articles/<id>/read — record a read
    # ------------------------------------------------------------------
    @bp.route("/articles/<article_id>/read", methods=["POST"])
    def record_read(article_id):
        try:
            # Verify article exists
            article = article_repo.get_by_id(article_id)
            if article is None:
                return not_found(f"Article '{article_id}' not found")

            stats_repo.record_read(article_id)
            return success({"success": True}, status=201)
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # GET /api/v1/articles/<id>/stats — article-level stats
    # ------------------------------------------------------------------
    @bp.route("/articles/<article_id>/stats", methods=["GET"])
    def article_stats(article_id):
        try:
            article = article_repo.get_by_id(article_id)
            if article is None:
                return not_found(f"Article '{article_id}' not found")

            # Use StatsService for detailed stats
            svc_stats = stats_service.get_article_stats(article_id)
            total_views = svc_stats.get("totalViews", 0) if svc_stats else 0

            # Also get from StatsRepo for backward compat
            repo_stats = stats_repo.get_by_article(article_id)
            last_read = repo_stats.get("last_read_at", None) if repo_stats else None

            return success({
                "article_id": article_id,
                "read_count": total_views,
                "last_read_at": last_read,
                "title": article.get("title", ""),
            })
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # GET /api/v1/stats/overview — site-wide overview (legacy)
    # ------------------------------------------------------------------
    @bp.route("/stats/overview", methods=["GET"])
    def stats_overview_legacy():
        try:
            result = stats_repo.get_overview()
            return success(result)
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # GET /api/v1/stats/ranking — read ranking (legacy)
    # ------------------------------------------------------------------
    @bp.route("/stats/ranking", methods=["GET"])
    def stats_ranking():
        limit = request.args.get("limit", 10, type=int)
        limit = max(1, min(100, limit))

        try:
            ranking = stats_repo.get_ranking(limit=limit)
            return success({"ranking": ranking})
        except Exception as e:
            return internal_error(str(e))

    # ------------------------------------------------------------------
    # GET /api/v1/stats/trends — time-series trends
    # ------------------------------------------------------------------
    @bp.route("/stats/trends", methods=["GET"])
    def stats_trends():
        start_date = request.args.get("start_date", "")
        end_date = request.args.get("end_date", "")
        granularity = request.args.get("granularity", "day")
        article_id = request.args.get("article_id")

        if not start_date or not end_date:
            return validation_error("start_date and end_date are required")

        try:
            trends = stats_service.get_trends(
                start_date=start_date,
                end_date=end_date,
                granularity=granularity,
                article_id=article_id,
            )
            return success(trends)
        except ValueError as e:
            return validation_error(str(e))
        except Exception as e:
            return internal_error(str(e))

    return bp
