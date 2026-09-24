"""Company admin pages over HTTP: sign-in, company settings, employees' rights, disconnecting."""

from __future__ import annotations

from conftest import admin_login, connect, field
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def mcp(settings, token: str) -> Client:  # noqa: ANN001
    return Client(
        StreamableHttpTransport(settings.mcp_url, headers={"Authorization": f"Bearer {token}"})
    )


def origin(settings) -> dict[str, str]:  # noqa: ANN001
    return {"Origin": settings.public_url}


async def test_admin_sign_in_and_employees(server, settings, http):
    resp = await http.get("/admin")
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/login"

    page = await http.get("/admin/login")
    resp = await http.post(
        "/admin/login",
        data={"csrf": field(page.text, "csrf"), "login": "anna@example.com", "password": "right"},
        headers=origin(settings),
    )
    assert resp.status_code == 200 and "Main Co" in resp.text and "Two Co" in resp.text
    assert "Own Co" not in resp.text, "only companies where the person is an admin"
    resp = await http.post(
        "/admin/login/company",
        data={"csrf": field(resp.text, "csrf"), "company": "c-main"},
        headers=origin(settings),
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/admin"
    session = next(c for c in resp.headers.get_list("set-cookie") if c.startswith("ygc-admin="))
    assert all(flag in session.lower() for flag in ("httponly", "samesite=lax", "path=/"))

    home = await http.get("/admin")
    assert home.status_code == 200 and "Main Co" in home.text and "Пробный период" in home.text
    assert "u-anna@example.com" in home.text and "u-bob@example.com" in home.text
    assert "<img" not in home.text and "&lt;img" in home.text, "names from YouGile are escaped"
    assert "form-action 'self'" in home.headers["content-security-policy"]
    assert home.headers["referrer-policy"] == "same-origin", "else browsers post Origin: null"


async def test_only_admins_get_in(server, settings, http, fake):
    resp = await admin_login(http, settings, login="bob@example.com", password="pw", company=None)
    assert resp.status_code == 403 and "не администратор" in resp.text
    assert not fake.keys, "no key is issued to people who are not admins"
    resp = await admin_login(http, settings, password="wrong", company=None)
    assert resp.status_code == 401


async def test_company_settings_reach_mcp_sessions(server, settings, http):
    _, bob = await connect(http, settings, login="bob@example.com", password="pw")
    assert (await admin_login(http, settings)).status_code == 303

    page = await http.get("/admin/settings")
    assert page.status_code == 200 and 'value="Europe/Moscow"' in page.text
    resp = await http.post(
        "/admin/settings",
        data={
            "csrf": field(page.text, "csrf"),
            "default_role": "reader",
            "timezone": "Asia/Yerevan",
            "instructions": "Пиши кратко",
            "confirm": ["p1", "p-foreign"],
            "deny": ["task_delete", "bogus"],
            "workflows": "проект / доска: очередь -> готово",
            "done": ["готово", "Нет такой"],
        },
        headers=origin(settings),
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/settings?done=settings"
    stored = (await server.db.get_company("c-main")).settings
    assert stored["workflows"] == {"Проект / Доска": ["Очередь", "Готово"]}
    assert (stored["deny"], stored["confirm_projects"]) == (["tasks.delete"], ["p1"])
    assert stored["done_columns"] == ["Готово"], "known titles only, as YouGile spells them"

    page = await http.get("/admin/settings?done=settings")
    assert "Настройки сохранены" in page.text and "Проект / Доска: Очередь → Готово" in page.text
    assert 'name="done" value="Готово" checked' in page.text

    server.tenancy._cache.clear()
    async with mcp(settings, bob["access_token"]) as c:
        tools = {t.name for t in await c.list_tools()}
        assert "yougile_overview" in tools and "yougile_create_task" not in tools, "reader"
        overview = (await c.call_tool("yougile_overview", {})).data
        assert (overview["company_rules"], overview["timezone"]) == ("Пиши кратко", "Asia/Yerevan")
        assert overview["projects"][0]["boards"][0]["workflow"] == ["Очередь", "Готово"]
        assert overview["done_columns"] == ["Готово"]
        assert "role=reader" in overview["permissions"]
        assert "/admin" in overview["settings_in"], "the assistant can give the admin link"
        result = await c.call_tool(
            "yougile_tasks",
            {"operation": "create", "params": {"title": "x", "columnId": "k1"}},
            raise_on_error=False,
        )
        assert result.is_error and "reader" in str(result.content)
        assert "/admin" in str(result.content), "a refusal says where rights are changed"
        standup = await c.get_prompt("standup")
        assert "Asia/Yerevan" in standup.messages[0].content.text, "prompts follow company time"

    assert "последний запрос" in (await http.get("/admin")).text


async def test_bad_settings_are_rejected(server, settings, http):
    await admin_login(http, settings)
    page = await http.get("/admin/settings")
    resp = await http.post(
        "/admin/settings",
        data={
            "csrf": field(page.text, "csrf"),
            "default_role": "member",
            "timezone": "Mars/Base",
            "workflows": "Нет / Такой: А → Б\nПроект / Доска: Очередь → Архив",
        },
        headers=origin(settings),
    )
    assert resp.status_code == 400
    assert all(bit in resp.text for bit in ("Mars/Base", "Нет / Такой", "«Архив»"))
    assert (await server.db.get_company("c-main")).settings == {}


async def test_employee_rights_and_reset(server, settings, http):
    await admin_login(http, settings)
    page = await http.get("/admin/users/u-bob")
    assert page.status_code == 200 and "Подключения к YouGile MCP ещё не было" in page.text
    csrf = field(page.text, "csrf")

    resp = await http.post(
        "/admin/users/u-bob",
        data={
            "csrf": csrf,
            "role": "admin",
            "scope": "some",
            "project": ["p1", "p-foreign"],
            "deny": "chat_send",
        },
        headers=origin(settings),
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/users/u-bob?done=rights"
    rights = await server.db.get_rights("c-main", "u-bob")
    assert (rights.role, rights.projects, rights.deny) == (
        "admin",
        ["p1"],
        ["chats.send_message", "chats.typing"],
    )

    bad = await http.post(
        "/admin/users/u-bob",
        data={"csrf": csrf, "role": "root", "scope": "some"},
        headers=origin(settings),
    )
    assert bad.status_code == 400 and "хотя бы один проект" in bad.text

    resp = await http.post(
        "/admin/users/u-bob/reset", data={"csrf": csrf}, headers=origin(settings)
    )
    assert resp.status_code == 303
    assert await server.db.get_rights("c-main", "u-bob") is None
    assert (await http.get("/admin/users/nobody")).status_code == 404


async def test_admin_forms_are_protected(server, settings, http):
    anonymous = await http.post("/admin/settings", data={}, headers=origin(settings))
    assert anonymous.status_code == 303 and anonymous.headers["location"] == "/admin/login"
    no_cookie = await http.post(
        "/admin/login",
        data={"csrf": "x", "login": "anna@example.com", "password": "right"},
        headers=origin(settings),
    )
    assert no_cookie.status_code == 403, "the login form only works from our login page"

    await admin_login(http, settings)
    page = await http.get("/admin/settings")
    data = {"csrf": field(page.text, "csrf"), "default_role": "admin", "timezone": "UTC"}
    forged = await http.post(
        "/admin/settings", data={**data, "csrf": "forged"}, headers=origin(settings)
    )
    assert forged.status_code == 403
    foreign = await http.post(
        "/admin/settings", data=data, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    assert (await server.db.get_company("c-main")).settings == {}


async def test_demoted_admin_is_let_out(server, settings, http, fake):
    await admin_login(http, settings)
    fake.admins["u-anna"] = False
    resp = await http.get("/admin")
    assert resp.status_code == 403 and "Нет прав администратора" in resp.text
    assert (await http.get("/admin")).status_code == 303, "the session is gone"


async def test_disconnect_revokes_key_and_tokens(server, settings, http, fake):
    client_id, bob = await connect(http, settings, login="bob@example.com", password="pw")
    bob_key = next(k for k, (_, user) in fake.keys.items() if user == "u-bob")
    await admin_login(http, settings)

    page = await http.get("/admin/users/u-bob")
    resp = await http.post(
        "/admin/users/u-bob/disconnect",
        data={"csrf": field(page.text, "csrf")},
        headers=origin(settings),
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/admin?done=disconnect"
    assert bob_key in fake.deleted
    assert [u.yougile_user_id for u in await server.db.list_users("c-main")] == ["u-anna"]

    refresh = await http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": bob["refresh_token"],
            "client_id": client_id,
        },
    )
    assert refresh.status_code in (400, 401)
    async with mcp(settings, bob["access_token"]) as c:
        result = await c.call_tool("yougile_overview", {}, raise_on_error=False)
        assert result.is_error and "подключите YouGile MCP заново" in str(result.content)


async def test_logout(server, settings, http):
    await admin_login(http, settings)
    page = await http.get("/admin")
    resp = await http.post(
        "/admin/logout", data={"csrf": field(page.text, "csrf")}, headers=origin(settings)
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/admin/login"
    assert (await http.get("/admin")).status_code == 303
