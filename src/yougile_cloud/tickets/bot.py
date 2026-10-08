"""The ticket bot's conversations (Telegram side) and its reports on tickets (YouGile side)."""

from __future__ import annotations

import asyncio
import html
import logging
import time
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
    {"command": "senders", "description": "Сотрудники и их проекты"},
    {"command": "id", "description": "Мой Telegram id"},
]
NO_MENU = {"remove_keyboard": True}
DONE_COLUMNS = {"готово"}
MAX_TITLE = 200
MAX_FILES = 10
MAX_TEXT = 3500  # Telegram allows 4096 characters per message
MAX_TOPIC = 128  # characters in a topic's name
TOPICS_RECHECK = 600.0  # seconds between checks that the bot's topics are on (@BotFather)

# The topic the message being handled came from: answers go back there.
_thread: ContextVar[int | None] = ContextVar("ticket_bot_thread", default=None)

ASK_NAME = (
    "Здравствуйте! Это бот для заявок команде Indalo.\n\n"
    "Как вас зовут и из какой вы компании? Ответьте одним сообщением — "
    "после подтверждения доступа сможете отправлять заявки."
)
ASK_TITLE = "Коротко: что нужно сделать или что не работает? Одной строкой — это будет заголовок."
ASK_DETAILS = (
    "Опишите подробнее: где, что происходит, что ожидали. Можно приложить фото и файлы.\n"
    "Когда всё готово — нажмите «Отправить»."
)
HELP = (
    f"Чтобы отправить заявку — «{NEW}».\n"
    "Чтобы написать по заявке — ответьте на сообщение бота о ней или нажмите «Ответить» под ним."
)
THREAD_HELP = (
    "Чтобы отправить заявку, начните новую тему (значок ✏️ или «Новая тема») и опишите, что "
    "случилось: текстом, фото, голосовым, можно в несколько сообщений. Перед отправкой я покажу "
    "черновик.\n\nВсё по заявке — в её теме: статус, вопросы команды. Пишите туда, сообщения "
    "уйдут в заявку."
)
TOPIC_COMMANDS = [{"command": "my", "description": "Мои заявки"}]
TOPIC_ADMIN_COMMANDS = [
    *TOPIC_COMMANDS,
    {"command": "senders", "description": "Сотрудники и их проекты"},
    {"command": "id", "description": "Мой Telegram id"},
]
NO_PROJECT = "Проект не найден или в нём больше нет доски «Заявки»"
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
        f"kinds={kinds} service={service}"
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

    # ================= Telegram side =================

    async def handle(self, update: dict) -> None:
        msg = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        token = _thread.set(thread_of(msg))
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

    async def setup(self) -> None:
        """The command menu next to the input field (approvers get theirs as well)."""
        topics = await self.topics_enabled()
        try:
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
        if thread and markup is MENU:
            markup = NO_MENU
        try:
            return await self.tg.send(chat, text, markup=markup, thread=thread)
        except TelegramError as exc:
            if not thread or exc.code != 400:
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
                log.info("getMe: %s", exc)
                self._topics = (now, False)
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
        await self.compose(chat, uid, thread, msg, text)

    async def compose(self, chat: int, uid: int, thread: int, msg: dict, text: str) -> None:
        content = text or (msg.get("caption") or "").strip()
        file = attachment(msg)
        if content.startswith("/"):
            content = ""
        if not content and not file:
            await self.reply(chat, "Пришлите текст, фото, голосовое или файл.")
            return
        if file and file["size"] > MAX_DOWNLOAD:
            await self.reply(chat, "Файл больше 20 МБ: бот такие не принимает. Пришлите ссылку.")
            return
        async with self._locks[f"draft:{uid}:{thread}"]:
            draft = await self.store.draft(uid, thread) or {
                "step": "compose",
                "title": "",
                "parts": [],
                "files": [],
            }
            if content:
                draft["title"] = draft["title"] or clip(first_line(content), MAX_TITLE)
                draft["parts"].append(content)
            if file:
                if len(draft["files"]) >= MAX_FILES:
                    await self.reply(chat, f"Не больше {MAX_FILES} файлов на заявку.")
                    return
                draft["files"].append(file)
            # The card with the buttons follows the latest message, so it stays at hand.
            old = draft.get("card")
            card = await self.reply(chat, draft_card(draft), markup=topic_buttons(thread))
            draft["card"] = card["message_id"]
            await self.store.save_draft(uid, draft, thread)
        if old:
            try:
                await self.tg.call("deleteMessage", chat_id=chat, message_id=old)
            except TelegramError as exc:
                log.info("deleteMessage: %s", exc)

    async def cancel_topic(self, chat: int, uid: int, thread: int) -> None:
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
            task, number, failed = made
            await self.store.update_ticket(task["id"], tg_thread_id=thread)
            await self.store.save_draft(uid, {}, thread)
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
            await self.say(chat, task["id"], text, thread=thread)

    async def create(
        self, chat: int, sender: Sender, title: str, draft: dict
    ) -> tuple[dict, str, list[str]] | None:
        """The ticket's task, with its files; None (and the sender told) when it failed."""
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
            task = await desk.create_task(project.column_id, title, description)
        except YouGileError as exc:
            log.warning("cannot create a ticket in %s: %s", project.title, exc)
            await self.reply(
                chat, "Не получилось создать заявку. Попробуйте «Отправить» ещё раз чуть позже."
            )
            return None
        failed = await self.upload(desk, task["id"], draft.get("files") or [])
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
        return task, number, failed

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
        if sender and sender.status == "pending":
            await self.reply(
                chat, "Запрос на доступ ещё рассматривается. Напишу, как только решат."
            )
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
        await self.store.save_draft(uid, {})
        await self.reply(chat, "Спасибо! Запрос на доступ отправлен. Напишу, когда его одобрят.")
        await self.ask_approvers(sender, user)

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
            "Перевести потом в другой проект — /senders."
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
            if data.startswith(("ok:", "no:")):
                answer = await self.decide(uid, data, msg)
            elif data.startswith("mvl:"):
                answer = await self.offer_move(uid, chat, int(data.removeprefix("mvl:")))
            elif data.startswith("mv:"):
                answer = await self.move(uid, data, msg)
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
        if uid not in self.admins:
            return "Только для администраторов"
        parts = data.split(":")
        target = int(parts[1])
        if parts[0] == "ok":
            account, project = await self.chosen(parts[2:])
            if account is None or project is None:
                return NO_PROJECT
            sender = await self.store.decide(
                target,
                "approved",
                by=uid,
                account_id=account.id,
                project_id=project.id,
                project_name=project.title,
            )
            verdict = f"✅ Одобрено, проект: {project.title}"
        else:
            project = None
            sender = await self.store.decide(target, "rejected", by=uid)
            verdict = "❌ Отклонено"
        if sender is None:
            return "Запрос не найден"
        await self.mark(msg, verdict)
        if project:
            help_text = await self.help_text()
            await self.tell(
                target, f"Доступ открыт. Теперь можно отправлять заявки.\n\n{help_text}"
            )
        else:
            await self.tell(target, "К сожалению, в доступе отказано.")
        return "Готово"

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
        senders = [s for s in await self.store.senders() if s.approved]
        if not senders:
            await self.reply(chat, "Одобренных сотрудников пока нет.")
            return
        rows = [
            [(f"🔀 {clip(s.name, 40)} — {s.project_name}", f"mvl:{s.tg_user_id}")]
            for s in senders[:90]  # Telegram allows 100 buttons per message
        ]
        await self.reply(
            chat, "Сотрудники и их проекты. Нажмите, чтобы перевести в другой:", markup=inline(rows)
        )

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

    async def move(self, uid: int, data: str, msg: dict) -> str:
        if uid not in self.admins:
            return "Только для администраторов"
        parts = data.split(":")
        target = int(parts[1])
        current = await self.store.sender(target)
        if current is None or not current.approved:
            return "Сотрудник не найден"
        account, project = await self.chosen(parts[2:])
        if account is None or project is None:
            return NO_PROJECT
        await self.store.decide(
            target,
            "approved",
            by=uid,
            account_id=account.id,
            project_id=project.id,
            project_name=project.title,
        )
        await self.store.drop_drafts(target)  # half-written tickets were for the old project
        await self.mark(msg, f"✅ {current.name}: {current.project_name} → {project.title}")
        await self.tell(
            target,
            f"Теперь ваши заявки идут в проект «{esc(project.title)}». "
            "Уже отправленные остаются там, где были.",
        )
        return "Переведён"

    async def take_title(self, chat: int, uid: int, draft: dict, msg: dict, text: str) -> None:
        if not text or text.startswith("/"):
            await self.reply(chat, "Сначала напишите заголовок текстом — одной строкой.")
            return
        title = clip(" ".join(text.split()), MAX_TITLE)
        await self.store.save_draft(
            uid, {"step": "details", "title": title, "parts": [], "files": []}
        )
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
            await self.store.save_draft(uid, {})
            title = draft["title"]
            made = await self.create(chat, sender, title, draft)
            if made is None:
                await self.store.save_draft(uid, draft)  # to try «Отправить» again
                return
            task, number, failed = made
            note = f"\n\nНе удалось приложить: {esc(', '.join(failed))}." if failed else ""
            thread = await self.open_topic(chat, topic_name(number, title))
            if thread:
                await self.store.update_ticket(task["id"], tg_thread_id=thread)
                thread = await self.say(
                    chat,
                    task["id"],
                    f"✅ Заявка <b>{esc(number)}</b> принята: «{esc(title)}».\n"
                    f"Здесь — всё по ней: статус, вопросы команды. Пишите сюда, "
                    f"сообщения уйдут в заявку.{note}",
                    thread=thread,
                )
            if thread:
                await self.reply(
                    chat,
                    f"✅ Заявка <b>{esc(number)}</b> принята. Переписка по ней — в отдельной "
                    f"теме «{esc(topic_name(number, title))}».",
                    markup=MENU,
                )
                return
            await self.say(
                chat,
                task["id"],
                f"✅ Заявка <b>{esc(number)}</b> принята: «{esc(title)}».\n"
                f"Напишу, когда она сдвинется. Ответить по ней — кнопкой ниже "
                f"или ответом на это сообщение.{note}",
            )
            await self.reply(chat, "Что-то ещё?", markup=MENU)

    async def open_topic(self, chat: int, name: str) -> int | None:
        """A topic for a ticket in the sender's chat, or None (topics off or refused)."""
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
        header = f"{sender.name} ({sender.project_name}) пишет из Telegram:"
        try:
            await desk.post(task_id, f"{header}\n{text}" if text else f"{header} файл")
            failed = await self.upload(desk, task_id, [file]) if file else []
        except YouGileError as exc:
            log.warning("relaying to %s failed: %s", ticket.number, exc)
            await self.reply(chat, "Не получилось передать сообщение. Попробуйте позже.")
            return
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
        await self.say(
            chat,
            task_id,
            f"Передал в заявку <b>{esc(ticket.number)}</b>.{note}",
            thread=_thread.get(),
        )

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
                if exc.code != 400:
                    raise
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
            try:
                await self.refresh(task_id, chat=not name.startswith("task-"))
            except Exception:
                log.exception("refreshing ticket %s failed", task_id)
        return len(known)

    async def refresh_open(self) -> int:
        """Check every open ticket (webhooks can be lost); returns how many failed."""
        failed = 0
        for ticket in await self.store.open_tickets():
            try:
                await self.refresh(ticket.task_id)
            except Exception:
                log.exception("refreshing ticket %s failed", ticket.number)
                failed += 1
        return failed

    async def refresh(self, task_id: str, *, chat: bool = True) -> None:
        """Compare a ticket with its task and tell the sender what changed: the column (or
        completion, or removal) and new messages in the task's chat. A task that has left
        the customer's project is not reported at all."""
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
                # not removal, YouGile deletes softly. Say nothing, change nothing.
                log.info("ticket %s is not visible to the bot (%s)", ticket.number, exc.status)
                return
            if task.get("deleted"):
                await self.store.update_ticket(task_id, deleted=True)
                await self.notify(
                    ticket, f"Заявка <b>{esc(ticket.number)}</b> «{esc(ticket.title)}» снята."
                )
                await self.rename_topic(ticket, topic_name(ticket.number, ticket.title, "✖ "))
                return
            if column is None or column.project_id != ticket.project_id:
                log.info("ticket %s is outside the customer's project: not reported", ticket.number)
                return
            await self.report_status(ticket, task, column.id, column.title)
            if chat:
                await self.report_messages(ticket, account, desk)

    async def report_status(self, ticket: Ticket, task: dict, column_id: str, title: str) -> None:
        done = is_done(task, title)
        name = task.get("title") or ticket.title
        if column_id == ticket.column_id and done == ticket.completed and name == ticket.title:
            return
        await self.store.update_ticket(
            ticket.task_id, column_id=column_id, completed=done, title=name
        )
        if done != ticket.completed or name != ticket.title:
            # A closed ticket's topic stays as its history, marked; writing there still works.
            await self.rename_topic(ticket, topic_name(ticket.number, name, "✅ " if done else ""))
        if column_id == ticket.column_id and done == ticket.completed:
            return  # only the title changed
        head = f"<b>{esc(ticket.number)}</b> «{esc(name)}»"
        text = f"✅ Заявка {head} выполнена." if done else f"🔄 Заявка {head}: {esc(title)}."
        await self.notify(ticket, text)

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
                body = f'📎 <a href="{esc(desk.link(url))}">файл</a>'
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
        except TelegramError as exc:  # the sender blocked the bot, say
            log.info("cannot notify about %s: %s", ticket.number, exc)
