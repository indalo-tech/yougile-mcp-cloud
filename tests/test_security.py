"""Sign-in phishing (open client registration), keys in the log, and their guards."""

from __future__ import annotations

import logging

from conftest import REDIRECT, pkce, register_client, start_flow

from yougile_cloud import logredact, redirects

EVIL = "https://evil.example/cb"


def test_only_known_callbacks_and_loopback():
    assert redirects.allowed("http://localhost:50158/callback")
    assert redirects.allowed("http://127.0.0.1:1/cb")
    assert redirects.allowed("https://claude.ai/api/mcp/auth_callback")
    assert redirects.allowed("https://claude.com/api/mcp/auth_callback")
    assert not redirects.allowed(EVIL)
    assert not redirects.allowed("https://claude.ai.evil.example/cb")
    assert not redirects.allowed("http://claude.ai/cb"), "https only for known clients"
    assert not redirects.allowed("javascript:alert(1)")
    extra = redirects.parse_allowed("https://chatgpt.com/whatever, cursor:")
    assert extra == ("https://chatgpt.com", "cursor:")
    assert redirects.allowed("https://chatgpt.com/connector_platform_oauth_redirect", extra)
    assert not redirects.allowed("https://chatgpt.com.evil.example/x", extra)
    assert redirects.allowed("cursor://anysphere.cursor-mcp/oauth/callback", extra)
    assert not redirects.allowed("javascript:alert(1)", redirects.parse_allowed("javascript:"))
    assert redirects.destination("https://claude.ai/api/mcp/auth_callback") == "claude.ai"
    assert redirects.destination(REDIRECT) == "программа на этом компьютере"


def test_keys_are_masked_in_the_log(caplog):
    logredact.install()
    caplog.set_level(logging.INFO, logger="yougile_mcp.client")
    logging.getLogger("yougile_mcp.client").info(
        "%s %s -> %d", "DELETE", "/auth/keys/abc123secret", 200
    )
    logging.getLogger("yougile_mcp.client").warning("DELETE /auth/keys/def456secret failed: boom")
    text = caplog.text
    assert "abc123secret" not in text and "def456secret" not in text
    assert text.count("/auth/keys/***") == 2


async def test_unknown_callbacks_cannot_register(server, http):
    resp = await http.post(
        "/register",
        json={
            "redirect_uris": [EVIL],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_redirect_uri"


async def test_a_client_registered_before_is_refused_on_our_page(server, settings, http):
    await server.db.save_client(
        "old-evil",
        {
            "client_id": "old-evil",
            "client_name": "Claude",
            "redirect_uris": [EVIL],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    _verifier, challenge = pkce()
    resp = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": "old-evil",
            "redirect_uri": EVIL,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "s",
            "resource": settings.mcp_url,
        },
    )
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == f"{settings.public_url}/signin/refused"
    assert "evil.example" not in resp.headers["location"]
    page = await http.get("/signin/refused")
    assert page.status_code == 403 and "не из списка известных" in page.text


async def test_the_sign_in_page_says_who_asks(server, settings, http):
    client_id = await register_client(http)
    location, _verifier = await start_flow(http, settings, client_id)
    page = await http.get(location)
    assert "Доступ к вашему YouGile запрашивает" in page.text
    assert "&lt;script&gt;" in page.text and "<script>" not in page.text
    assert "программа на этом компьютере" in page.text
