"""
Flask application factory — creates and configures the Flask app.

Assembles all blueprints with injected service instances.
"""
import os
import sys

# ── Ensure the backend package is on sys.path ──
_backend_dir = os.path.dirname(os.path.abspath(__file__))
if _backend_dir not in sys.path:
    sys.path.insert(0, os.path.dirname(_backend_dir))

from flask import Flask
from flask_cors import CORS

from backend.config import API_PREFIX, CORS_ORIGINS, DATA_DIR, UPLOAD_DIR

# ── CsvEngine & Repos (csv_data_layer) ──
from backend.storage.engine import CsvEngine
from backend.models.article_repo import ArticleRepo
from backend.models.tag_repo import TagRepo
from backend.models.stats_repo import StatsRepo
from backend.models.user_repo import UserRepo

# ── Markdown Service ──
from markdown_service import MarkdownService

# ── Auth Service ──
from backend.auth.service import AuthService

# ── Stats Service (advanced) ──
from backend.services.stats_service import StatsService

# ── API Blueprints ──
from backend.api.health import create_health_bp
from backend.api.auth import create_auth_bp
from backend.api.tags import create_tags_bp
from backend.api.articles import create_articles_bp
from backend.api.stats import create_stats_bp
from backend.api.upload import create_upload_bp
from backend.api.rss import create_rss_bp


def register_routes(app, engine, article_repo, tag_repo, user_repo,
                    stats_repo, stats_service_obj, markdown_service_obj,
                    auth_service_obj):
    """Register all API blueprints with the Flask app."""
    prefix = API_PREFIX

    # Health (no services needed)
    app.register_blueprint(create_health_bp(prefix))

    # Auth
    app.register_blueprint(create_auth_bp(prefix, auth_service_obj))

    # Tags (needs auth_service for write protection)
    app.register_blueprint(
        create_tags_bp(prefix, tag_repo, article_repo, auth_service_obj)
    )

    # Articles (needs auth_service + actual StatsService)
    app.register_blueprint(create_articles_bp(
        prefix, article_repo, tag_repo, markdown_service_obj, stats_service_obj,
        auth_service_obj,
    ))

    # Stats (needs both repos + both stats services)
    app.register_blueprint(create_stats_bp(
        prefix, stats_repo, stats_service_obj, article_repo, tag_repo,
    ))

    # Upload (needs auth_service)
    app.register_blueprint(create_upload_bp(prefix, auth_service_obj))

    # RSS
    app.register_blueprint(create_rss_bp(prefix, article_repo))


def create_app(data_dir: str | None = None) -> Flask:
    """Create and configure the Flask application.

    Args:
        data_dir: Override for CSV data directory (used in tests).

    Returns:
        Configured Flask app instance.
    """
    app = Flask(__name__)

    # ── CORS ──
    CORS(app, origins=CORS_ORIGINS, supports_credentials=True)

    # ── Data directory ──
    effective_data_dir = data_dir or DATA_DIR

    # ── Initialize CsvEngine ──
    engine = CsvEngine(effective_data_dir)

    # ── Initialize Repos ──
    tag_repo = TagRepo(engine)
    article_repo = ArticleRepo(engine, tag_repo)
    user_repo = UserRepo(engine)
    stats_repo = StatsRepo(engine)

    # ── Initialize Services ──
    markdown_service_obj = MarkdownService()
    auth_service_obj = AuthService(engine, user_repo)
    stats_service_obj = StatsService(engine, effective_data_dir)

    # ── Register routes ──
    register_routes(
        app,
        engine=engine,
        article_repo=article_repo,
        tag_repo=tag_repo,
        user_repo=user_repo,
        stats_repo=stats_repo,
        stats_service_obj=stats_service_obj,
        markdown_service_obj=markdown_service_obj,
        auth_service_obj=auth_service_obj,
    )

    # ── Store service references on app for test access ──
    app.extensions["engine"] = engine
    app.extensions["article_repo"] = article_repo
    app.extensions["tag_repo"] = tag_repo
    app.extensions["user_repo"] = user_repo
    app.extensions["stats_repo"] = stats_repo
    app.extensions["auth_service"] = auth_service_obj
    app.extensions["markdown_service"] = markdown_service_obj
    app.extensions["stats_service"] = stats_service_obj

    # ── Static file serving ──
    frontend_dir = os.environ.get(
        "FRONTEND_DIR",
        os.path.join(os.path.dirname(_backend_dir), "frontend"),
    )
    if os.path.isdir(frontend_dir):
        app.static_folder = frontend_dir
        app.static_url_path = ""

    return app


if __name__ == "__main__":
    app = create_app()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=8000, debug=True)
