"""The ticket bot's admin page over HTTP: only the bot's company, token, approvers, the bot's
YouGile account and the webhook subscriptions."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import httpx2
import pytest
from conftest import admin_login, field

from yougile_cloud.tickets.service import hook_url
from yougile_cloud.tickets.store import TicketStore

TOKEN = "123456:" + "A" * 35


@pytest.fixture
def settings(settings):  # noqa: ANN001 - the conftest one, with the bot's company set
    return dataclasses.replace(settings, tickets_company_id="c-main")


@pytest.fixture
def telegram(server):  # noqa: ANN001
    calls: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if "/bot" + TOKEN + "/" not in request.url.path:
            return httpx2.Response(
                401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}
            )
        me = {"id": 1, "is_bot": True, "username": "test_bot", "has_topics_enabled": True}
        return httpx2.Response(200, json={"ok": True, "result": me})

    server.admin.tickets.tg_transport = httpx2.MockTransport(handle)
    return calls


@pytest.fixture
async def store(server):  # noqa: ANN001
    yield TicketStore(server.db)


def origin(settings) -> dict[str, str]:  # noqa: ANN001
    return {"Origin": settings.public_url}


async def post(http, settings, path: str, data: dict) -> httpx2.Response:  # noqa: ANN001
    page = await http.get("/admin/tickets")
    return await http.post(
        path, data={"csrf": field(page.text, "csrf"), **data}, headers=origin(settings)
    )


async def test_only_the_bots_company_sees_the_page(server, settings, http):
    assert (await admin_login(http, settings, company="c-two")).status_code == 303
    home = await http.get("/admin")
    assert "Бот заявок" not in home.text
    assert (await http.get("/admin/tickets")).status_code == 404
    resp = await http.post(
        "/admin/tickets/telegram",
        data={"csrf": field(home.text, "csrf"), "admins": "1"},
        headers=origin(settings),
    )
    assert resp.status_code == 404


async def test_token_and_approvers(server, settings, http, telegram, store):
    assert (await admin_login(http, settings)).status_code == 303
    home = await http.get("/admin")
    assert 'href="/admin/tickets"' in home.text
    page = await http.get("/admin/tickets")
    assert page.status_code == 200 and "Токен ещё не задан" in page.text

    bad = await post(http, settings, "/admin/tickets/telegram", {"token": "", "admins": "12 x"})
    assert bad.status_code == 200 and "только цифры" in bad.text
    junk = await post(http, settings, "/admin/tickets/telegram", {"token": "abc", "admins": ""})
    assert "не похоже на токен" in junk.text
    wrong = await post(
        http, settings, "/admin/tickets/telegram", {"token": "654321:" + "B" * 35, "admins": ""}
    )
    assert "Telegram не принял токен" in wrong.text
    assert await store.bot_config() is None

    ok = await post(
        http, settings, "/admin/tickets/telegram", {"token": TOKEN, "admins": "11, 22\n22"}
    )
    assert ok.status_code == 303 and ok.headers["location"].endswith("done=tickets_telegram")
    config = await store.bot_config()
    assert config.admins == {11, 22} and config.bot_username == "test_bot"
    assert server.secrets.decrypt(config.token_enc) == TOKEN
    page = await http.get("/admin/tickets")
    assert "@test_bot" in page.text and "темы включены" in page.text
    assert TOKEN not in page.text and "AAAAAAAAAA" not in page.text, "the token is never shown"

    # An empty token keeps the stored one; the approvers are replaced.
    await post(http, settings, "/admin/tickets/telegram", {"token": "", "admins": "33"})
    config = await store.bot_config()
    assert config.admins == {33} and server.secrets.decrypt(config.token_enc) == TOKEN

    no_csrf = await http.post(
        "/admin/tickets/telegram", data={"admins": "1"}, headers=origin(settings)
    )
    assert no_csrf.status_code == 403


async def test_bot_account(server, settings, http, fake, store, telegram):
    fake.accounts["bot@example.com"] = {"password": "pw", "memberships": {"c-main": "u-bot"}}
    fake.accounts["stranger@example.com"] = {"password": "pw", "memberships": {"c-solo": "u-x"}}
    assert (await admin_login(http, settings)).status_code == 303
    page = await http.get("/admin/tickets")
    assert "Учётка ещё не подключена" in page.text

    mine = await post(
        http, settings, "/admin/tickets/account", {"login": "anna@example.com", "password": "right"}
    )
    assert "собственная учётка" in mine.text
    other = await post(
        http,
        settings,
        "/admin/tickets/account",
        {"login": "stranger@example.com", "password": "pw"},
    )
    assert "не состоит в компании" in other.text
    wrong = await post(
        http, settings, "/admin/tickets/account", {"login": "bot@example.com", "password": "no"}
    )
    assert "Неверный логин" in wrong.text
    assert await store.account_of_company("c-main") is None

    ok = await post(
        http, settings, "/admin/tickets/account", {"login": "bot@example.com", "password": "pw"}
    )
    assert ok.status_code == 303
    account = await store.account_of_company("c-main")
    assert account.bot_user_id == "u-bot" and account.name == "Main Co"
    first_key = server.secrets.decrypt(account.api_key_enc)
    page = await http.get("/admin/tickets")
    assert "Бот пишет в YouGile как" in page.text and "u-bot" in page.text
    assert "Клиенты, Проект" in page.text  # what the bot account sees, none with «Заявки»

    await post(
        http, settings, "/admin/tickets/account", {"login": "bot@example.com", "password": "pw"}
    )
    assert first_key in fake.deleted, "the replaced key is deleted in YouGile"
    assert len(await store.accounts()) == 1


async def test_webhooks_are_made_with_the_admins_key(server, settings, http, fake, telegram):
    assert (await admin_login(http, settings)).status_code == 303
    page = await http.get("/admin/tickets")
    assert "Подписок ещё нет" in page.text
    ok = await post(http, settings, "/admin/tickets/webhooks", {})
    assert ok.status_code == 303
    assert {h["event"] for h in fake.webhooks} == {"task-.*", "chat_message-created"}
    assert all(h["url"] == hook_url(settings) for h in fake.webhooks)
    await post(http, settings, "/admin/tickets/webhooks", {})
    assert len(fake.webhooks) == 2, "subscribing again changes nothing"
    page = await http.get("/admin/tickets")
    assert page.text.count('<td data-label="Состояние">работает</td>') == 2
    assert "Проверить подписки" in page.text
    assert json.dumps(hook_url(settings))[1:-1] not in page.text, "the hook secret is not shown"


async def test_people_are_managed_from_the_page(server, settings, http, fake, store, telegram):
    from yougile_cloud.tickets import admin_page

    fake.accounts["bot@example.com"] = {"password": "pw", "memberships": {"c-main": "u-bot"}}
    fake.boards.append({"id": "b-req", "title": "Заявки", "projectId": "p2"})
    fake.columns.append({"id": "k-req", "title": "Очередь", "boardId": "b-req"})
    assert (await admin_login(http, settings)).status_code == 303
    await post(
        http, settings, "/admin/tickets/account", {"login": "bot@example.com", "password": "pw"}
    )
    account = await store.account_of_company("c-main")
    await store.request_access(100, "Анна", "anna")
    await store.request_access(200, "Ева", "")
    await store.decide(200, "blocked", by=None)
    await store.request_access(300, "<img src=x onerror=alert(1)>", "")
    await store.decide(300, "blocked", by=None)

    page = await http.get("/admin/tickets")
    assert f'<option value="{account.id}:p2">Клиенты</option>' in page.text
    assert page.text.count('value="approve"') == 1 and page.text.count('value="reject"') == 1
    assert page.text.count('value="unblock"') == 2 and page.text.count('value="block"') == 1
    assert "<img src=x" not in page.text
    calls = len(fake.calls)
    named = await post(
        http, settings, "/admin/tickets/sender", {"tg_user_id": "300", "action": "block"}
    )
    assert "Уже заблокирован" in named.text and "<img src=x" not in named.text
    # Clicks re-render the page from what YouGile said a moment ago: per request, only the
    # check that the admin is still one.
    assert set(fake.calls[calls:]) == {("GET", "/users/me")}

    no_project = await post(
        http, settings, "/admin/tickets/sender", {"tg_user_id": "100", "action": "approve"}
    )
    assert "Выберите проект" in no_project.text
    stranger = await post(
        http, settings, "/admin/tickets/sender", {"tg_user_id": "999", "action": "block"}
    )
    assert "Сотрудник не найден" in stranger.text
    stale = await post(
        http, settings, "/admin/tickets/sender", {"tg_user_id": "200", "action": "reject"}
    )
    assert "Ева: Запрос уже решён." in stale.text
    assert await store.open_actions() == []

    # The bot is not running: the request waits for it, and the page says so.
    admin_page.ACTION_WAIT, wait = 0.0, admin_page.ACTION_WAIT
    try:
        queued = await post(
            http,
            settings,
            "/admin/tickets/sender",
            {"tg_user_id": "100", "action": "approve", "project": f"{account.id}:p2"},
        )
    finally:
        admin_page.ACTION_WAIT = wait
    assert queued.headers["location"].endswith("done=tickets_queued")
    [request] = await store.open_actions()
    assert (request.tg_user_id, request.action, request.account_id, request.project_id) == (
        100,
        "approve",
        account.id,
        "p2",
    )
    assert request.user_id is not None
    page = await http.get("/admin/tickets")
    assert "ждёт бота: одобрение" in page.text
    await store.finish_action(request.id, True, "✅")

    # The bot carries it out while the page waits: what it says is what the admin sees.
    async def bot(ok: bool, line: str) -> None:
        while not (actions := await store.open_actions()):
            await asyncio.sleep(0.05)
        await store.finish_action(actions[0].id, ok, line)

    task = asyncio.create_task(bot(True, "🚫 Ева: заблокирован"))
    done = await post(
        http, settings, "/admin/tickets/sender", {"tg_user_id": "200", "action": "unblock"}
    )
    await task
    assert done.headers["location"].endswith("done=tickets_sender")
    task = asyncio.create_task(bot(False, "Не получилось"))
    failed = await post(
        http, settings, "/admin/tickets/sender", {"tg_user_id": "100", "action": "block"}
    )
    await task
    assert failed.status_code == 200 and "Анна: Не получилось." in failed.text
