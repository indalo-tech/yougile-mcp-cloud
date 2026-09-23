"""End to end over HTTP: register a client, sign in with YouGile, get tokens, call tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from conftest import REDIRECT, exchange, register_client, sign_in, start_flow
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def code_from(resp) -> str:  # noqa: ANN001
    assert resp.status_code == 302, resp.text
    target = urlparse(resp.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == REDIRECT
    query = parse_qs(target.query)
    assert query["state"] == ["xyz"]
    return query["code"][0]


async def mcp_client(settings, token: str) -> Client:  # noqa: ANN001
    return Client(
        StreamableHttpTransport(settings.mcp_url, headers={"Authorization": f"Bearer {token}"})
    )


async def test_full_flow_with_company_choice(server, settings, http, fake):
    client_id = await register_client(http)
    signin_path, verifier = await start_flow(http, settings, client_id)
    resp = await sign_in(
        http, settings, signin_path, login="anna@example.com", password="right", company="c-main"
    )
    tokens = await exchange(http, client_id, code_from(resp), verifier)
    assert tokens["token_type"] == "Bearer" and tokens["refresh_token"]

    # the code works once
    again = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code_from(resp),
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert again.status_code in (400, 401), again.text
    assert again.json()["error"] in ("invalid_grant", "invalid_client"), again.text

    async with await mcp_client(settings, tokens["access_token"]) as c:
        tools = {t.name for t in await c.list_tools()}
        assert {"yougile_overview", "yougile_tasks"} <= tools
        overview = (await c.call_tool("yougile_overview", {})).data
        assert overview["projects"][0]["boards"][0]["columns"] == ["Очередь", "Готово"]
        # hosted servers must not read local files
        result = await c.call_tool(
            "yougile_files",
            {"operation": "upload", "params": {"file_path": "/etc/passwd"}},
            raise_on_error=False,
        )
        assert result.is_error and "not available on this server" in str(result.content)

    user = (await server.db.list_users("c-main"))[0]
    assert user.is_admin and user.api_key_enc and b"u-anna" not in user.api_key_enc


async def test_refresh_rotates_and_old_token_dies(server, settings, http):
    client_id = await register_client(http)
    signin_path, verifier = await start_flow(http, settings, client_id)
    resp = await sign_in(http, settings, signin_path, login="solo@example.com", password="pw")
    tokens = await exchange(http, client_id, code_from(resp), verifier)

    refresh = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
    }
    first = await http.post("/token", data=refresh)
    assert first.status_code == 200 and first.json()["refresh_token"] != tokens["refresh_token"]
    replay = await http.post("/token", data=refresh)
    assert replay.status_code in (400, 401), "a refresh token must work only once"
    assert replay.json()["error"] == "invalid_grant"


async def test_mcp_needs_a_valid_token(server, settings, http):
    resp = await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": "Bearer garbage",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert resp.status_code == 401


async def test_signin_page_is_safe(server, settings, http):
    client_id = await register_client(http)
    signin_path, _ = await start_flow(http, settings, client_id)
    page = await http.get(signin_path)
    assert "<script>" not in page.text, "client_name must never be rendered raw"
    assert "default-src 'none'" in page.headers["content-security-policy"]

    flow = signin_path.split("flow=")[1]
    no_csrf = await http.post(
        "/signin",
        data={"flow": flow, "login": "a@b.c", "password": "x"},
        headers={"Origin": settings.public_url},
    )
    assert no_csrf.status_code == 403
    foreign = await sign_in(
        http, settings, signin_path, login='"><script>x</script>', password="bad"
    )
    assert foreign.status_code == 401
    assert "<script>x</script>" not in foreign.text and "&lt;script&gt;" in foreign.text


async def test_cross_origin_post_is_refused(server, settings, http):
    client_id = await register_client(http)
    signin_path, _ = await start_flow(http, settings, client_id)
    page = await http.get(signin_path)
    from conftest import field

    resp = await http.post(
        "/signin",
        data={
            "flow": field(page.text, "flow"),
            "csrf": field(page.text, "csrf"),
            "login": "anna@example.com",
            "password": "right",
        },
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


async def test_attempt_limit(server, settings, http):
    client_id = await register_client(http)
    signin_path, _ = await start_flow(http, settings, client_id)
    statuses = [
        (
            await sign_in(http, settings, signin_path, login="solo@example.com", password="wrong")
        ).status_code
        for _ in range(9)
    ]
    assert statuses[:8] == [401] * 8 and statuses[8] == 429


async def test_expired_trial_blocks_sign_in_and_tools(server, settings, http, fake):
    client_id = await register_client(http)
    signin_path, verifier = await start_flow(http, settings, client_id)
    resp = await sign_in(http, settings, signin_path, login="solo@example.com", password="pw")
    tokens = await exchange(http, client_id, code_from(resp), verifier)

    await server.db.set_company_access(
        "c-solo", status="trial", trial_ends_at=datetime.now(UTC) - timedelta(days=1)
    )
    server.tenancy._cache.clear()
    async with await mcp_client(settings, tokens["access_token"]) as c:
        result = await c.call_tool("yougile_overview", {}, raise_on_error=False)
        assert result.is_error and "Пробный период закончился" in str(result.content)

    keys_before = len(fake.keys)
    signin_path, _ = await start_flow(http, settings, client_id)
    resp = await sign_in(http, settings, signin_path, login="solo@example.com", password="pw")
    assert resp.status_code == 402 and "Пробный период" in resp.text
    assert len(fake.keys) == keys_before, "no key is issued for a company without access"


async def test_free_company_is_exempt_and_second_login_revokes_old_key(
    server, settings, http, fake
):
    client_id = await register_client(http)
    for _ in range(2):
        signin_path, verifier = await start_flow(http, settings, client_id)
        resp = await sign_in(
            http,
            settings,
            signin_path,
            login="anna@example.com",
            password="right",
            company="c-free",
        )
        await exchange(http, client_id, code_from(resp), verifier)
    company = await server.db.get_company("c-free")
    assert company.status == "exempt"
    assert len(fake.deleted) == 1, "the previous key is deleted on re-login"
    assert len([k for k, (c, _) in fake.keys.items() if c == "c-free"]) == 1


async def test_unknown_resource_is_rejected(server, settings, http):
    client_id = await register_client(http)
    resp = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": "x" * 43,
            "code_challenge_method": "S256",
            "state": "s",
            "resource": "https://other.example/mcp",
        },
    )
    assert resp.status_code in (302, 400)
    assert "flow=" not in resp.headers.get("location", "")
