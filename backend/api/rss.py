"""
RSS feed endpoint — generates RSS 2.0 XML from published articles.
"""
from flask import Blueprint, Response
from datetime import datetime, timezone

SITE_TITLE = "My Blog"
SITE_DESCRIPTION = "A Markdown-powered blog"
SITE_URL = "http://localhost:8000"


def create_rss_bp(api_prefix: str, article_repo):
    """Create RSS Blueprint with injected ArticleRepo."""
    bp = Blueprint("rss", __name__, url_prefix=api_prefix)

    @bp.route("/rss.xml", methods=["GET"])
    def rss_feed():
        try:
            result = article_repo.list_all(
                page=1,
                page_size=20,
                status="PUBLISHED",
                order_by="created_at",
                descending=True,
            )
            articles = result.get("items", [])
        except Exception:
            articles = []

        now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

        items_xml = ""
        for article in articles:
            art_id = article.get("id", "")
            title = _xml_escape(article.get("title", ""))
            link = f"{SITE_URL}/article/{art_id}"
            description = _xml_escape(article.get("content", "")[:500])
            pub_date = _format_rfc2822(article.get("created_at", ""))
            items_xml += f"""
  <item>
    <title>{title}</title>
    <link>{link}</link>
    <description>{description}</description>
    <pubDate>{pub_date}</pubDate>
    <guid>{link}</guid>
  </item>"""

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{_xml_escape(SITE_TITLE)}</title>
    <link>{SITE_URL}</link>
    <description>{_xml_escape(SITE_DESCRIPTION)}</description>
    <language>zh-CN</language>
    <lastBuildDate>{now}</lastBuildDate>
    {items_xml}
  </channel>
</rss>"""

        return Response(xml, mimetype="application/rss+xml")

    return bp


def _xml_escape(text: str) -> str:
    """Escape special XML characters."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    text = text.replace("'", "&apos;")
    return text


def _format_rfc2822(iso_str: str) -> str:
    """Convert ISO-8601 string to RFC 2822 format."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    except (ValueError, TypeError):
        return ""
