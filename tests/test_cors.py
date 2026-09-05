"""Tests for CORS middleware configuration (issue #49, gap G7).

CORS_ORIGINS is parsed at import time (default "*" = allow all). These tests
verify the default behavior via TestClient and the parsing helper directly.
"""

from fastapi.testclient import TestClient

from server.main import _parse_cors_origins, app


client = TestClient(app)


class TestCorsParsing:
    def test_default_allows_all(self):
        assert _parse_cors_origins("*") == ["*"]

    def test_single_origin(self):
        assert _parse_cors_origins("http://localhost:8501") == [
            "http://localhost:8501"
        ]

    def test_multiple_origins_with_spaces(self):
        assert _parse_cors_origins("http://a.com , http://b.com,") == [
            "http://a.com",
            "http://b.com",
        ]

    def test_empty_parts_ignored(self):
        assert _parse_cors_origins("") == []
        assert _parse_cors_origins(" , ") == []


class TestCorsDefaultWildcard:
    def test_get_request_returns_cors_header(self):
        resp = client.get("/agents", headers={"Origin": "http://localhost:8501"})
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "*"

    def test_preflight_options_allowed(self):
        resp = client.options(
            "/agents",
            headers={
                "Origin": "http://localhost:8501",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "*"
        assert "GET" in resp.headers.get("access-control-allow-methods", "")

    def test_preflight_post_allowed(self):
        resp = client.options(
            "/agents",
            headers={
                "Origin": "https://agent-harness.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert resp.status_code == 200
        assert "authorization" in resp.headers.get("access-control-allow-headers", "").lower()
