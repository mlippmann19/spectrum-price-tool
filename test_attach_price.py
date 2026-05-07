"""Local tests for /pipedrive/attach-price.

Run with:   python -m pytest test_attach_price.py -v

Uses httpx.MockTransport to intercept Pipedrive API calls so nothing
hits the live Pipedrive account.
"""
import importlib
import json
import os
import sys
import tempfile

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def server(monkeypatch):
    """Reload server.py with an isolated tokens.db per test."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    monkeypatch.setenv("TOKENS_DB_PATH", tmp.name)
    monkeypatch.delenv("PIPEDRIVE_API_TOKEN", raising=False)
    monkeypatch.delenv("PIPEDRIVE_API_BASE", raising=False)
    if "server" in sys.modules:
        del sys.modules["server"]
    server = importlib.import_module("server")
    server.init_db()
    yield server
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def test_no_tokens_returns_no_install_tokens(server):
    """With no OAuth install row and no PIPEDRIVE_API_TOKEN env, the
    endpoint must return the friendly no_install_tokens error and not
    attempt any Pipedrive call.
    """
    with TestClient(server.app) as client:
        resp = client.post(
            "/pipedrive/attach-price",
            json={"dealId": 1473, "lineItems": [{"code": "RCS", "qty": 1, "unitPrice": 100}]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["detail"] == "no_install_tokens"
    assert "hint" in body  # explains how to recover


def test_api_token_fallback_attaches_products(server, monkeypatch):
    """When PIPEDRIVE_API_TOKEN is set and no OAuth row exists, the
    endpoint must use the api_token query-string auth and attach products.
    """
    monkeypatch.setenv("PIPEDRIVE_API_TOKEN", "TEST_API_TOKEN_123")

    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.method == "GET" and "/deals/1473/products" in request.url.path:
            return httpx.Response(200, json={"data": []})  # no existing products
        if request.method == "POST" and request.url.path.endswith("/deals/1473/products"):
            payload = json.loads(request.content)
            return httpx.Response(201, json={"data": {"id": 999, **payload}})
        return httpx.Response(404, json={"error": "unexpected"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(server.httpx, "AsyncClient", fake_async_client)

    with TestClient(server.app) as client:
        resp = client.post(
            "/pipedrive/attach-price",
            json={
                "dealId": 1473,
                "lineItems": [
                    {"code": "RCS", "qty": 1, "unitPrice": 1500.00},
                    {"code": "FF",  "qty": 2, "unitPrice": 200.00},
                ],
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok", body
    assert body["mode"] == "attach_products"
    assert len(body["actions"]["added"]) == 2

    # Auth assertion: every outbound request carried api_token in the query string.
    assert requests_seen, "no Pipedrive calls were made"
    for r in requests_seen:
        assert "api_token=TEST_API_TOKEN_123" in str(r.url), r.url


def test_oauth_row_takes_precedence_over_api_token(server, monkeypatch):
    """If both an OAuth install and PIPEDRIVE_API_TOKEN are present,
    the OAuth path is used (multi-tenant + refresh-capable).
    """
    monkeypatch.setenv("PIPEDRIVE_API_TOKEN", "FALLBACK_TOKEN")

    server.store_tokens(
        {"access_token": "OAUTH_ACCESS", "refresh_token": "REFRESH",
         "expires_in": 3600, "token_type": "Bearer", "scope": "deals:full"},
        {"company_id": "42", "company_domain": "acme", "id": "7"},
    )

    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        if request.method == "GET" and "/deals/1473/products" in request.url.path:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"error": "unexpected"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        server.httpx, "AsyncClient",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": transport}),
    )

    with TestClient(server.app) as client:
        resp = client.post(
            "/pipedrive/attach-price",
            json={"dealId": 1473, "lineItems": []},
        )

    assert resp.status_code == 200
    assert requests_seen
    # OAuth path uses Authorization: Bearer header, not api_token query.
    first = requests_seen[0]
    assert first.headers.get("Authorization") == "Bearer OAUTH_ACCESS"
    assert "api_token=" not in str(first.url)


def test_debug_tokens_reports_active_path(server, monkeypatch):
    """/debug/tokens must report the active auth path without leaking
    token values."""
    monkeypatch.setenv("PIPEDRIVE_API_TOKEN", "VERY_SECRET")

    with TestClient(server.app) as client:
        resp = client.get("/debug/tokens")
    body = resp.json()
    assert body["install_count"] == 0
    assert body["api_token_env_set"] is True
    assert body["active_path"] == "api_token_env"
    # Make absolutely sure the secret is not in the response.
    assert "VERY_SECRET" not in resp.text


def test_install_route_redirects_to_pipedrive(server, monkeypatch):
    monkeypatch.setenv("PIPEDRIVE_CLIENT_ID", "abc123")
    monkeypatch.setenv("REDIRECT_URI", "https://example.com/callback")
    with TestClient(server.app) as client:
        resp = client.get("/install", follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("https://oauth.pipedrive.com/oauth/authorize")
    assert "client_id=abc123" in loc
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Fcallback" in loc
