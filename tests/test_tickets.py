"""The Telegram ticket bot against a fake Telegram, a fake YouGile and the real Postgres."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx2
import pytest
from conftest import DB_URL, FERNET_KEY, _reachable
from starlette.requests import Request

from yougile_cloud.crypto import Secrets
from yougile_cloud.db import Database
from yougile_cloud.settings import Settings
from yougile_cloud.tickets.bot import MENU, MINE, NEW, TicketBot, attachment, ids_in
from yougile_cloud.tickets.desk import Desks, text_html
from yougile_cloud.tickets.service import TicketService, TicketSettings, hook_secret
from yougile_cloud.tickets.store import TicketStore
from yougile_cloud.tickets.telegram import Telegram

PG_UP = "TEST_DATABASE_URL" in os.environ or _reachable("127.0.0.1", 55432)
ADMIN, ANNA, EVE = 1, 100, 200
SECRETS = Secrets([FERNET_KEY], "t" * 48)
FILE_MARK = "/root/#file:"


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []  # sendMessage bodies
        self.calls: list[str] = []
        self.next_id = 1000
        self.topics_on = False  # Threaded Mode in @BotFather
        self.refuse_topics = False  # createForumTopic fails
        self.dead_threads: set[int] = set()  # topics Telegram no longer knows
        self.topics: dict[int, str] = {}  # thread id: name
        self.deleted: list[int] = []  # deleteMessage
        self.deleted_topics: list[int] = []
        self.edits: list[dict] = []  # editMessageText
        self.reactions: list[dict] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if "/file/bot" in path:
            return httpx2.Response(200, content=b"PHOTO-BYTES")
        method = path.rsplit("/", 1)[-1]
        self.calls.append(method)
        body = json.loads(request.content) if request.content else {}
        result: Any = True
        if method == "sendMessage" and body.get("message_thread_id") in self.dead_threads:
            return httpx2.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "message thread not found"},
            )
        if method == "sendMessage":
            self.next_id += 1
            self.sent.append(body)
            result = {"message_id": self.next_id, "chat": {"id": body["chat_id"]}}
        elif method == "getMe":
            result = {"id": 1, "is_bot": True, "has_topics_enabled": self.topics_on}
        elif method == "createForumTopic":
            if self.refuse_topics:
                return httpx2.Response(
                    400,
                    json={
                        "ok": False,
                        "error_code": 400,
                        "description": "BOT_FORUM_CREATE_FORBIDDEN",
                    },
                )
            thread = 500 + len(self.topics)
            self.topics[thread] = body["name"]
            result = {"message_thread_id": thread, "name": body["name"], "icon_color": 0}
        elif method == "editForumTopic":
            self.topics[body["message_thread_id"]] = body["name"]
        elif method == "deleteForumTopic":
            self.deleted_topics.append(body["message_thread_id"])
            self.topics.pop(body["message_thread_id"], None)
        elif method == "deleteMessage":
            self.deleted.append(body["message_id"])
        elif method == "editMessageText":
            self.edits.append(body)
        elif method == "setMessageReaction":
            self.reactions.append(body)
        elif method == "getFile":
            result = {"file_id": body["file_id"], "file_path": f"photos/{body['file_id']}.jpg"}
        return httpx2.Response(200, json={"ok": True, "result": result})

    def to(self, chat: int) -> list[str]:
        return [m["text"] for m in self.sent if m["chat_id"] == chat]

    def last(self, chat: int) -> dict:
        return [m for m in self.sent if m["chat_id"] == chat][-1]


class FakeYouGile:
    """Two projects: the client's «Работы» and an internal one the bot must never report."""

    def __init__(self) -> None:
        self.projects = {"p-client": "Работы", "p-other": "Клиент Б", "p-internal": "Внутреннее"}
        self.boards = {  # id: (title, project)
            "b-req": ("Заявки", "p-client"),
            "b-hub": ("Хаб", "p-client"),
            "b-other": ("заявки", "p-other"),
            "b-int": ("Хаб", "p-internal"),
        }
        self.columns = {  # id: (title, board), in screen order
            "c-docs": ("Документы", "b-req"),
            "c-queue": ("Очередь", "b-req"),
            "c-work": ("В работе", "b-req"),
            "c-done": ("Готово", "b-req"),
            "c-hub": ("В работе", "b-hub"),
            "c-new": ("Новые", "b-other"),
            "c-internal": ("В работе", "b-int"),
        }
        self.tasks: dict[str, dict] = {}
        self.chats: dict[str, list[dict]] = {}
        self.uploads: list[str] = []
        self.hidden: set[str] = set()  # tasks the bot account cannot see (403)
        self.broken: set[str] = set()  # tasks whose reading fails (400)
        self.requests: list[str] = []
        self.clock = 2_000_000_000_000

    def message(self, chat: str, user: str, text: str) -> dict:
        self.clock += 1
        m = {"id": self.clock, "fromUserId": user, "text": text, "textHtml": "", "deleted": False}
        self.chats.setdefault(chat, []).append(m)
        return m

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/api-v2")
        self.requests.append(f"{request.method} {path}")
        body = json.loads(request.content) if request.content and path != "/upload-file" else {}
        if request.method == "POST" and path == "/tasks":
            n = len(self.tasks) + 1
            task = {"id": f"t-{n}", "idTaskCommon": f"ID-{n}", "completed": False, **body}
            self.tasks[task["id"]] = task
            return httpx2.Response(201, json={"id": task["id"]})
        if m := re.fullmatch(r"/tasks/([\w-]+)", path):
            if m[1] in self.hidden:
                return httpx2.Response(403, json={"message": "forbidden"})
            if m[1] in self.broken:
                return httpx2.Response(400, json={"message": "broken"})
            task = self.tasks.get(m[1])
            return httpx2.Response(200, json=task) if task else httpx2.Response(404, json={})
        if path in ("/projects", "/boards", "/columns"):
            items = {
                "/projects": [{"id": k, "title": v} for k, v in self.projects.items()],
                "/boards": [
                    {"id": k, "title": t, "projectId": p} for k, (t, p) in self.boards.items()
                ],
                "/columns": [
                    {"id": k, "title": t, "boardId": b} for k, (t, b) in self.columns.items()
                ],
            }[path]
            return httpx2.Response(200, json={"paging": {"next": False}, "content": items})
        if m := re.fullmatch(r"/columns/([\w-]+)", path):
            title, board = self.columns[m[1]]
            return httpx2.Response(200, json={"id": m[1], "title": title, "boardId": board})
        if m := re.fullmatch(r"/boards/([\w-]+)", path):
            title, project = self.boards[m[1]]
            return httpx2.Response(200, json={"id": m[1], "title": title, "projectId": project})
        if m := re.fullmatch(r"/users/([\w-]+)", path):
            return httpx2.Response(200, json={"id": m[1], "realName": "Ованес"})
        if path == "/upload-file":
            self.uploads.append(request.content.decode("latin-1"))
            return httpx2.Response(201, json={"url": "/user-data/x/photo.jpg", "fullUrl": "-"})
        if m := re.fullmatch(r"/chats/([\w-]+)/messages", path):
            if request.method == "POST":
                sent = self.message(m[1], "u-bot", body["text"])
                return httpx2.Response(201, json={"id": sent["id"]})
            since = int(request.url.params.get("since") or 0)
            items = [x for x in self.chats.get(m[1], []) if x["id"] > since]
            return httpx2.Response(200, json={"paging": {"next": False}, "content": items})
        return httpx2.Response(404, json={"message": f"no route {path}"})


@pytest.fixture
async def store():
    if not PG_UP:
        pytest.skip("test Postgres is not running")
    db = Database(DB_URL, max_size=2)
    await db.open()
    await db.migrate()
    await db._exec(
        "TRUNCATE ticket_bot, ticket_admin_topics, ticket_drafts, ticket_tg_messages, tickets, "
        "ticket_senders, ticket_accounts CASCADE"
    )
    yield TicketStore(db)
    await db.close()


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def yg() -> FakeYouGile:
    return FakeYouGile()


@pytest.fixture
async def bot(store, tg, yg):
    telegram = Telegram("123:abc", transport=httpx2.MockTransport(tg))
    desks = Desks(
        SECRETS, "https://yg.test", kv=None, rate_limit=45, transport=httpx2.MockTransport(yg)
    )
    yield TicketBot(telegram, store, desks, frozenset({ADMIN}))
    await desks.close()
    await telegram.aclose()


async def account(store: TicketStore):
    return await store.add_account(
        name="Indalo", company_id="c-1", bot_user_id="u-bot", api_key_enc=SECRETS.encrypt("k")
    )


_ids = iter(range(1, 10**6))


def text(uid: int, value: str, **extra: Any) -> dict:
    return {
        "update_id": next(_ids),
        "message": {
            "message_id": next(_ids),
            "from": {"id": uid, "first_name": "Anna", "username": "anna"},
            "chat": {"id": uid, "type": "private"},
            "text": value,
            **extra,
        },
    }


def photo(uid: int, caption: str | None = None) -> dict:
    update = text(uid, "")
    msg = update["message"]
    del msg["text"]
    msg["photo"] = [{"file_id": "small", "file_size": 10}, {"file_id": "big", "file_size": 999}]
    if caption:
        msg["caption"] = caption
    return update


def tap(uid: int, data: str, message: dict | None = None) -> dict:
    return {
        "update_id": next(_ids),
        "callback_query": {
            "id": str(next(_ids)),
            "from": {"id": uid},
            "data": data,
            "message": message or {"message_id": 1, "chat": {"id": uid}, "text": "…"},
        },
    }


def buttons(message: dict) -> list[str]:
    rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
    return [b["callback_data"] for row in rows for b in row]


async def approved(bot, store, tg, project: str = "p-client") -> int:  # noqa: ANN001
    a = await account(store)
    await bot.handle(text(ANNA, "/start"))
    await bot.handle(text(ANNA, "Анна, администратор, Подружки"))
    await bot.handle(tap(ADMIN, f"ok:{ANNA}:{a.id}:{project}"))
    return a.id


async def ticket(bot, tg) -> str:  # noqa: ANN001
    await bot.handle(text(ANNA, NEW))
    await bot.handle(text(ANNA, "Не приходят уведомления"))
    await bot.handle(text(ANNA, "С утра <b>не</b> приходят.\n\nНомер 89001112233"))
    await bot.handle(photo(ANNA, "скрин"))
    await bot.handle(tap(ANNA, "send"))
    return "t-1"


# ---------------- access ----------------


async def test_access_needs_an_approver(bot, store, tg):
    a = await account(store)
    await bot.handle(text(ANNA, NEW))  # not approved: asked who they are
    assert "Как вас зовут" in tg.to(ANNA)[-1]
    await bot.handle(text(ANNA, "Анна, Подружки"))
    assert (await store.sender(ANNA)).status == "pending"
    request = [m for m in tg.sent if m["chat_id"] == ADMIN][-1]
    assert "Анна, Подружки" in request["text"] and "@anna" in request["text"]
    # Projects with a «Заявки» board, by name; the internal one has none.
    assert buttons(request) == [
        f"ok:{ANNA}:{a.id}:p-other",
        f"ok:{ANNA}:{a.id}:p-client",
        f"no:{ANNA}",
    ]

    await bot.handle(tap(EVE, f"ok:{ANNA}:{a.id}:p-client"))  # not an approver
    assert (await store.sender(ANNA)).status == "pending"
    await bot.handle(text(ANNA, NEW))
    assert "рассматривается" in tg.to(ANNA)[-1]

    await bot.handle(tap(ADMIN, f"ok:{ANNA}:{a.id}:p-internal"))  # no «Заявки» there
    assert (await store.sender(ANNA)).status == "pending"
    await bot.handle(tap(ADMIN, f"ok:{ANNA}:{a.id}:p-client"))
    sender = await store.sender(ANNA)
    assert sender.approved and sender.project_id == "p-client"
    assert sender.project_name == "Работы"
    assert "Доступ открыт" in tg.to(ANNA)[-1]


async def test_rejected_and_blocked(bot, store, tg):
    await account(store)
    await bot.handle(text(EVE, "/start"))
    await bot.handle(text(EVE, "Ева"))
    await bot.handle(tap(ADMIN, f"no:{EVE}"))
    assert (await store.sender(EVE)).status == "rejected"
    assert "отказано" in tg.to(EVE)[-1]
    await store.decide(EVE, "blocked", by=None)
    before = len(tg.sent)
    await bot.handle(text(EVE, "/start"))
    assert len(tg.sent) == before  # silence


# ---------------- tickets ----------------


async def test_ticket_lands_in_the_queue_of_the_senders_project(bot, store, tg, yg):
    await approved(bot, store, tg)
    await ticket(bot, tg)
    task = yg.tasks["t-1"]
    assert task["title"] == "Не приходят уведомления"
    assert task["columnId"] == "c-queue"
    assert "&lt;b&gt;не&lt;/b&gt;" in task["description"]  # people's text is escaped
    assert "<p>Номер 89001112233</p>" in task["description"]
    assert "Анна, администратор, Подружки (Работы), Telegram @anna" in task["description"]
    assert "скрин" in task["description"]
    assert len(yg.uploads) == 1 and "PHOTO-BYTES" in yg.uploads[0]
    assert yg.chats["t-1"][-1]["text"] == FILE_MARK + "/user-data/x/photo.jpg"
    accepted = [m for m in tg.sent if m["chat_id"] == ANNA and "принята" in m["text"]][-1]
    assert "ID-1" in accepted["text"] and buttons(accepted) == ["reply:t-1"]
    assert await store.draft(ANNA) == {}

    await bot.handle(tap(ANNA, "send"))  # a second tap creates nothing
    assert len(yg.tasks) == 1
    await bot.handle(text(ANNA, MINE))
    assert "ID-1" in tg.to(ANNA)[-1]


async def test_column_changes_and_team_messages_are_reported(bot, store, tg, yg):
    await approved(bot, store, tg)
    await ticket(bot, tg)
    before = len(tg.to(ANNA))

    yg.tasks["t-1"]["columnId"] = "c-work"
    assert await bot.on_event({"event": "task-moved", "payload": {"id": "t-1"}}) == 1
    assert tg.to(ANNA)[-1] == "🔄 Заявка <b>ID-1</b> «Не приходят уведомления»: В работе."
    await bot.on_event({"event": "task-updated", "payload": {"id": "t-1"}})
    assert len(tg.to(ANNA)) == before + 1  # nothing new, nothing said

    yg.message("t-1", "u-bot", "the bot's own message")
    yg.message("t-1", "u-dev", "Посмотрим <сегодня>")
    await bot.on_event({"event": "chat_message-created", "payload": {"chatId": "t-1"}})
    assert tg.to(ANNA)[-1] == "💬 <b>ID-1</b> · <b>Ованес</b>:\nПосмотрим &lt;сегодня&gt;"
    await bot.on_event({"event": "chat_message-created", "payload": {"chatId": "t-1"}})
    assert len(tg.to(ANNA)) == before + 2

    yg.tasks["t-1"]["columnId"] = "c-done"
    await bot.refresh("t-1")
    assert "✅ Заявка <b>ID-1</b>" in tg.to(ANNA)[-1]
    assert (await store.ticket("t-1")).completed


async def test_each_sender_writes_to_their_own_project(bot, store, tg, yg):
    await approved(bot, store, tg, project="p-other")
    await ticket(bot, tg)
    task = yg.tasks["t-1"]
    assert task["columnId"] == "c-new"  # no «Очередь» there: the first column
    assert "(Клиент Б)" in task["description"]
    assert (await store.ticket("t-1")).project_id == "p-other"
    yg.tasks["t-1"]["columnId"] = "c-queue"  # into another client's project: silence
    await bot.refresh("t-1")
    assert "Очередь" not in tg.to(ANNA)[-1]


async def test_an_approver_moves_a_sender_to_another_project(bot, store, tg, yg):
    a = await approved(bot, store, tg)
    await ticket(bot, tg)
    await bot.handle(text(EVE, "/senders"))  # not an approver: just a stranger
    assert "Сотрудники" not in tg.to(EVE)[-1]

    await bot.handle(text(ADMIN, "/senders"))
    listing = [m for m in tg.sent if m["chat_id"] == ADMIN][-1]
    assert buttons(listing) == [f"mvl:{ANNA}"]
    assert "Работы" in listing["reply_markup"]["inline_keyboard"][0][0]["text"]
    await bot.handle(tap(ADMIN, f"mvl:{ANNA}"))
    offer = [m for m in tg.sent if m["chat_id"] == ADMIN][-1]
    assert buttons(offer) == [f"mv:{ANNA}:{a}:p-other"]  # the current project is not offered

    await bot.handle(tap(EVE, f"mv:{ANNA}:{a}:p-other"))
    assert (await store.sender(ANNA)).project_id == "p-client"
    await bot.handle(tap(ADMIN, f"mv:{ANNA}:{a}:p-internal"))  # no «Заявки» there
    assert (await store.sender(ANNA)).project_id == "p-client"

    await bot.handle(text(ANNA, NEW))  # a draft in progress is dropped by the move
    await bot.handle(tap(ADMIN, f"mv:{ANNA}:{a}:p-other"))
    sender = await store.sender(ANNA)
    assert sender.approved and sender.project_id == "p-other"
    assert sender.project_name == "Клиент Б"
    assert "«Клиент Б»" in tg.to(ANNA)[-1]
    assert await store.draft(ANNA) == {}

    await ticket(bot, tg)
    assert yg.tasks["t-2"]["columnId"] == "c-new"
    yg.tasks["t-1"]["columnId"] = "c-work"  # the old ticket stays in its project, still reported
    await bot.refresh("t-1")
    assert tg.to(ANNA)[-1].startswith("🔄 Заявка <b>ID-1</b>")


async def test_a_ticket_moved_to_another_board_of_its_project_is_still_reported(bot, store, tg, yg):
    await approved(bot, store, tg)
    await ticket(bot, tg)
    yg.tasks["t-1"]["columnId"] = "c-hub"
    await bot.refresh("t-1")
    assert tg.to(ANNA)[-1].endswith("В работе.")


async def test_a_task_moved_out_of_the_clients_project_is_never_reported(bot, store, tg, yg):
    await approved(bot, store, tg)
    await ticket(bot, tg)
    before = len(tg.to(ANNA))
    yg.tasks["t-1"]["columnId"] = "c-internal"
    yg.message("t-1", "u-dev", "внутренняя кухня")
    await bot.refresh("t-1")
    assert len(tg.to(ANNA)) == before


async def test_events_about_other_tasks_cost_no_requests(bot, store, tg, yg):
    await approved(bot, store, tg)
    await ticket(bot, tg)
    yg.requests.clear()
    assert await bot.on_event({"event": "task-moved", "payload": {"id": "someone-else"}}) == 0
    assert yg.requests == []


async def test_removed_task(bot, store, tg, yg):
    await approved(bot, store, tg)
    await ticket(bot, tg)
    yg.tasks["t-1"]["deleted"] = True
    await bot.refresh("t-1")
    assert "снята" in tg.to(ANNA)[-1]
    assert (await store.ticket("t-1")).deleted


async def test_a_task_out_of_sight_is_not_removed(bot, store, tg, yg):
    await approved(bot, store, tg)
    await ticket(bot, tg)
    before = len(tg.to(ANNA))
    yg.hidden.add("t-1")
    await bot.refresh("t-1")
    yg.hidden.clear()
    task = yg.tasks.pop("t-1")  # 404
    await bot.refresh("t-1")
    assert len(tg.to(ANNA)) == before
    assert not (await store.ticket("t-1")).deleted
    yg.tasks["t-1"] = {**task, "columnId": "c-work"}  # visible again: reported as usual
    await bot.refresh("t-1")
    assert tg.to(ANNA)[-1].endswith("В работе.")


async def test_one_failing_ticket_does_not_stop_the_others(bot, store, tg, yg):
    await approved(bot, store, tg)
    await ticket(bot, tg)
    await ticket(bot, tg)
    yg.broken.add("t-1")
    yg.tasks["t-2"]["columnId"] = "c-work"
    assert await bot.refresh_open() == 1
    assert tg.to(ANNA)[-1].startswith("🔄 Заявка <b>ID-2</b>")
    yg.tasks["t-2"]["columnId"] = "c-done"
    assert (
        await bot.on_event({"event": "task-moved", "payload": {"id": "t-1", "x": {"id": "t-2"}}})
        == 2
    )
    assert "✅ Заявка <b>ID-2</b>" in tg.to(ANNA)[-1]


async def test_replies_go_to_the_tickets_chat(bot, store, tg, yg):
    await approved(bot, store, tg)
    await ticket(bot, tg)
    accepted = [m for m in tg.sent if m["chat_id"] == ANNA and "принята" in m["text"]][-1]
    message_id = tg.next_id - 1  # «принята», then «Что-то ещё?»
    assert await store.ticket_of_message(ANNA, message_id) == "t-1", accepted

    await bot.handle(text(ANNA, "Уже работает", reply_to_message={"message_id": message_id}))
    posted = yg.chats["t-1"][-1]
    assert posted["fromUserId"] == "u-bot"
    assert (
        posted["text"] == "Анна, администратор, Подружки (Работы) пишет из Telegram:\nУже работает"
    )

    await bot.handle(tap(ANNA, "reply:t-1"))
    await bot.handle(text(ANNA, "И ещё"))
    assert yg.chats["t-1"][-1]["text"].endswith("И ещё")
    # The bot's own messages never come back to Telegram.
    before = len(tg.to(ANNA))
    await bot.refresh("t-1")
    assert len(tg.to(ANNA)) == before


async def test_someone_elses_ticket_is_out_of_reach(bot, store, tg, yg):
    a = await approved(bot, store, tg)
    await ticket(bot, tg)
    await store.request_access(EVE, "Ева", "")
    await store.decide(
        EVE, "approved", by=ADMIN, account_id=a, project_id="p-client", project_name="Работы"
    )
    await bot.handle(tap(EVE, "reply:t-1"))
    await bot.handle(text(EVE, "чужая"))
    assert all("чужая" not in m["text"] for m in yg.chats["t-1"])


# ---------------- topics ----------------


def in_topic(uid: int, thread: int, value: str) -> dict:
    update = text(uid, value)
    update["message"] |= {"message_thread_id": thread, "is_topic_message": True}
    return update


def photo_in_topic(uid: int, thread: int) -> dict:
    update = photo(uid)
    update["message"] |= {"message_thread_id": thread, "is_topic_message": True}
    return update


def tap_in(uid: int, data: str, thread: int) -> dict:
    message = {
        "message_id": 1,
        "chat": {"id": uid},
        "text": "…",
        "message_thread_id": thread,
        "is_topic_message": True,
    }
    return tap(uid, data, message)


async def test_in_topic_mode_a_new_topic_is_a_new_ticket(bot, store, tg, yg):
    tg.topics_on = True
    await approved(bot, store, tg)
    start = len(tg.sent)

    await bot.handle(in_topic(ANNA, 700, "Не приходят уведомления\nС утра, на Невском"))
    card = tg.last(ANNA)
    assert card["message_thread_id"] == 700 and "«Не приходят уведомления»" in card["text"]
    assert buttons(card) == ["send:700", "cancel:700"]
    first_card = tg.next_id
    await bot.handle(photo_in_topic(ANNA, 700))
    assert first_card in tg.deleted, "the card follows the latest message"
    assert "файлов: 1" in tg.last(ANNA)["text"] and yg.tasks == {}

    card = tg.next_id
    await bot.handle(tap_in(ANNA, "send:700", 700))
    task = yg.tasks["t-1"]
    assert task["title"] == "Не приходят уведомления" and "на Невском" in task["description"]
    assert len(yg.uploads) == 1
    assert (await store.ticket("t-1")).tg_thread_id == 700
    assert tg.topics[700] == "ID-1 · Не приходят уведомления"  # the topic takes the ticket's name
    assert tg.edits[-1]["message_id"] == card and "принята" in tg.edits[-1]["text"]
    assert await store.draft(ANNA, 700) == {}
    await bot.handle(tap_in(ANNA, "send:700", 700))  # a second tap creates nothing
    assert len(yg.tasks) == 1

    # The topic now talks to the ticket: a 👍 says the message got there.
    await bot.handle(in_topic(ANNA, 700, "Ещё деталь"))
    assert yg.chats["t-1"][-1]["text"].endswith("Ещё деталь")
    assert tg.reactions[-1]["reaction"] == [{"type": "emoji", "emoji": "👍"}]
    yg.tasks["t-1"]["columnId"] = "c-work"
    yg.message("t-1", "u-dev", "Какой браузер?")
    await bot.refresh("t-1")
    assert tg.last(ANNA)["message_thread_id"] == 700 and "Какой браузер?" in tg.last(ANNA)["text"]
    await bot.handle(in_topic(ANNA, 700, "/new"))
    assert "начните новую тему" in tg.last(ANNA)["text"]
    await bot.handle(in_topic(ANNA, 700, MINE))
    assert "ID-1" in tg.last(ANNA)["text"] and not buttons(tg.last(ANNA))

    sent = [m for m in tg.sent[start:] if m["chat_id"] == ANNA]
    assert sent and all(m.get("message_thread_id") == 700 for m in sent), "nothing outside topics"
    assert all(m.get("reply_markup") != MENU for m in sent), "no menu keyboard in topics"

    yg.tasks["t-1"]["columnId"] = "c-done"
    await bot.refresh("t-1")
    assert tg.topics[700] == "✅ ID-1 · Не приходят уведомления"  # kept as history, marked


async def test_drafts_in_two_topics_and_a_cancelled_one(bot, store, tg, yg):
    tg.topics_on = True
    await approved(bot, store, tg)
    await bot.handle(in_topic(ANNA, 701, "Первая"))
    await bot.handle(in_topic(ANNA, 702, "Вторая"))
    await bot.handle(tap_in(ANNA, "cancel:701", 701))
    assert 701 in tg.deleted_topics and await store.draft(ANNA, 701) == {}
    await bot.handle(tap_in(ANNA, "send:702", 702))
    assert [t["title"] for t in yg.tasks.values()] == ["Вторая"]
    await bot.handle(tap_in(ANNA, "send:701", 701))  # cancelled: nothing
    assert len(yg.tasks) == 1


async def test_access_in_topic_mode(bot, store, tg, yg):
    tg.topics_on = True
    a = await account(store)
    await bot.handle(in_topic(EVE, 900, "/start"))
    assert tg.last(EVE)["message_thread_id"] == 900 and "Как вас зовут" in tg.last(EVE)["text"]
    await bot.handle(in_topic(EVE, 900, "Ева, Подружки"))
    request = tg.last(ADMIN)
    thread = request["message_thread_id"]
    assert tg.topics[thread] == "🔑 Запросы доступа"
    await bot.handle(tap_in(ADMIN, f"ok:{EVE}:{a.id}:p-client", thread))
    told = tg.last(EVE)
    assert told["message_thread_id"] == 900 and "Доступ открыт" in told["text"]
    assert "новую тему" in told["text"] and told["reply_markup"] == {"remove_keyboard": True}

    await bot.handle(in_topic(ANNA, 901, "/start"))  # the next request: the same topic
    await bot.handle(in_topic(ANNA, 901, "Анна"))
    assert tg.last(ADMIN)["message_thread_id"] == thread
    assert list(tg.topics.values()).count("🔑 Запросы доступа") == 1
    tg.dead_threads.add(thread)  # the approver deleted the topic: a new one is made
    await bot.handle(in_topic(200 + 1, 902, "/start"))
    await bot.handle(in_topic(200 + 1, 902, "Кто-то"))
    assert tg.last(ADMIN)["message_thread_id"] not in (None, thread)


async def test_topics_with_the_old_main_chat_flow(bot, store, tg, yg):
    """A message outside topics while topics are on (an old client, say): the ticket still
    gets a topic of its own."""
    tg.topics_on = True
    await approved(bot, store, tg)
    await ticket(bot, tg)
    thread = (await store.ticket("t-1")).tg_thread_id
    assert tg.topics[thread] == "ID-1 · Не приходят уведомления"
    yg.tasks["t-1"]["deleted"] = True
    await bot.refresh("t-1")
    assert tg.topics[thread] == "✖ ID-1 · Не приходят уведомления"


async def test_without_topics_the_main_chat_is_used(bot, store, tg, yg):
    tg.topics_on, tg.refuse_topics = True, True
    await approved(bot, store, tg)
    await ticket(bot, tg)
    assert (await store.ticket("t-1")).tg_thread_id is None
    accepted = [m for m in tg.sent if "принята" in m["text"]][-1]
    assert buttons(accepted) == ["reply:t-1"] and "message_thread_id" not in accepted


async def test_a_topic_telegram_lost_falls_back_to_the_main_chat(bot, store, tg, yg):
    tg.topics_on = True
    await approved(bot, store, tg)
    await bot.handle(in_topic(ANNA, 700, "Вопрос по входу"))
    await bot.handle(tap_in(ANNA, "send:700", 700))
    tg.dead_threads.add(700)
    yg.message("t-1", "u-dev", "Вопрос")
    await bot.refresh("t-1")
    last = tg.last(ANNA)
    assert "Вопрос" in last["text"] and "message_thread_id" not in last
    assert buttons(last) == ["reply:t-1"]
    assert (await store.ticket("t-1")).tg_thread_id is None


# ---------------- webhook endpoint and helpers ----------------


def request(secret: str, body: bytes) -> Request:
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "path_params": {}}
    scope["path_params"] = {"secret": secret}

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def service_settings() -> Settings:
    return Settings(
        public_url="https://x.test",
        database_url=DB_URL,
        valkey_url="valkey://127.0.0.1:1",
        encryption_keys=[FERNET_KEY],
        jwt_secret="t" * 48,
    )


async def test_hook_checks_its_secret(store):
    service = TicketService(service_settings(), TicketSettings(None, frozenset()))
    await service.start(use_kv=False, poll=False)  # no token anywhere: idle
    try:
        assert service.bot is None
        assert (await service.hook(request("wrong", b"{}"))).status_code == 404
        assert (await service.hook(request(hook_secret("t" * 48), b"{}"))).status_code == 503
        assert hook_secret("t" * 48) != hook_secret("u" * 48)
    finally:
        await service.stop()


async def test_the_service_takes_its_settings_from_the_admin_page(store):
    service = TicketService(service_settings(), TicketSettings(None, frozenset({7})))
    await service.start(use_kv=False, poll=False)
    try:
        assert service.bot is None
        await store.save_bot_config(admins=[5], token_enc=SECRETS.encrypt("1:a"), bot_username="b")
        await service.apply_config()
        first = service.bot
        assert first is not None and first.admins == {5, 7}  # the page's and the environment's
        await store.save_bot_config(admins=[6])  # the token is kept: the same bot, new approvers
        await service.apply_config()
        assert service.bot is first and first.admins == {6, 7}
        assert service.secrets.decrypt((await store.bot_config()).token_enc) == "1:a"
        await store.save_bot_config(admins=[6], token_enc=SECRETS.encrypt("2:b"))
        await service.apply_config()
        assert service.bot is not None and service.bot is not first  # a new token: a new bot
    finally:
        await service.stop()


def test_ids_in_payloads():
    event = {"event": "chat_message-created", "payload": {"id": 17, "chatId": "t-1"}}
    assert ids_in(event) == ["t-1"]
    assert ids_in({"payload": {"id": "t-2", "prevData": {"columnId": "c"}}}) == ["t-2"]


def test_attachments_and_html():
    assert attachment({"message_id": 5, "photo": [{"file_id": "a"}, {"file_id": "b"}]}) == {
        "file_id": "b",
        "name": "photo_5.jpg",
        "size": 0,
    }
    assert attachment({"message_id": 5, "text": "hi"}) is None
    assert text_html("a <b>\nb\n\nc") == "<p>a &lt;b&gt;<br>b</p><p>c</p>"


def test_settings_from_env():
    s = TicketSettings.from_env({"TICKETS_BOT_TOKEN": " x ", "TICKETS_ADMINS": "1, 2"})
    assert s.bot_token == "x" and s.admins == frozenset({1, 2})
    assert TicketSettings.from_env({}).bot_token is None


async def test_command_menu_and_a_keyboard_that_stays(bot, tg):
    await bot.setup()
    assert tg.calls.count("setMyCommands") == 2  # everyone, and the approver's own
    assert MENU["is_persistent"] is True


async def test_service_messages_are_not_people(bot, store, tg):
    """Renaming a topic makes Telegram send a «topic edited» note «from» the bot itself: it must
    not be taken for a stranger and asked for a name."""
    await account(store)
    before = len(tg.sent)
    note = in_topic(8808924856, 700, "")
    del note["message"]["text"]
    note["message"]["from"] |= {"is_bot": True}
    note["message"]["forum_topic_edited"] = {"name": "ID-1 · Тест"}
    await bot.handle(note)
    created = in_topic(ANNA, 701, "")
    del created["message"]["text"]
    created["message"]["forum_topic_created"] = {"name": "Новая тема", "icon_color": 0}
    await bot.handle(created)
    assert len(tg.sent) == before
    assert await store.draft(8808924856) == {} and await store.draft(ANNA) == {}
