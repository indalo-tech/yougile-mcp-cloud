"""The ticket bot's conversations (Telegram side) and its reports on tickets (YouGile side)."""

from __future__ import annotations

import asyncio
import html
import logging
import time
import uuid
from collections import defaultdict
from contextvars import ContextVar
from typing import Any

from yougile_mcp.client import YouGileError

from .desk import Desk, Desks, Project, file_url, message_text, text_html
from .store import Account, Sender, Ticket, TicketStore
from .telegram import MAX_DOWNLOAD, Telegram, TelegramError, inline

log = logging.getLogger(__name__)

NEW = "📝 Новая заявка"
MINE = "📋 Мои заявки"
MENU = {
    "keyboard": [[{"text": NEW}, {"text": MINE}]],
    "resize_keyboard": True,
    "is_persistent": True,
}
COMMANDS = [
    {"command": "new", "description": "Новая заявка"},
    {"command": "my", "description": "Мои заявки"},
    {"command": "cancel", "description": "Отменить заявку, которую пишете"},
]
ADMIN_COMMANDS = [
    *COMMANDS,
    {"command": "senders", "description": "Сотрудники: доступ и проекты"},
    {"command": "id", "description": "Мой Telegram id"},
]
NO_MENU = {"remove_keyboard": True}
DONE_COLUMNS = {"готово"}
MAX_TITLE = 200
MAX_FILES = 10
MAX_TEXT = 3500  # Telegram allows 4096 characters per message
MAX_TOPIC = 128  # characters in a topic's name
TOPICS_RECHECK = 600.0  # seconds between checks that the bot's topics are on (@BotFather)
REASK_AFTER = 600  # seconds: a pending person writing again re-sends the access request
REJECTED_QUIET = 86_400  # seconds: a refused person cannot ask again sooner

# The topic the message being handled came from: answers go back there.
_thread: ContextVar[int | None] = ContextVar("ticket_bot_thread", default=None)
# The message being handled: with topics on, a message that came without a topic (one written
# in a topic the bot made) is answered as a reply to it, which keeps the answer in its topic.
_incoming: ContextVar[int | None] = ContextVar("ticket_bot_incoming", default=None)

ASK_NAME = (
    "Здравствуйте! Это бот для заявок команде Indalo.\n\n"
    "Как вас зовут и из какой вы компании? Ответьте одним сообщением — "
    "после подтверждения доступа сможете отправлять заявки."
)
PENDING = (
    "Пока доступа нет — заявки отправлять нельзя. Как только администратор одобрит запрос, "
    "я напишу."
)
HELD = " То, что вы пишете сейчас, я сохраню: после одобрения это сразу станет заявкой."
ASK_TITLE = "Коротко: что нужно сделать или что не работает? Одной строкой — это будет заголовок."
ASK_DETAILS = (
    "Опишите подробнее: где, что происходит, что ожидали. Можно приложить фото и файлы.\n"
    "Когда всё готово — нажмите «Отправить»."
)
HELP = (
    f"Чтобы отправить заявку — «{NEW}».\n"
    "Чтобы написать по заявке — ответьте на сообщение бота о ней или нажмите «Ответить» под ним."
)
THREAD_HELP = """📘 <b>Как пользоваться ботом</b>

<b>Отправить заявку</b>
1. Начните новую тему: значок ✏️ над списком тем или поле «Новая тема».
2. Напишите, что нужно сделать или что не работает. Первая строка станет заголовком, \
например: «Не приходят уведомления о записи». Заявка создаётся сразу, по первому сообщению.
3. Подробности — где и когда это происходит, что вы ожидали — пишите следом в ту же тему: \
текстом, скриншотами, фото, файлами, голосовыми (файл до 20 МБ). Всё уйдёт в заявку.

<b>Дальше</b>
• Тема получит номер и заголовок заявки, например «ID-1234 · Не приходят уведомления».
• В ней будут приходить смена статуса (в работе, на проверке, готово) и вопросы команды.
• Чтобы ответить или что-то дополнить, пишите в эту же тему: текст, фото, файлы. 👍 на \
вашем сообщении значит, что оно дошло до команды.
• Выполненная заявка остаётся в списке тем с пометкой ✅, снятая — с ✖. В закрытую тему \
тоже можно написать: команда увидит.

<b>Важно</b>
• Одна тема — одна заявка. Новую задачу начинайте новой темой, даже если она похожа на \
прежнюю.
• Дополнения к заявке пишите внутри её темы: сообщение в новой теме — это новая заявка.
• Ошиблись или заявка больше не нужна — напишите об этом в её теме, команда её снимет.
• Не присылайте пароли и данные банковских карт.
• Список ваших заявок — /my, эта инструкция — /help (кнопка «Меню»)."""
DESCRIPTION = (
    "Бот для заявок команде Indalo: задачи, ошибки, вопросы по нашим системам.\n\n"
    "Как начать: нажмите «Старт» и напишите, как вас зовут и из какой вы компании. После "
    "подтверждения доступа бот пришлёт инструкцию.\n\n"
    "Каждая заявка — отдельная тема: напишите в новой теме — заявка создастся сразу. В теме "
    "приходят статус и вопросы команды, туда же пишете ответы."
)
SHORT_DESCRIPTION = "Заявки команде Indalo: отправляйте задачи и следите за их статусом в Telegram."
NO_TOPIC = (
    "Не понял, к какой заявке это сообщение. Чтобы написать по заявке, ответьте на сообщение "
    "бота о ней (свайп влево по сообщению). Новая заявка — новая тема (✏️ или «Новая тема»)."
)
TOPIC_COMMANDS = [
    {"command": "help", "description": "Как пользоваться ботом"},
    {"command": "my", "description": "Мои заявки"},
]
TOPIC_ADMIN_COMMANDS = [
    *TOPIC_COMMANDS,
    {"command": "senders", "description": "Сотрудники: доступ и проекты"},
    {"command": "id", "description": "Мой Telegram id"},
]
NO_PROJECT = "Проект не найден или в нём больше нет доски «Заявки»"
REVOKED = (
    "Доступ к заявкам закрыт администратором. Если это ошибка — напишите сюда, как вас зовут и "
    "из какой вы компании: запрос уйдёт снова."
)
# Approvers' buttons: their code, and what each does to a person.
DECISIONS = {
    "ok": "approve",
    "no": "reject",
    "mv": "move",
    "rv": "revoke",
    "bl": "block",
    "ub": "unblock",
}
NEW_STATUS = {
    "approve": "approved",
    "reject": "rejected",
    "move": "approved",
    "revoke": "revoked",
    "block": "blocked",
    "unblock": "revoked",  # as after revoking: they may ask again, nothing more
}
ICON = {"pending": "⏳", "approved": "✅", "revoked": "⛔", "rejected": "❌", "blocked": "🚫"}
STATUS_TEXT = {
    "pending": "ждёт решения",
    "approved": "одобрен",
    "revoked": "доступ отозван",
    "rejected": "отклонён",
    "blocked": "заблокирован",
}
ACTION_TTL = 86_400  # seconds: an admin page request the bot did not get to by then is dropped
DRAFT_BUTTONS = inline([[("✅ Отправить", "send"), ("✖️ Отменить", "cancel")]])


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def clip(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def now_ms() -> int:
    return int(time.time() * 1000)


def attachment(msg: dict) -> dict | None:
    """The file in a message: {file_id, name, size}, or None."""
    n = msg.get("message_id")
    if doc := msg.get("document"):
        return {
            "file_id": doc["file_id"],
            "name": doc.get("file_name") or f"file_{n}",
            **_size(doc),
        }
    if photos := msg.get("photo"):
        return {"file_id": photos[-1]["file_id"], "name": f"photo_{n}.jpg", **_size(photos[-1])}
    for kind, name in (
        ("video", f"video_{n}.mp4"),
        ("voice", f"voice_{n}.ogg"),
        ("audio", f"audio_{n}.mp3"),
        ("video_note", f"video_{n}.mp4"),
    ):
        if item := msg.get(kind):
            return {
                "file_id": item["file_id"],
                "name": item.get("file_name") or name,
                **_size(item),
            }
    return None


def _size(item: dict) -> dict:
    return {"size": int(item.get("file_size") or 0)}


def thread_of(msg: dict) -> int | None:
    """The topic of a message in the private chat; None for the main one (General is 1).
    Telegram does not always mark a private topic's messages with is_topic_message, so the
    thread id alone counts."""
    thread = msg.get("message_thread_id")
    return thread if isinstance(thread, int) and thread != 1 else None


def describe(msg: dict) -> str:
    """What a message is, for the log — never what it says."""
    kinds = [k for k in ("text", "caption", "photo", "document", "voice", "video") if k in msg]
    service = [k for k in SERVICE_KEYS if k in msg]
    return (
        f"from={(msg.get('from') or {}).get('id')} thread={msg.get('message_thread_id')} "
        f"topic={msg.get('is_topic_message')} reply={'reply_to_message' in msg} "
        f"kinds={kinds} service={service} fields={sorted(msg)}"
    )


SERVICE_KEYS = (
    "forum_topic_created",
    "forum_topic_edited",
    "forum_topic_closed",
    "forum_topic_reopened",
    "general_forum_topic_hidden",
    "general_forum_topic_unhidden",
    "pinned_message",
    "new_chat_members",
    "left_chat_member",
)


def is_service(msg: dict) -> bool:
    """A message no person wrote: Telegram's notes about topics (renaming one included, which
    comes «from» the bot itself), pins and the like, or anything from a bot."""
    return bool((msg.get("from") or {}).get("is_bot")) or any(k in msg for k in SERVICE_KEYS)


def new_draft() -> dict:
    """A ticket being written. Its idempotency key is fixed now, before any send, so every
    attempt to send it is the same request to YouGile."""
    return {"step": "compose", "title": "", "parts": [], "files": [], "idem": uuid.uuid4().hex}


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def topic_buttons(thread: int) -> dict:
    return inline([[("✅ Отправить", f"send:{thread}"), ("✖️ Отменить", f"cancel:{thread}")]])


def draft_card(draft: dict) -> str:
    title = draft.get("title") or ""
    body = "\n\n".join(draft.get("parts") or [])
    files = len(draft.get("files") or [])
    lines = ["📝 <b>Черновик заявки</b>"]
    lines.append(f"«{esc(title)}»" if title else "<i>без заголовка — напишите, что случилось</i>")
    if body and body.strip() != title:
        lines.append(esc(clip(body, 600)))
    if files:
        lines.append(f"📎 файлов: {files}")
    lines.append("\nДопишите сюда ещё, если нужно, и нажмите «Отправить».")
    return "\n".join(lines)


def refused(action: str, sender: Sender) -> str | None:
    """Why an action does not apply to the person as they are now (None: it does)."""
    status = sender.status
    if action == "approve" and sender.approved:
        return f"Уже одобрен, проект: {sender.project_name}"
    if action == "approve" and status == "blocked":
        return "Заблокирован: сначала разблокируйте"
    if action == "reject" and status != "pending":
        return "Запрос уже решён"
    if action in ("move", "revoke") and not sender.approved:
        return "Доступа и так нет"
    if action == "block" and status == "blocked":
        return "Уже заблокирован"
    if action == "unblock" and status != "blocked":
        return "Не заблокирован"
    if action not in NEW_STATUS:
        return "Неизвестное действие"
    return None


def where(sender: Sender) -> str:
    """A person's project, or their status when they have none."""
    if sender.approved:
        return sender.project_name
    return STATUS_TEXT.get(sender.status, sender.status)


def topic_name(number: str, title: str, mark: str = "") -> str:
    return clip(f"{mark}{number} · {title}", MAX_TOPIC)


def is_done(task: dict, column_title: str) -> bool:
    return bool(task.get("completed")) or column_title.strip().lower() in DONE_COLUMNS


def shape(value: Any, depth: int = 0) -> Any:
    """The structure of a payload without its values: {key: shape} and type names."""
    if isinstance(value, dict) and depth < 3:
        return {k: shape(v, depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def ids_in(payload: Any, depth: int = 0) -> list[str]:
    """Object ids anywhere near the top of a webhook payload (its format is not documented)."""
    found: list[str] = []
    if depth > 3:
        return found
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("id", "taskId", "chatId", "objectId", "parentId") and isinstance(value, str):
                found.append(value)
            elif isinstance(value, dict | list):
                found += ids_in(value, depth + 1)
    elif isinstance(payload, list):
        for item in payload[:20]:
            found += ids_in(item, depth + 1)
    return list(dict.fromkeys(found))


class TicketBot:
    def __init__(
        self, tg: Telegram, store: TicketStore, desks: Desks, admins: frozenset[int]
    ) -> None:
        self.tg = tg
        self.store = store
        self.desks = desks
        self.admins = admins
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._topics: tuple[float, bool] | None = None
        # Tickets whose last report failed (Telegram unreachable): tried again every minute
        # by the service, until it goes through. A restart leaves it to the regular check.
        self.pending: set[str] = set()
        # Webhooks come in bursts (an edit is several events): one refresh per task at a time,
        # and one more if events arrived meanwhile — not one per event.
        self._busy: set[str] = set()
        self._dirty: dict[str, bool] = {}  # task id -> the rerun needs the chat too
        self._fresh: dict[str, float] = {}  # task id -> last successful refresh (monotonic)

    # ================= Telegram side =================

    async def handle(self, update: dict) -> None:
        msg = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        token = _thread.set(thread_of(msg))
        incoming = _incoming.set(msg.get("message_id") if update.get("message") else None)
        try:
            if update.get("message"):
                log.info("message %s", describe(msg))
                if (
                    (msg.get("chat") or {}).get("type") == "private"
                    and msg.get("from")
                    and not is_service(msg)
                ):
                    await self.on_message(msg)
            elif cq := update.get("callback_query"):
                await self.on_callback(cq)
        finally:
            _thread.reset(token)
            _incoming.reset(incoming)

    async def setup(self) -> None:
        """The command menu next to the input field (approvers get theirs as well)."""
        topics = await self.topics_enabled()
        try:
            await self.tg.call("setMyDescription", description=DESCRIPTION)
            await self.tg.call("setMyShortDescription", short_description=SHORT_DESCRIPTION)
            await self.tg.call("setMyCommands", commands=TOPIC_COMMANDS if topics else COMMANDS)
            for admin in self.admins:
                await self.tg.call(
                    "setMyCommands",
                    commands=TOPIC_ADMIN_COMMANDS if topics else ADMIN_COMMANDS,
                    scope={"type": "chat", "chat_id": admin},
                )
        except TelegramError as exc:
            log.info("setMyCommands: %s", exc)

    async def reply(self, chat: int, text: str, *, markup: dict | None = None) -> dict:
        """Answer where the person wrote: in the topic they wrote in, if any (topics have no
        menu keyboard: a new topic is the way to a new ticket)."""
        thread = _thread.get()
        topics = thread is not None or await self.topics_enabled()
        if topics and markup is MENU:
            markup = NO_MENU
        reply_to = _incoming.get() if topics and thread is None else None
        try:
            return await self.tg.send(chat, text, markup=markup, thread=thread, reply_to=reply_to)
        except TelegramError as exc:
            if not (thread or reply_to) or exc.code != 400:
                raise
            return await self.tg.send(chat, text, markup=markup)

    async def topics_enabled(self) -> bool:
        """Whether topics are on for the bot (Threaded Mode in @BotFather)."""
        now = time.monotonic()
        if self._topics is None or now - self._topics[0] > TOPICS_RECHECK:
            try:
                me = await self.tg.call("getMe")
                self._topics = (now, bool(me.get("has_topics_enabled")))
            except TelegramError as exc:
                # A blip must not flip the bot back to the main-chat flow for ten minutes:
                # keep what was known, ask again in a minute.
                log.info("getMe: %s", exc)
                known = self._topics[1] if self._topics else False
                self._topics = (now - TOPICS_RECHECK + 60, known)
        return self._topics[1]

    async def on_message(self, msg: dict) -> None:
        uid = msg["from"]["id"]
        chat = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        if text == "/id":
            await self.reply(chat, f"Ваш Telegram id: <code>{uid}</code>")
            return
        if text == "/senders" and uid in self.admins:
            await self.list_senders(chat)
            return
        sender = await self.store.sender(uid)
        if sender is None or not sender.approved:
            await self.stranger(msg, sender, text)
            return
        thread = thread_of(msg)
        if thread is not None:
            await self.in_topic(msg, sender, thread, text)
            return
        if await self.topics_enabled():
            await self.unplaced(msg, sender, text)
            return
        draft = await self.store.draft(uid)

        if text in ("/start", "/menu", "/help"):
            await self.store.save_draft(uid, {})
            await self.reply(chat, HELP, markup=MENU)
            return
        if text in ("/cancel", "Отмена", "Отменить"):
            await self.store.save_draft(uid, {})
            await self.reply(chat, "Отменено.", markup=MENU)
            return
        if text in (NEW, "/new"):
            await self.store.save_draft(uid, {"step": "title"})
            await self.reply(chat, ASK_TITLE)
            return
        if text in (MINE, "/my"):
            await self.list_tickets(chat, uid)
            return

        # A ticket being written takes everything; then a reply started with the button; then
        # the ticket's own topic; then a reply to a message about a ticket.
        step = draft.get("step")
        if step == "title":
            await self.take_title(chat, uid, draft, msg, text)
            return
        if step == "details":
            await self.take_details(chat, uid, draft, msg, text)
            return
        if step == "reply":
            await self.store.save_draft(uid, {})
            await self.relay(sender, msg, draft["task_id"])
            return
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_to and (task_id := await self.store.ticket_of_message(chat, reply_to)):
            await self.relay(sender, msg, task_id)
            return
        await self.reply(chat, HELP, markup=MENU)

    async def in_topic(self, msg: dict, sender: Sender, thread: int, text: str) -> None:
        """Topic mode: a ticket's topic talks to the ticket; any other topic is a ticket being
        written — each message adds to its draft."""
        uid, chat = sender.tg_user_id, msg["chat"]["id"]
        if text in ("/start", "/help", "/menu", "/new", NEW):
            # Also takes away the menu keyboard someone may still have from the main-chat days.
            await self.reply(chat, THREAD_HELP, markup=NO_MENU)
            return
        if text in ("/my", MINE):
            await self.list_tickets(chat, uid)
            return
        if task_id := await self.store.ticket_of_thread(chat, thread):
            await self.relay(sender, msg, task_id)
            return
        if text in ("/cancel", "Отмена", "Отменить"):
            await self.cancel_topic(chat, uid, thread)
            return
        # No draft and no «send» step: the first message of a new topic is the ticket.
        if await self.compose(chat, uid, thread, msg, text):
            await self.submit_topic(chat, sender, thread)

    async def unplaced(self, msg: dict, sender: Sender, text: str) -> None:
        """Topics on, yet no topic on the message: it was written in a topic the bot made (those
        come without their thread id). A reply to a message about a ticket still finds it."""
        chat = msg["chat"]["id"]
        if text in ("/my", MINE):
            await self.list_tickets(chat, sender.tg_user_id)
            return
        reply_to = (msg.get("reply_to_message") or {}).get("message_id")
        if reply_to and (task_id := await self.store.ticket_of_message(chat, reply_to)):
            await self.relay(sender, msg, task_id)
            return
        if text in ("/start", "/help", "/menu", "/new", NEW):
            await self.reply(chat, THREAD_HELP, markup=NO_MENU)
            return
        await self.reply(chat, NO_TOPIC, markup=NO_MENU)

    async def compose(self, chat: int, uid: int, thread: int, msg: dict, text: str) -> bool:
        """Add a message to the topic's draft (a new one, or one left by a failed send or by
        the card flow of before); True when the draft is ready to become the ticket."""
        content = text or (msg.get("caption") or "").strip()
        file = attachment(msg)
        if content.startswith("/"):
            content = ""
        if not content and not file:
            await self.reply(chat, "Пришлите текст, фото, голосовое или файл.")
            return False
        if file and file["size"] > MAX_DOWNLOAD:
            await self.reply(chat, "Файл больше 20 МБ: бот такие не принимает. Пришлите ссылку.")
            return False
        async with self._locks[f"draft:{uid}:{thread}"]:
            draft = await self.store.draft(uid, thread) or new_draft()
            seen = draft.setdefault("seen", [])
            if msg.get("message_id") in seen:
                return False  # the same update again (after a restart, say)
            if content:
                draft["title"] = draft["title"] or clip(first_line(content), MAX_TITLE)
                draft["parts"].append(content)
            if file and len(draft["files"]) < MAX_FILES:
                draft["files"].append(file)
            seen.append(msg.get("message_id"))
            # Saved before the ticket is made: a failed send must not lose what was written.
            await self.store.save_draft(uid, draft, thread)
        log.info(
            "draft of %s in topic %s: %d part(s), %d file(s)",
            uid,
            thread,
            len(draft["parts"]),
            len(draft["files"]),
        )
        return True

    async def cancel_topic(self, chat: int, uid: int, thread: int) -> None:
        if await self.store.ticket_of_thread(chat, thread):
            # An old card's «Отменить» after the ticket was made: the topic is the ticket's.
            await self.reply(chat, "Заявка уже отправлена — её тема остаётся.")
            return
        await self.store.save_draft(uid, {}, thread)
        try:
            await self.tg.call("deleteForumTopic", chat_id=chat, message_thread_id=thread)
        except TelegramError as exc:
            log.info("deleteForumTopic: %s", exc)
            await self.reply(chat, "Черновик отменён. Эту тему можно удалить.")

    async def submit_topic(self, chat: int, sender: Sender, thread: int) -> None:
        uid = sender.tg_user_id
        async with self._locks[f"draft:{uid}:{thread}"]:
            draft = await self.store.draft(uid, thread)
            if draft.get("step") != "compose":
                return  # sent already (a second tap) or cancelled
            title = draft.get("title") or "Заявка из Telegram"
            made = await self.create(chat, sender, title, draft)
            if made is None:
                return
            task, number, desk = made
            await self.store.update_ticket(task["id"], tg_thread_id=thread)
            # The messages it was made of count as delivered: one coming again (Telegram
            # redelivers after a restart) is not posted into the ticket a second time.
            for seen in draft.get("seen") or []:
                if seen:
                    await self.store.link_message(chat, seen, task["id"])
            await self.store.save_draft(uid, {}, thread)
        log.info("ticket %s created by %s from topic %s", number, uid, thread)
        failed = await self.upload(desk, task["id"], draft.get("files") or [])
        ticket = await self.store.ticket(task["id"])
        assert ticket is not None
        await self.rename_topic(ticket, topic_name(number, title))
        note = f"\n\nНе удалось приложить: {esc(', '.join(failed))}." if failed else ""
        text = (
            f"✅ Заявка <b>{esc(number)}</b> принята: «{esc(title)}».\n"
            f"Здесь — всё по ней: статус, вопросы команды. Пишите сюда, сообщения уйдут в "
            f"заявку.{note}"
        )
        try:
            await self.tg.call(
                "editMessageText",
                chat_id=chat,
                message_id=draft["card"],
                text=text,
                parse_mode="HTML",
            )
            await self.store.link_message(chat, draft["card"], task["id"])
        except (TelegramError, KeyError) as exc:
            log.info("editMessageText: %s", exc)
            try:
                await self.say(chat, task["id"], text, thread=thread)
            except TelegramError as again:  # the ticket is made: the reply is a courtesy
                log.info("cannot confirm %s: %s", number, again)

    async def create(
        self, chat: int, sender: Sender, title: str, draft: dict
    ) -> tuple[dict, str, Desk] | None:
        """The ticket's task and row; None (and the sender told) when it failed. The draft's
        idempotency key makes a repeat — a second tap, a retry after a timeout — return the
        same task. Files are the caller's next step, once the draft is cleared."""
        account = await self.store.account(sender.account_id or 0)
        desk = self.desks.get(account) if account else None
        project = await self.project_of(desk, sender) if desk else None
        if account is None or desk is None or project is None:
            await self.reply(chat, "Не нашёл, куда отправить заявку: напишите администратору бота.")
            return None
        body = "\n\n".join(draft.get("parts") or []) or title
        signature = f"{sender.name} ({sender.project_name})" + (
            f", Telegram @{sender.username}" if sender.username else ", Telegram"
        )
        description = text_html(body) + f"<p><i>Заявка из Telegram: {esc(signature)}</i></p>"
        try:
            task = await desk.create_task(
                project.column_id, title, description, draft.get("idem") or uuid.uuid4().hex
            )
        except YouGileError as exc:
            log.warning("cannot create a ticket in %s: %s", project.title, exc)
            await self.reply(
                chat,
                "Не получилось создать заявку. Напишите в эту тему ещё раз чуть позже — "
                "всё написанное сохранено, заявка создастся.",
            )
            return None
        number = task.get("idTaskCommon") or task["id"]
        await self.store.add_ticket(
            task_id=task["id"],
            account_id=account.id,
            project_id=project.id,
            tg_user_id=sender.tg_user_id,
            number=number,
            title=title,
            column_id=task.get("columnId") or project.column_id,
            last_message_id=now_ms(),
        )
        return task, number, desk

    async def help_text(self) -> str:
        return THREAD_HELP if await self.topics_enabled() else HELP

    async def tell(self, tg_user_id: int, text: str) -> None:
        """A message to a sender about their access: into the topic they asked in, if topics are
        on; otherwise into the chat, with the menu keyboard."""
        sender = await self.store.sender(tg_user_id)
        topics = await self.topics_enabled()
        thread = sender.tg_thread_id if sender and topics else None
        markup = NO_MENU if topics else MENU
        try:
            try:
                await self.tg.send(tg_user_id, text, markup=markup, thread=thread)
            except TelegramError as exc:
                if not thread or exc.code != 400:
                    raise
                await self.tg.send(tg_user_id, text, markup=markup)
        except TelegramError as exc:
            log.info("cannot tell sender %s: %s", tg_user_id, exc)

    async def admin_thread(self, admin: int) -> int | None:
        """The approver's «access requests» topic (made once), when topics are on."""
        if not await self.topics_enabled():
            return None
        thread = await self.store.admin_topic(admin)
        if thread is None:
            thread = await self.open_topic(admin, "🔑 Запросы доступа")
            if thread:
                await self.store.set_admin_topic(admin, thread)
        return thread

    async def to_admin(self, admin: int, text: str, markup: dict) -> None:
        for attempt in (1, 2):
            thread = await self.admin_thread(admin)
            try:
                await self.tg.send(admin, text, markup=markup, thread=thread)
                return
            except TelegramError as exc:
                if thread and exc.code == 400 and attempt == 1:
                    await self.store.set_admin_topic(admin, None)  # the topic is gone: anew
                    continue
                raise

    async def stranger(self, msg: dict, sender: Sender | None, text: str) -> None:
        """Someone without access: ask who they are, pass the request to the approvers."""
        uid, chat = msg["from"]["id"], msg["chat"]["id"]
        if sender and sender.status == "blocked":
            return
        if (
            sender
            and sender.status == "rejected"
            and sender.decided_at
            and time.time() - sender.decided_at.timestamp() < REJECTED_QUIET
        ):
            # Refused today: no new request to the approvers on every message.
            await self.reply(chat, "В доступе отказано. Попробовать снова можно завтра.")
            return
        if sender and sender.status == "pending":
            # The request may never have reached an approver (Telegram down at the time) or
            # been missed: a person writing again after a while sends it once more.
            async with self._locks[f"draft:{uid}:0"]:
                note = await self.store.draft(uid)
                again = time.time() - float(note.get("asked") or 0) > REASK_AFTER
                if again:  # what is held stays: only the time of asking changes
                    await self.store.save_draft(
                        uid, {**note, "step": "pending", "asked": time.time()}
                    )
            if again:
                await self.ask_approvers(sender, msg["from"])
            held = await self.hold(msg, sender, text)
            log.info("pending sender %s wrote (kept: %s)", uid, held)
            await self.reply(chat, PENDING + (HELD if held else ""))
            return
        draft = await self.store.draft(uid)
        if draft.get("step") != "intro" or not text or text.startswith("/"):
            await self.store.save_draft(uid, {"step": "intro"})
            await self.reply(chat, ASK_NAME, markup=NO_MENU)
            return
        user = msg["from"]
        sender = await self.store.request_access(
            uid, clip(text, 200), user.get("username") or "", thread=thread_of(msg)
        )
        await self.store.save_draft(uid, {"step": "pending", "asked": time.time()})
        await self.ask_approvers(sender, user)
        topics = await self.topics_enabled()
        await self.reply(
            chat,
            "Спасибо! Запрос на доступ отправлен. "
            + PENDING
            + (" Можете уже описать проблему — сохраню до одобрения." if topics else ""),
        )

    async def hold(self, msg: dict, sender: Sender, text: str) -> bool:
        """Topic mode: what a person writes while waiting for access is kept, per topic, and
        becomes a draft once they are let in — Telegram gives a bot no history to read later.
        Returns whether the message was kept."""
        if not await self.topics_enabled():
            return False
        thread = thread_of(msg) or sender.tg_thread_id
        content = text or (msg.get("caption") or "").strip()
        if content.startswith("/"):
            content = ""
        file = attachment(msg)
        if file and file["size"] > MAX_DOWNLOAD:
            file = None
        if not thread or (not content and not file):
            return False
        uid = sender.tg_user_id
        async with self._locks[f"draft:{uid}:0"]:
            note = await self.store.draft(uid)
            draft = note.setdefault("held", {}).setdefault(str(thread), new_draft())
            seen = draft.setdefault("seen", [])
            if msg.get("message_id") in seen:
                return True  # the same update again
            if content:
                draft["title"] = draft["title"] or clip(first_line(content), MAX_TITLE)
                draft["parts"].append(content)
            if file and len(draft["files"]) < MAX_FILES:
                draft["files"].append(file)
            seen.append(msg.get("message_id"))
            note.setdefault("step", "pending")
            await self.store.save_draft(uid, note)
        return True

    async def release_held(self, sender: Sender, held: dict) -> int:
        """After approval: what was written in each topic while waiting becomes its ticket at
        once, as a first message there would. A topic that has a draft or a ticket already
        keeps it. Returns how many tickets were made."""
        uid, made = sender.tg_user_id, 0
        for key, draft in held.items():
            thread = int(key)
            if not draft.get("parts") and not draft.get("files"):
                continue
            if await self.store.ticket_of_thread(uid, thread):
                continue
            async with self._locks[f"draft:{uid}:{thread}"]:
                if await self.store.draft(uid, thread):
                    continue
                await self.store.save_draft(uid, draft, thread)
            # Answers of this step belong in that topic, not in the approver's.
            token = _thread.set(thread)
            incoming = _incoming.set(None)
            try:
                await self.submit_topic(uid, sender, thread)
            except Exception:
                log.exception("a held ticket of %s in topic %s failed", uid, thread)
                continue  # its draft stays: the person's next message there sends it
            finally:
                _thread.reset(token)
                _incoming.reset(incoming)
            made += 1
        return made

    async def choices(self) -> list[tuple[str, str]]:
        """Approval buttons: one per project with a «Заявки» board, in every account."""
        accounts = await self.store.accounts()
        buttons: list[tuple[str, str]] = []
        for account in accounts:
            try:
                projects = await self.desks.get(account).projects(fresh=True)
            except YouGileError as exc:
                log.warning("cannot list projects of %s: %s", account.name, exc)
                continue
            for p in sorted(projects.values(), key=lambda p: p.title.lower()):
                label = f"{account.name} / {p.title}" if len(accounts) > 1 else p.title
                buttons.append((label, f"{account.id}:{p.id}"))
        return buttons

    async def ask_approvers(self, sender: Sender, user: dict) -> None:
        who = esc(sender.name)
        tg_name = esc(" ".join(filter(None, [user.get("first_name"), user.get("last_name")])))
        handle = f" @{esc(sender.username)}" if sender.username else ""
        text = (
            f"Запрос доступа к заявкам:\n<b>{who}</b>\n"
            f"Telegram: {tg_name}{handle} (id <code>{sender.tg_user_id}</code>)\n\n"
            "В какой проект пойдут его (её) заявки? Они попадут на доску «Заявки» проекта.\n"
            "Перевести потом в другой проект, отозвать доступ, заблокировать — /senders или "
            "страница бота в админке."
        )
        choices = await self.choices()
        if not choices:
            text += "\n\n⚠️ Ни в одном проекте нет доски «Заявки»: заведите её и нажмите снова."
        rows = [[(f"✅ {label}", f"ok:{sender.tg_user_id}:{ref}")] for label, ref in choices]
        rows.append([("❌ Отклонить", f"no:{sender.tg_user_id}")])
        for admin in self.admins:
            try:
                await self.to_admin(admin, text, inline(rows))
            except TelegramError as exc:  # an approver who never started the bot
                log.warning("cannot notify approver %s: %s", admin, exc)

    async def on_callback(self, cq: dict) -> None:
        uid = cq["from"]["id"]
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id", uid)
        answer: str | None = None
        try:
            if data.startswith(("ok:", "no:", "mv:", "rv:", "bl:", "ub:")):
                answer = await self.decide(uid, data, msg)
            elif data.startswith("who:"):
                answer = await self.sender_card(uid, chat, int(data.removeprefix("who:")))
            elif data.startswith("mvl:"):
                answer = await self.offer_move(uid, chat, int(data.removeprefix("mvl:")))
            else:
                sender = await self.store.sender(uid)
                if sender is None or not sender.approved:
                    answer = "Нет доступа"
                elif data == "send":
                    await self.submit(chat, sender)
                elif data == "cancel":
                    await self.store.save_draft(uid, {})
                    await self.reply(chat, "Отменено.", markup=MENU)
                elif data.startswith("send:") and data[5:].isdigit():
                    await self.submit_topic(chat, sender, int(data[5:]))
                elif data.startswith("cancel:") and data[7:].isdigit():
                    await self.cancel_topic(chat, uid, int(data[7:]))
                elif data.startswith("reply:"):
                    answer = await self.start_reply(chat, sender, data.removeprefix("reply:"))
        finally:
            try:
                await self.tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=answer)
            except TelegramError as exc:
                log.info("answerCallbackQuery: %s", exc)

    async def decide(self, uid: int, data: str, msg: dict) -> str:
        """An approver's button about a person: ``<code>:<tg id>[:<account id>:<project id>]``."""
        if uid not in self.admins:
            return "Только для администраторов"
        code, target, *ref = data.split(":")
        ok, verdict = await self.manage(int(target), DECISIONS[code], ref, by=uid)
        if not ok:
            return verdict
        await self.mark(msg, verdict)
        return "Готово"

    async def manage(
        self,
        target: int,
        action: str,
        ref: list[str] | None = None,
        *,
        by: int | None = None,
        user_id: int | None = None,
    ) -> tuple[bool, str]:
        """What an approver decides about a person — from the bot's buttons (``by``, their
        Telegram id) or the admin page (``user_id``, the admin there). ``ref`` is the
        ``<account id>, <project id>`` of approve and move. Returns whether it was done and
        one line about it for the approver."""
        async with self._locks[f"access:{target}"]:
            sender = await self.store.sender(target)
            if sender is None:
                return False, "Сотрудник не найден"
            refusal = refused(action, sender)
            if refusal:
                return False, refusal
            account = project = None
            if action in ("approve", "move"):
                account, project = await self.chosen(ref or [])
                if account is None or project is None:
                    return False, NO_PROJECT
                if action == "move" and (account.id, project.id) == (
                    sender.account_id,
                    sender.project_id,
                ):
                    return False, f"Уже в проекте «{sender.project_name}»"
            status = NEW_STATUS[action]
            done = await self.store.decide(
                target,
                status,
                by=by,
                account_id=account.id if account else None,
                project_id=project.id if project else None,
                project_name=project.title if project else "",
            )
            assert done is not None
            company = account.company_id if account else None
            if company is None and sender.account_id:
                previous = await self.store.account(sender.account_id)
                company = previous.company_id if previous else None
            await self.store.db.audit(
                "tickets_sender",
                company_id=company,
                user_id=user_id,
                action=action,
                tg_user_id=target,
                approver_tg=by,
                project=project.title if project else None,
            )
            log.info("sender %s: %s (%s -> %s)", target, action, sender.status, status)

            name = sender.name
            if action == "approve":
                assert project is not None
                held = (await self.store.draft(target)).get("held") or {}
                await self.store.save_draft(target, {})  # the «request pending» note
                help_text = await self.help_text()
                waiting = (
                    "\n\nТо, что вы писали, пока ждали доступа, я отправил заявками — каждая в "
                    "своей теме."
                    if any(d.get("parts") or d.get("files") for d in held.values())
                    else ""
                )
                await self.tell(
                    target,
                    f"Доступ открыт — теперь можно оставлять заявки.{waiting}\n\n{help_text}",
                )
                await self.release_held(done, held)
                return True, f"✅ Одобрено, проект: {project.title}"
            if action == "reject":
                await self.store.save_draft(target, {})
                await self.tell(target, "К сожалению, в доступе отказано.")
                return True, "❌ Отклонено"
            if action == "move":
                assert project is not None
                await self.store.drop_drafts(target)  # half-written tickets were for the old one
                await self.tell(
                    target,
                    f"Теперь ваши заявки идут в проект «{esc(project.title)}». "
                    "Уже отправленные остаются там, где были.",
                )
                return True, f"✅ {name}: {sender.project_name} → {project.title}"
            await self.store.drop_drafts(target)  # nothing written from now on is a ticket
            if action == "revoke":
                # Told to introduce themselves: their next message is that, not a greeting.
                await self.store.save_draft(target, {"step": "intro"})
                await self.tell(target, REVOKED)
                return True, f"⛔ {name}: доступ отозван"
            if action == "block":  # said nothing: the bot simply stops answering
                return True, f"🚫 {name}: заблокирован"
            return True, f"↩️ {name}: разблокирован — может снова запросить доступ"

    async def run_actions(self) -> int:
        """Carry out what the admin page asked for (see ``ticket_actions``)."""
        done = 0
        for a in await self.store.open_actions():
            if time.time() - a.created_at.timestamp() > ACTION_TTL:
                await self.store.finish_action(a.id, False, "Не выполнено: бот долго не работал")
                continue
            ref = [str(a.account_id), a.project_id or ""] if a.account_id else []
            try:
                ok, result = await self.manage(a.tg_user_id, a.action, ref, user_id=a.user_id)
            except Exception:
                log.exception("admin page action %s failed", a.id)
                ok, result = False, "Не получилось: ошибка в боте, подробности в журнале"
            await self.store.finish_action(a.id, ok, result)
            done += 1
        return done

    async def chosen(self, ref: list[str]) -> tuple[Account | None, Project | None]:
        """The account and project of a button's ``<account id>:<project id>``."""
        if len(ref) != 2 or not ref[0].isdigit():
            return None, None
        account = await self.store.account(int(ref[0]))
        if account is None:
            return None, None
        projects = await self.desks.get(account).projects(fresh=True)
        return account, projects.get(ref[1])

    async def mark(self, msg: dict, verdict: str) -> None:
        """Append the decision to an approver's message (plain text: no markup to escape)."""
        if not msg.get("message_id"):
            return
        try:
            await self.tg.call(
                "editMessageText",
                chat_id=msg["chat"]["id"],
                message_id=msg["message_id"],
                text=f"{msg.get('text') or ''}\n\n{verdict}",
            )
        except TelegramError as exc:
            log.info("editMessageText: %s", exc)

    async def list_senders(self, chat: int) -> None:
        senders = await self.store.senders()
        if not senders:
            await self.reply(chat, "Доступ к заявкам пока никто не просил.")
            return
        order = {"pending": 0, "approved": 1, "revoked": 2, "rejected": 3, "blocked": 4}
        senders.sort(key=lambda s: (order.get(s.status, 5), s.name.lower()))
        rows = [
            [(f"{ICON.get(s.status, '')} {clip(s.name, 40)} — {where(s)}", f"who:{s.tg_user_id}")]
            for s in senders[:90]  # Telegram allows 100 buttons per message
        ]
        await self.reply(
            chat,
            "Сотрудники в боте. Нажмите на человека — откроются действия: перевести в другой "
            "проект, одобрить, отозвать доступ, заблокировать.\n"
            "⏳ ждёт решения · ✅ одобрен · ⛔ доступ отозван · ❌ отклонён · 🚫 заблокирован",
            markup=inline(rows),
        )

    async def sender_card(self, uid: int, chat: int, target: int) -> str | None:
        """One person and what can be done about them now."""
        if uid not in self.admins:
            return "Только для администраторов"
        s = await self.store.sender(target)
        if s is None:
            return "Сотрудник не найден"
        handle = f" @{esc(s.username)}" if s.username else ""
        text = (
            f"<b>{esc(s.name)}</b>{handle} (id <code>{s.tg_user_id}</code>)\n"
            f"{ICON.get(s.status, '')} {STATUS_TEXT.get(s.status, s.status)}"
            + (f", проект: {esc(s.project_name)}" if s.approved else "")
        )
        rows: list[list[tuple[str, str]]] = []
        if s.approved:
            rows.append([("🔀 Перевести в другой проект", f"mvl:{target}")])
            rows.append([("⛔ Отозвать доступ", f"rv:{target}")])
        elif s.status != "blocked":
            rows += [[(f"✅ {label}", f"ok:{target}:{ref}")] for label, ref in await self.choices()]
            if s.status == "pending":
                rows.append([("❌ Отклонить", f"no:{target}")])
        if s.status == "blocked":
            rows.append([("↩️ Разблокировать", f"ub:{target}")])
        else:
            rows.append([("🚫 Заблокировать", f"bl:{target}")])
        await self.reply(chat, text, markup=inline(rows))
        return None

    async def offer_move(self, uid: int, chat: int, target: int) -> str | None:
        if uid not in self.admins:
            return "Только для администраторов"
        sender = await self.store.sender(target)
        if sender is None or not sender.approved:
            return "Сотрудник не найден"
        choices = [
            (label, ref)
            for label, ref in await self.choices()
            if ref != f"{sender.account_id}:{sender.project_id}"
        ]
        if not choices:
            return "Других проектов с доской «Заявки» нет"
        rows = [[(f"➡️ {label}", f"mv:{target}:{ref}")] for label, ref in choices]
        await self.reply(
            chat,
            f"Куда перевести <b>{esc(sender.name)}</b>? Сейчас: {esc(sender.project_name)}.\n"
            "Новые заявки пойдут в выбранный проект, отправленные останутся где были.",
            markup=inline(rows),
        )
        return None

    async def take_title(self, chat: int, uid: int, draft: dict, msg: dict, text: str) -> None:
        if not text or text.startswith("/"):
            await self.reply(chat, "Сначала напишите заголовок текстом — одной строкой.")
            return
        title = clip(" ".join(text.split()), MAX_TITLE)
        await self.store.save_draft(uid, {**new_draft(), "step": "details", "title": title})
        await self.reply(chat, ASK_DETAILS, markup=DRAFT_BUTTONS)

    async def take_details(self, chat: int, uid: int, draft: dict, msg: dict, text: str) -> None:
        caption = (msg.get("caption") or "").strip()
        file = attachment(msg)
        notes: list[str] = []
        if text or caption:
            draft["parts"].append(text or caption)
            notes.append("текст")
        if file:
            if file["size"] > MAX_DOWNLOAD:
                await self.reply(
                    chat, "Файл больше 20 МБ: бот такие не принимает. Пришлите ссылку."
                )
                return
            if len(draft["files"]) >= MAX_FILES:
                await self.reply(chat, f"Не больше {MAX_FILES} файлов на заявку.")
                return
            draft["files"].append(file)
            notes.append(f"файл {esc(file['name'])}")
        if not notes:
            await self.reply(
                chat, "Такое сообщение бот не понимает: пришлите текст, фото или файл."
            )
            return
        await self.store.save_draft(uid, draft)
        await self.reply(
            chat,
            f"Добавлено: {', '.join(notes)}. Ещё что-то — или «Отправить».",
            markup=DRAFT_BUTTONS,
        )

    async def submit(self, chat: int, sender: Sender) -> None:
        uid = sender.tg_user_id
        async with self._locks[f"user:{uid}"]:
            draft = await self.store.draft(uid)
            if draft.get("step") != "details":
                return  # sent already (a second tap) or cancelled
            title = draft["title"]
            made = await self.create(chat, sender, title, draft)
            if made is None:
                return  # the draft stays, with its key: «Отправить» again is safe
            task, number, desk = made
            await self.store.save_draft(uid, {})
            failed = await self.upload(desk, task["id"], draft.get("files") or [])
            note = f"\n\nНе удалось приложить: {esc(', '.join(failed))}." if failed else ""
            await self.say(
                chat,
                task["id"],
                f"✅ Заявка <b>{esc(number)}</b> принята: «{esc(title)}».\n"
                f"Напишу, когда она сдвинется. Ответить по ней — кнопкой ниже "
                f"или ответом на это сообщение.{note}",
            )
            await self.reply(chat, "Что-то ещё?", markup=MENU)

    async def open_topic(self, chat: int, name: str) -> int | None:
        """A topic the bot makes (the approvers' one), or None (topics off or refused). Not for
        tickets: messages written in a topic the bot made reach it without their thread id."""
        if not await self.topics_enabled():
            return None
        try:
            topic = await self.tg.call("createForumTopic", chat_id=chat, name=name)
        except TelegramError as exc:
            log.info("createForumTopic: %s", exc)
            return None
        thread = int((topic or {}).get("message_thread_id") or 0)
        return thread or None

    async def rename_topic(self, ticket: Ticket, name: str) -> None:
        if not ticket.tg_thread_id:
            return
        try:
            await self.tg.call(
                "editForumTopic",
                chat_id=ticket.tg_user_id,
                message_thread_id=ticket.tg_thread_id,
                name=name,
            )
        except TelegramError as exc:
            log.info("editForumTopic for %s: %s", ticket.number, exc)

    async def project_of(self, desk: Desk, sender: Sender) -> Project | None:
        projects = await desk.projects()
        if sender.project_id not in projects:  # a board renamed or added a minute ago
            projects = await desk.projects(fresh=True)
        return projects.get(sender.project_id or "")

    async def upload(self, desk: Desk, task_id: str, files: list[dict]) -> list[str]:
        """Attach Telegram files to the ticket's chat; returns the names that failed."""
        failed: list[str] = []
        for file in files:
            try:
                data = await self.tg.download(file["file_id"])
                await desk.attach(task_id, file["name"], data)
            except (TelegramError, YouGileError) as exc:
                log.warning("attaching a file failed: %s", exc)
                failed.append(file["name"])
        return failed

    async def start_reply(self, chat: int, sender: Sender, task_id: str) -> str | None:
        ticket = await self.store.ticket(task_id)
        if ticket is None or ticket.tg_user_id != sender.tg_user_id:
            return "Заявка не найдена"
        await self.store.save_draft(sender.tg_user_id, {"step": "reply", "task_id": task_id})
        await self.reply(chat, f"Напишите сообщение по заявке <b>{esc(ticket.number)}</b>:")
        return None

    async def relay(self, sender: Sender, msg: dict, task_id: str) -> None:
        """A sender's message about their ticket goes to the ticket's chat in YouGile."""
        chat = msg["chat"]["id"]
        ticket = await self.store.ticket(task_id)
        if ticket is None or ticket.tg_user_id != sender.tg_user_id or ticket.deleted:
            await self.reply(chat, "Эта заявка недоступна.", markup=MENU)
            return
        account = await self.store.account(ticket.account_id)
        assert account is not None
        desk = self.desks.get(account)
        text = (msg.get("text") or msg.get("caption") or "").strip()
        file = attachment(msg)
        if not text and not file:
            await self.reply(chat, "Пришлите текст, фото или файл.")
            return
        mid = msg.get("message_id")
        if mid and await self.store.ticket_of_message(chat, mid) == task_id:
            return  # delivered already: the same update again
        header = f"{sender.name} ({sender.project_name}) пишет из Telegram:"
        try:
            await desk.post(task_id, f"{header}\n{text}" if text else f"{header} файл")
            if mid:
                await self.store.link_message(chat, mid, task_id)
            failed = await self.upload(desk, task_id, [file]) if file else []
        except YouGileError as exc:
            log.warning("relaying to %s failed: %s", ticket.number, exc)
            await self.reply(chat, "Не получилось передать сообщение. Попробуйте позже.")
            return
        log.info("relayed a message of %s to %s", sender.tg_user_id, ticket.number)
        # A 👍 on the message says it reached the ticket, without another message.
        if not failed:
            try:
                await self.tg.call(
                    "setMessageReaction",
                    chat_id=chat,
                    message_id=msg["message_id"],
                    reaction=[{"type": "emoji", "emoji": "👍"}],
                )
                return
            except TelegramError as exc:
                log.info("setMessageReaction: %s", exc)
        note = " Файл приложить не удалось." if failed else ""
        try:  # the message is in the ticket already: the acknowledgement is a courtesy
            sent = await self.reply(chat, f"Передал в заявку <b>{esc(ticket.number)}</b>.{note}")
            await self.store.link_message(chat, sent["message_id"], task_id)
        except TelegramError as exc:
            log.info("cannot acknowledge a message to %s: %s", ticket.number, exc)

    async def list_tickets(self, chat: int, uid: int) -> None:
        tickets = await self.store.tickets_of(uid)
        if not tickets:
            await self.reply(chat, "Заявок пока нет.", markup=MENU)
            return
        lines = []
        for t in tickets:
            mark = "✅" if t.completed else "•"
            lines.append(f"{mark} <b>{esc(t.number)}</b> {esc(clip(t.title, 80))}")
        rows = [
            [(f"Ответить {t.number}", f"reply:{t.task_id}")]
            for t in tickets
            if not t.completed and not t.tg_thread_id  # a ticket with a topic is answered there
        ]
        await self.reply(
            chat, "Ваши заявки:\n" + "\n".join(lines), markup=inline(rows[:8]) if rows else MENU
        )

    async def say(
        self, chat: int, task_id: str, text: str, *, thread: int | None = None
    ) -> int | None:
        """A message about a ticket: into its topic, or into the main chat with a reply
        button. A topic Telegram refuses is dropped for good, and the message goes to the main
        chat. Returns the topic used, if any. Replies to the message reach the ticket."""
        sent = None
        if thread:
            try:
                sent = await self.tg.send(chat, text, thread=thread)
            except TelegramError as exc:
                if not (exc.code == 400 and "thread" in exc.description.lower()):
                    raise  # any other refusal (bad markup, say) is not the topic's fault
                log.info("the topic of %s is unusable (%s): main chat instead", task_id, exc)
                await self.store.update_ticket(task_id, tg_thread_id=None)
                thread = None
        if sent is None:
            button = inline([[("💬 Ответить", f"reply:{task_id}")]])
            sent = await self.tg.send(chat, text, markup=button)
        await self.store.link_message(chat, sent["message_id"], task_id)
        return thread

    # ================= YouGile side =================

    async def on_event(self, event: dict) -> int:
        """A YouGile webhook: refresh the tickets it may concern. Returns how many."""
        name = str(event.get("event") or "")
        known = await self.store.known(ids_in(event))
        # The payload format is not documented: its shape (never its values) helps to see why.
        log.info("YouGile event %s %s: %d ticket(s)", name, shape(event), len(known))
        for task_id in known:
            await self.coalesced(task_id, chat=not name.startswith("task-"))
        return len(known)

    async def coalesced(self, task_id: str, *, chat: bool) -> None:
        """Refresh a ticket for an event; a refresh already running takes this event along."""
        if task_id in self._busy:
            self._dirty[task_id] = self._dirty.get(task_id, False) or chat
            return
        self._busy.add(task_id)
        try:
            while True:
                try:
                    await self.refresh(task_id, chat=chat)
                except Exception:
                    log.exception("refreshing ticket %s failed", task_id)
                    self.pending.add(task_id)
                if task_id not in self._dirty:
                    break
                chat = self._dirty.pop(task_id)
        finally:
            self._busy.discard(task_id)

    async def retry_pending(self) -> int:
        """Report again the tickets whose report failed; returns how many still fail."""
        for task_id in list(self.pending):
            try:
                await self.refresh(task_id)
                self.pending.discard(task_id)
            except Exception as exc:  # still down: next minute
                log.info("ticket %s still not reported: %s", task_id, exc)
        return len(self.pending)

    async def refresh_open(self, fresh_within: float = 0.0, pause: float = 0.0) -> int:
        """Check every open ticket (webhooks can be lost); returns how many failed. Tickets a
        webhook refreshed within ``fresh_within`` seconds are skipped, and ``pause`` spreads
        the rest out, so the check never crowds the company's shared limit."""
        failed = 0
        now = time.monotonic()
        tickets = [
            t
            for t in await self.store.open_tickets()
            if now - self._fresh.get(t.task_id, -1e18) >= fresh_within
        ]
        self._tidy()
        for n, ticket in enumerate(tickets):
            if pause and n:
                await asyncio.sleep(pause)
            try:
                await self.refresh(ticket.task_id)
                self.pending.discard(ticket.task_id)
            except Exception:
                log.exception("refreshing ticket %s failed", ticket.number)
                self.pending.add(ticket.task_id)
                failed += 1
        return failed

    def _tidy(self) -> None:
        """Forget idle locks and old refresh times (they would pile up for every ticket)."""
        for key in [k for k, lock in self._locks.items() if not lock.locked()]:
            del self._locks[key]
        horizon = time.monotonic() - 86_400
        self._fresh = {k: v for k, v in self._fresh.items() if v > horizon}

    async def refresh(self, task_id: str, *, chat: bool = True) -> None:
        """Compare a ticket with its task and tell the sender what changed: the column (or
        completion, or removal) and new messages in the task's chat. A task that has left
        the customer's project is not reported at all."""
        await self._refresh(task_id, chat=chat)
        if chat:
            self._fresh[task_id] = time.monotonic()

    async def _refresh(self, task_id: str, *, chat: bool = True) -> None:
        async with self._locks[f"task:{task_id}"]:
            ticket = await self.store.ticket(task_id)
            if ticket is None or ticket.deleted:
                return
            account = await self.store.account(ticket.account_id)
            assert account is not None
            desk = self.desks.get(account)
            try:
                task = await desk.task(task_id)
                column_id = task.get("columnId")
                column = await desk.column(column_id) if column_id else None
            except YouGileError as exc:
                if exc.status not in (403, 404):
                    raise
                # Out of the bot account's sight (moved to a board it cannot read): that is
                # not removal, YouGile deletes softly. Say nothing — and skip what is said in
                # its chat meanwhile, or it would reach the sender once the task is back.
                log.info("ticket %s is not visible to the bot (%s)", ticket.number, exc.status)
                await self.skip_messages(ticket, desk)
                return
            sender = await self.store.sender(ticket.tg_user_id)
            quiet = sender is None or not sender.approved  # blocked, rejected: told nothing
            outside = column is None or column.project_id != ticket.project_id
            if task.get("deleted") and outside:  # removed in another project: not the sender's news
                await self.store.update_ticket(task_id, deleted=True)
                return
            if task.get("deleted"):
                if not quiet:  # told first, saved after: a failure is retried, not lost
                    await self.notify(
                        ticket, f"Заявка <b>{esc(ticket.number)}</b> «{esc(ticket.title)}» снята."
                    )
                    await self.rename_topic(ticket, topic_name(ticket.number, ticket.title, "✖ "))
                await self.store.update_ticket(task_id, deleted=True)
                return
            if column is None or column.project_id != ticket.project_id:
                # In another project (the internal one, say): nothing is reported, and what is
                # said in its chat there stays there — even after the task comes back.
                log.info("ticket %s is outside the customer's project: not reported", ticket.number)
                await self.skip_messages(ticket, desk)
                return
            if quiet:
                # Kept in step silently, so that unblocking later brings no stale news.
                await self.store.update_ticket(
                    task_id,
                    column_id=column.id,
                    completed=is_done(task, column.title),
                    title=task.get("title") or ticket.title,
                )
                await self.skip_messages(ticket, desk)
                return
            await self.report_status(ticket, task, column.id, column.title)
            if chat:
                await self.report_messages(ticket, account, desk)

    async def skip_messages(self, ticket: Ticket, desk: Desk) -> None:
        """Move the ticket's chat watermark past every message there is: they are never
        relayed. Message ids are YouGile's own clock, so they are read, not guessed; when the
        chat cannot be read, our clock is the fallback."""
        try:
            messages = await desk.messages_since(ticket.task_id, ticket.last_message_id)
            mark = max([ticket.last_message_id, *(int(m["id"]) for m in messages)])
        except YouGileError:
            mark = max(ticket.last_message_id, now_ms())
        if mark != ticket.last_message_id:
            await self.store.update_ticket(ticket.task_id, last_message_id=mark)

    async def report_status(self, ticket: Ticket, task: dict, column_id: str, title: str) -> None:
        done = is_done(task, title)
        name = task.get("title") or ticket.title
        if column_id == ticket.column_id and done == ticket.completed and name == ticket.title:
            return
        if column_id != ticket.column_id or done != ticket.completed:
            head = f"<b>{esc(ticket.number)}</b> «{esc(name)}»"
            text = f"✅ Заявка {head} выполнена." if done else f"🔄 Заявка {head}: {esc(title)}."
            # Told first, saved after: if Telegram fails, the change is reported next time.
            await self.notify(ticket, text)
        if done != ticket.completed or name != ticket.title:
            # A closed ticket's topic stays as its history, marked; writing there still works.
            await self.rename_topic(ticket, topic_name(ticket.number, name, "✅ " if done else ""))
        await self.store.update_ticket(
            ticket.task_id, column_id=column_id, completed=done, title=name
        )

    async def report_messages(self, ticket: Ticket, account: Account, desk: Desk) -> None:
        messages = await desk.messages_since(ticket.task_id, ticket.last_message_id)
        if not messages:
            return
        newest = ticket.last_message_id
        for m in messages:
            newest = max(newest, int(m["id"]))
            if m.get("deleted") or m.get("fromUserId") == account.bot_user_id:
                continue
            author = esc(await desk.user_name(m.get("fromUserId") or ""))
            url = file_url(m)
            if url:
                body = f'📎 <a href="{html.escape(desk.link(url), quote=True)}">файл</a>'
            else:
                body = esc(clip(message_text(m)))
            if not body:
                continue
            await self.notify(ticket, f"💬 <b>{esc(ticket.number)}</b> · <b>{author}</b>:\n{body}")
            # Saved per message: a failure further on must not repeat what was sent.
            await self.store.update_ticket(ticket.task_id, last_message_id=newest)
        await self.store.update_ticket(ticket.task_id, last_message_id=newest)

    async def notify(self, ticket: Ticket, text: str) -> None:
        try:
            await self.say(ticket.tg_user_id, ticket.task_id, text, thread=ticket.tg_thread_id)
        except TelegramError as exc:
            if not exc.permanent:
                raise  # Telegram unreachable: nothing is marked as told, it is tried again
            # Not told, and not tried again: worth a warning, the sender may be waiting.
            log.warning("cannot notify %s about %s: %s", ticket.tg_user_id, ticket.number, exc)
            return
        log.info("told %s about %s", ticket.tg_user_id, ticket.number)
