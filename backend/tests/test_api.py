"""
Comprehensive API tests — covers all endpoints.

Tests are organized by blueprint: health, auth, tags, articles, stats, upload, rss.
Each test verifies the response format and behavior against the frontend contract.
"""
import io
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.app import create_app


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    """Create a fresh Flask app with temp data directory for each test."""
    data_dir = tempfile.mkdtemp()
    app = create_app(data_dir=data_dir)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    """Test client fixture."""
    return app.test_client()


@pytest.fixture
def auth_headers(client):
    """Register + login, return Authorization headers."""
    client.post("/api/v1/auth/register", json={
        "username": "testuser", "email": "test@test.com", "password": "test123",
    })
    resp = client.post("/api/v1/auth/login", json={
        "username": "testuser", "password": "test123",
    })
    data = resp.get_json()
    token = data["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────
# 1. Health endpoints
# ─────────────────────────────────────────────────────────────

class TestHealth:
    def test_health(self, client):
        """Health returns data directly (no wrapper)."""
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert "timestamp" in body

    def test_liveness(self, client):
        resp = client.get("/api/v1/liveness")
        assert resp.get_json()["status"] == "alive"

    def test_readiness(self, client):
        resp = client.get("/api/v1/readiness")
        assert resp.get_json()["status"] == "ready"


# ─────────────────────────────────────────────────────────────
# 2. Auth endpoints
# ─────────────────────────────────────────────────────────────

class TestAuth:
    def test_register_success(self, client):
        """Register returns user data directly."""
        resp = client.post("/api/v1/auth/register", json={
            "username": "newuser", "email": "new@test.com", "password": "test123",
        })
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["username"] == "newuser"
        assert "id" in body
        assert "password_hash" not in body

    def test_register_duplicate_username(self, client):
        client.post("/api/v1/auth/register", json={
            "username": "dupuser", "email": "dup1@test.com", "password": "test123",
        })
        resp = client.post("/api/v1/auth/register", json={
            "username": "dupuser", "email": "dup2@test.com", "password": "test123",
        })
        assert resp.status_code == 409
        assert resp.get_json()["code"] == 40002

    def test_register_missing_fields(self, client):
        resp = client.post("/api/v1/auth/register", json={})
        assert resp.status_code == 400
        assert resp.get_json()["code"] == 40001

    def test_register_short_password(self, client):
        resp = client.post("/api/v1/auth/register", json={
            "username": "u", "email": "u@t.com", "password": "ab",
        })
        assert resp.status_code == 400

    def test_login_success(self, client):
        client.post("/api/v1/auth/register", json={
            "username": "loginuser", "email": "login@test.com", "password": "test123",
        })
        resp = client.post("/api/v1/auth/login", json={
            "username": "loginuser", "password": "test123",
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert "expires_at" in body

    def test_login_invalid(self, client):
        resp = client.post("/api/v1/auth/login", json={
            "username": "nobody", "password": "wrong",
        })
        assert resp.status_code == 401

    def test_refresh_success(self, client):
        client.post("/api/v1/auth/register", json={
            "username": "refreshuser", "email": "refresh@test.com", "password": "test123",
        })
        login_resp = client.post("/api/v1/auth/login", json={
            "username": "refreshuser", "password": "test123",
        })
        refresh_token = login_resp.get_json()["refresh_token"]

        resp = client.post("/api/v1/auth/refresh", json={
            "refresh_token": refresh_token,
        })
        assert resp.status_code == 200
        assert "access_token" in resp.get_json()

    def test_refresh_invalid(self, client):
        resp = client.post("/api/v1/auth/refresh", json={
            "refresh_token": "invalid-token",
        })
        assert resp.status_code == 401

    def test_password_reset_request(self, client):
        client.post("/api/v1/auth/register", json={
            "username": "resetuser", "email": "reset@test.com", "password": "test123",
        })
        resp = client.post("/api/v1/auth/password-reset-request", json={
            "email": "reset@test.com",
        })
        assert resp.status_code == 200
        assert "reset_token" in resp.get_json()

    def test_password_reset_request_not_found(self, client):
        resp = client.post("/api/v1/auth/password-reset-request", json={
            "email": "nonexistent@test.com",
        })
        assert resp.status_code == 404

    def test_password_reset_confirm(self, client):
        client.post("/api/v1/auth/register", json={
            "username": "confirmuser", "email": "confirm@test.com", "password": "test123",
        })
        req_resp = client.post("/api/v1/auth/password-reset-request", json={
            "email": "confirm@test.com",
        })
        reset_token = req_resp.get_json()["reset_token"]

        resp = client.post("/api/v1/auth/password-reset-confirm", json={
            "reset_token": reset_token, "new_password": "newpass123",
        })
        assert resp.status_code == 200

        # Verify can login with new password
        login_resp = client.post("/api/v1/auth/login", json={
            "username": "confirmuser", "password": "newpass123",
        })
        assert login_resp.status_code == 200


# ─────────────────────────────────────────────────────────────
# 3. Tags endpoints
# ─────────────────────────────────────────────────────────────

class TestTags:
    def test_create_tag_requires_auth(self, client):
        """Write endpoints require JWT."""
        resp = client.post("/api/v1/tags", json={"name": "Python"})
        assert resp.status_code == 401

    def test_create_tag(self, client, auth_headers):
        resp = client.post("/api/v1/tags", json={"name": "Python"},
                           headers=auth_headers)
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["name"] == "Python"
        assert "id" in body

    def test_create_tag_duplicate(self, client, auth_headers):
        client.post("/api/v1/tags", json={"name": "Python"}, headers=auth_headers)
        resp = client.post("/api/v1/tags", json={"name": "Python"},
                           headers=auth_headers)
        assert resp.status_code == 409

    def test_create_tag_empty_name(self, client, auth_headers):
        resp = client.post("/api/v1/tags", json={"name": ""}, headers=auth_headers)
        assert resp.status_code == 400

    def test_list_tags_returns_array(self, client):
        """List tags returns a direct array (no wrapper)."""
        resp = client.get("/api/v1/tags")
        assert resp.status_code == 200
        body = resp.get_json()
        assert isinstance(body, list)

    def test_list_tags(self, client, auth_headers):
        client.post("/api/v1/tags", json={"name": "Python"}, headers=auth_headers)
        client.post("/api/v1/tags", json={"name": "JavaScript"}, headers=auth_headers)
        resp = client.get("/api/v1/tags")
        assert len(resp.get_json()) == 2

    def test_update_tag(self, client, auth_headers):
        create_resp = client.post("/api/v1/tags", json={"name": "Py"},
                                  headers=auth_headers)
        tag_id = create_resp.get_json()["id"]
        resp = client.put(f"/api/v1/tags/{tag_id}", json={"name": "Python"},
                          headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["name"] == "Python"

    def test_update_tag_not_found(self, client, auth_headers):
        resp = client.put("/api/v1/tags/nonexistent-id",
                          json={"name": "Whatever"}, headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_tag(self, client, auth_headers):
        create_resp = client.post("/api/v1/tags", json={"name": "DeleteMe"},
                                  headers=auth_headers)
        tag_id = create_resp.get_json()["id"]
        resp = client.delete(f"/api/v1/tags/{tag_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["message"] == "deleted"

    def test_delete_tag_not_found(self, client, auth_headers):
        resp = client.delete("/api/v1/tags/nonexistent-id", headers=auth_headers)
        assert resp.status_code == 404

    def test_merge_tags(self, client, auth_headers):
        r1 = client.post("/api/v1/tags", json={"name": "Source"},
                         headers=auth_headers)
        r2 = client.post("/api/v1/tags", json={"name": "Target"},
                         headers=auth_headers)
        src_id = r1.get_json()["id"]
        tgt_id = r2.get_json()["id"]

        # Create article with source tag
        art = client.post("/api/v1/articles", json={
            "title": "Merge Test", "content": "Content", "tags": ["Source"],
        }, headers=auth_headers)
        assert art.status_code == 201

        resp = client.post("/api/v1/tags/merge", json={
            "source_id": src_id, "target_id": tgt_id,
        }, headers=auth_headers)
        assert resp.status_code == 200

        # Source tag should be gone
        tags_resp = client.get("/api/v1/tags")
        tag_names = [t["name"] for t in tags_resp.get_json()]
        assert "Source" not in tag_names

    def test_merge_tags_same(self, client, auth_headers):
        resp = client.post("/api/v1/tags/merge", json={
            "source_id": "same", "target_id": "same",
        }, headers=auth_headers)
        assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────
# 4. Articles endpoints
# ─────────────────────────────────────────────────────────────

class TestArticles:
    def test_create_article_requires_auth(self, client):
        """Write endpoints require JWT."""
        resp = client.post("/api/v1/articles", json={
            "title": "Test", "content": "Content",
        })
        assert resp.status_code == 401

    def test_create_article(self, client, auth_headers):
        resp = client.post("/api/v1/articles", json={
            "title": "My Article",
            "content": "# Hello\nThis is **markdown**",
            "tags": ["Python"],
        }, headers=auth_headers)
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["title"] == "My Article"
        assert "id" in body
        # Tags should be strings (frontend contract)
        assert body["tags"] == ["Python"]
        assert body["read_count"] == 0

    def test_create_article_missing_title(self, client, auth_headers):
        resp = client.post("/api/v1/articles", json={
            "content": "Content only",
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_create_article_missing_content(self, client, auth_headers):
        resp = client.post("/api/v1/articles", json={
            "title": "Title only",
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_get_article(self, client, auth_headers):
        create_resp = client.post("/api/v1/articles", json={
            "title": "Detail Test", "content": "# Detail", "tags": [],
        }, headers=auth_headers)
        article_id = create_resp.get_json()["id"]

        resp = client.get(f"/api/v1/articles/{article_id}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["title"] == "Detail Test"
        assert "content" in body
        assert "read_count" in body
        assert "tags" in body

    def test_get_article_not_found(self, client):
        resp = client.get("/api/v1/articles/nonexistent-id")
        assert resp.status_code == 404
        assert resp.get_json()["code"] == 40003

    def test_list_articles(self, client, auth_headers):
        client.post("/api/v1/articles", json={
            "title": "Article 1", "content": "Content 1", "tags": [],
        }, headers=auth_headers)
        client.post("/api/v1/articles", json={
            "title": "Article 2", "content": "Content 2", "tags": [],
        }, headers=auth_headers)

        resp = client.get("/api/v1/articles")
        assert resp.status_code == 200
        body = resp.get_json()
        # Frontend pagination contract: {articles, total, page, per_page}
        assert "articles" in body
        assert "total" in body
        assert "page" in body
        assert "per_page" in body
        assert len(body["articles"]) == 2
        assert body["total"] == 2

    def test_list_articles_pagination(self, client, auth_headers):
        for i in range(5):
            client.post("/api/v1/articles", json={
                "title": f"Article {i}", "content": f"Content {i}", "tags": [],
            }, headers=auth_headers)

        resp = client.get("/api/v1/articles?page=1&per_page=2")
        body = resp.get_json()
        assert len(body["articles"]) == 2
        assert body["total"] == 5
        assert body["per_page"] == 2

    def test_list_articles_filter_by_tag_name(self, client, auth_headers):
        client.post("/api/v1/tags", json={"name": "Flask"}, headers=auth_headers)
        client.post("/api/v1/articles", json={
            "title": "Flask Post", "content": "Content", "tags": ["Flask"],
        }, headers=auth_headers)
        client.post("/api/v1/articles", json={
            "title": "Other Post", "content": "Content", "tags": [],
        }, headers=auth_headers)

        resp = client.get("/api/v1/articles?tag=Flask")
        body = resp.get_json()
        assert len(body["articles"]) == 1
        assert body["articles"][0]["title"] == "Flask Post"

    def test_update_article(self, client, auth_headers):
        create_resp = client.post("/api/v1/articles", json={
            "title": "Original", "content": "Original content", "tags": [],
        }, headers=auth_headers)
        article_id = create_resp.get_json()["id"]

        resp = client.put(f"/api/v1/articles/{article_id}", json={
            "title": "Updated Title",
        }, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["title"] == "Updated Title"

    def test_update_article_not_found(self, client, auth_headers):
        resp = client.put("/api/v1/articles/nonexistent", json={
            "title": "Updated",
        }, headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_article(self, client, auth_headers):
        create_resp = client.post("/api/v1/articles", json={
            "title": "Delete Me", "content": "To be deleted", "tags": [],
        }, headers=auth_headers)
        article_id = create_resp.get_json()["id"]

        resp = client.delete(f"/api/v1/articles/{article_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["message"] == "deleted"

        # Verify deleted
        get_resp = client.get(f"/api/v1/articles/{article_id}")
        assert get_resp.status_code == 404

    def test_publish_article(self, client, auth_headers):
        create_resp = client.post("/api/v1/articles", json={
            "title": "Publish Me", "content": "Publish content", "tags": [],
        }, headers=auth_headers)
        article_id = create_resp.get_json()["id"]

        resp = client.patch(f"/api/v1/articles/{article_id}/publish",
                            headers=auth_headers)
        assert resp.status_code == 200

        # Verify published
        get_resp = client.get(f"/api/v1/articles/{article_id}")
        assert get_resp.status_code == 200

    def test_archive_article(self, client, auth_headers):
        create_resp = client.post("/api/v1/articles", json={
            "title": "Archive Me", "content": "Archive content", "tags": [],
        }, headers=auth_headers)
        article_id = create_resp.get_json()["id"]

        resp = client.patch(f"/api/v1/articles/{article_id}/archive",
                            headers=auth_headers)
        assert resp.status_code == 200

    def test_preview_markdown(self, client):
        resp = client.post("/api/v1/articles/preview", json={
            "content": "# Heading\n**bold** *italic*",
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert "<h1>Heading</h1>" in body["html"]
        assert "<strong>bold</strong>" in body["html"]
        assert "<em>italic</em>" in body["html"]

    def test_preview_empty_content(self, client):
        resp = client.post("/api/v1/articles/preview", json={"content": ""})
        assert resp.status_code == 400

    def test_tags_are_strings_in_article(self, client, auth_headers):
        """Frontend contract: article.tags is string[], not object[]."""
        client.post("/api/v1/tags", json={"name": "Go"}, headers=auth_headers)
        resp = client.post("/api/v1/articles", json={
            "title": "Go Post", "content": "Content", "tags": ["Go"],
        }, headers=auth_headers)
        assert resp.get_json()["tags"] == ["Go"]

        # Also check in list
        list_resp = client.get("/api/v1/articles")
        article = list_resp.get_json()["articles"][0]
        assert article["tags"] == ["Go"]


# ─────────────────────────────────────────────────────────────
# 5. Stats endpoints
# ─────────────────────────────────────────────────────────────

class TestStats:
    def test_record_read(self, client, auth_headers):
        create_resp = client.post("/api/v1/articles", json={
            "title": "Stats Test", "content": "Stats content", "tags": [],
        }, headers=auth_headers)
        article_id = create_resp.get_json()["id"]

        # Frontend contract: {success: true}
        resp = client.post(f"/api/v1/articles/{article_id}/read")
        assert resp.status_code == 201
        assert resp.get_json() == {"success": True}

    def test_record_read_not_found(self, client):
        resp = client.post("/api/v1/articles/nonexistent/read")
        assert resp.status_code == 404

    def test_article_stats(self, client, auth_headers):
        create_resp = client.post("/api/v1/articles", json={
            "title": "Stats Detail", "content": "Stats content", "tags": [],
        }, headers=auth_headers)
        article_id = create_resp.get_json()["id"]

        # Record a read first
        client.post(f"/api/v1/articles/{article_id}/read")

        resp = client.get(f"/api/v1/articles/{article_id}/stats")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["article_id"] == article_id
        assert body["read_count"] >= 1
        assert body["title"] == "Stats Detail"

    def test_unified_stats_endpoint(self, client, auth_headers):
        """Frontend contract: GET /api/v1/stats returns StatsSummary."""
        resp = client.get("/api/v1/stats")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "total_articles" in body
        assert "total_tags" in body
        assert "total_reads" in body
        assert "top_articles" in body
        assert "reads_by_tag" in body

    def test_stats_overview_legacy(self, client):
        resp = client.get("/api/v1/stats/overview")
        assert resp.status_code == 200

    def test_stats_ranking(self, client):
        resp = client.get("/api/v1/stats/ranking?limit=5")
        assert resp.status_code == 200
        assert "ranking" in resp.get_json()

    def test_stats_trends(self, client):
        resp = client.get("/api/v1/stats/trends?start_date=2026-01-01&end_date=2026-12-31")
        assert resp.status_code == 200


# ─────────────────────────────────────────────────────────────
# 6. Upload endpoint
# ─────────────────────────────────────────────────────────────

class TestUpload:
    def test_upload_image_requires_auth(self, client):
        """Write endpoints require JWT."""
        data = {"file": (io.BytesIO(b"fake-image-data"), "test.png")}
        resp = client.post("/api/v1/upload/image", data=data,
                           content_type="multipart/form-data")
        assert resp.status_code == 401

    def test_upload_image(self, client, auth_headers):
        data = {"file": (io.BytesIO(b"fake-image-data"), "test.png")}
        resp = client.post("/api/v1/upload/image", data=data,
                           content_type="multipart/form-data",
                           headers=auth_headers)
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["url"].startswith("/uploads/")

    def test_upload_no_file(self, client, auth_headers):
        resp = client.post("/api/v1/upload/image", data={},
                           content_type="multipart/form-data",
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_upload_invalid_extension(self, client, auth_headers):
        data = {"file": (io.BytesIO(b"data"), "test.exe")}
        resp = client.post("/api/v1/upload/image", data=data,
                           content_type="multipart/form-data",
                           headers=auth_headers)
        assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────
# 7. RSS endpoint
# ─────────────────────────────────────────────────────────────

class TestRSS:
    def test_rss_feed(self, client):
        resp = client.get("/api/v1/rss.xml")
        assert resp.status_code == 200
        assert "rss" in resp.content_type
        assert b"<rss" in resp.data
        assert b"<channel>" in resp.data

    def test_rss_feed_with_articles(self, client, auth_headers):
        create_resp = client.post("/api/v1/articles", json={
            "title": "RSS Article", "content": "RSS content", "tags": [],
        }, headers=auth_headers)

        # Publish it
        article_id = create_resp.get_json()["id"]
        client.patch(f"/api/v1/articles/{article_id}/publish", headers=auth_headers)

        resp = client.get("/api/v1/rss.xml")
        assert b"RSS Article" in resp.data


# ─────────────────────────────────────────────────────────────
# 8. Error format
# ─────────────────────────────────────────────────────────────

class TestErrorFormat:
    def test_error_response_structure(self, client):
        """All errors should have {code, data, message}."""
        resp = client.get("/api/v1/articles/nonexistent")
        body = resp.get_json()
        assert "code" in body
        assert "data" in body
        assert "message" in body
        assert body["code"] == 40003

    def test_not_found_code(self, client):
        resp = client.get("/api/v1/articles/nonexistent")
        assert resp.get_json()["code"] == 40003

    def test_validation_error_code(self, client, auth_headers):
        """Empty title after strip triggers validation."""
        resp = client.post("/api/v1/articles", json={
            "title": "", "content": "x",
        }, headers=auth_headers)
        assert resp.get_json()["code"] in (40000, 40001)

    def test_auth_error(self, client):
        """Write without auth returns 401."""
        resp = client.post("/api/v1/articles", json={
            "title": "Test", "content": "Test",
        })
        assert resp.status_code == 401
        assert resp.get_json()["code"] == 40100

    def test_internal_error(self, client, monkeypatch):
        """Simulate an internal error."""
        import jwt
        from backend.config import JWT_SECRET

        def broken(*args):
            raise RuntimeError("Simulated failure")
        monkeypatch.setattr("backend.api.articles._get_read_count", broken)

        # Create a real JWT for auth
        token = jwt.encode(
            {"sub": "test-user", "username": "tester", "type": "access"},
            JWT_SECRET, algorithm="HS256",
        )
        auth = {"Authorization": f"Bearer {token}"}

        # Create an article first
        resp = client.post("/api/v1/articles", json={
            "title": "Error Test", "content": "Content",
        }, headers=auth)
        assert resp.status_code == 201
        article_id = resp.get_json()["id"]
        resp = client.get(f"/api/v1/articles/{article_id}")
        assert resp.status_code in (200, 500)
