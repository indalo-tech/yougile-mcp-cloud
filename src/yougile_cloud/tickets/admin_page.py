"""The ticket bot's page in the admin area (`/admin/tickets`): everything the bot needs is filled
in here — the Telegram token, who approves senders, the bot's YouGile account, the webhook
subscriptions — next to a status of each. Only admins of TICKETS_COMPANY_ID see it."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from yougile_mcp.client import YouGileError
from yougile_mcp.directory import fetch_all

from .. import admin_pages as ui
from ..pages import e
from ..signin import TooManyAttempts
from ..yougile_auth import BadCredentials, KeyLimitReached
from .bot import NEW_STATUS, refused
from .cli import _subscribe
from .desk import Desks
from .service import WEBHOOK_EVENTS, hook_url
from .store import Account, Sender, TicketStore
from .telegram import Telegram, TelegramError

if TYPE_CHECKING:
    from ..admin import AdminPages, Ctx

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^\d{5,20}:[A-Za-z0-9_-]{30,60}$")
STATUS = {
    "pending": "ждёт решения",
    "approved": "одобрен",
    "rejected": "отклонён",
    "revoked": "доступ отозван",
    "blocked": "заблокирован",
}
WAITING = {
    "approve": "одобрение",
    "reject": "отказ",
    "move": "перевод",
    "revoke": "отзыв доступа",
    "block": "блокировку",
    "unblock": "разблокировку",
}
ACTION_WAIT = 6.0  # seconds the page waits for the bot to carry out a request
# What the page reads from YouGile (the bot account, its projects, the subscriptions) is kept
# this long: managing people re-renders the page on every click, and YouGile's limit per
# company (shared with MCP users and the bot) would run out after a handful of them.
YOUGILE_TTL = 60.0
YOUGILE_FIELDS = ("bot_name", "bot_is_admin", "visible", "requests", "choices", "hooks")


@dataclass
class Status:
    token_set: bool = False
    bot: str | None = None  # @username, from Telegram
    topics: bool | None = None
    telegram_error: str | None = None
    admins: list[int] = field(default_factory=list)
    account: Account | None = None
    bot_name: str | None = None
    bot_is_admin: bool = False
    visible: list[str] = field(default_factory=list)  # projects the bot account sees
    requests: list[str] = field(default_factory=list)  # ... of them with a «Заявки» board
    account_error: str | None = None
    hooks: list[dict] = field(default_factory=list)  # subscriptions to this server's URL
    hooks_error: str | None = None
    senders: list[Sender] = field(default_factory=list)
    choices: list[tuple[str, str]] = field(default_factory=list)  # ("<account>:<project>", title)
    waiting: dict[int, str] = field(default_factory=dict)  # person: a request the bot has yet to do

    @property
    def missing_hooks(self) -> list[str]:
        alive = {h.get("event") for h in self.hooks if not h.get("disabled")}
        return [ev for ev in WEBHOOK_EVENTS if ev not in alive]


def parse_admins(text: str) -> list[int] | None:
    """Telegram ids separated by commas, spaces or lines; None if something else is there."""
    parts = [p for p in re.split(r"[\s,;]+", text.strip()) if p]
    if not all(p.isdigit() and len(p) <= 20 for p in parts):
        return None
    return list(dict.fromkeys(int(p) for p in parts))


class TicketAdmin:
    def __init__(self, pages: AdminPages) -> None:
        self.pages = pages
        self.tg_transport: Any = None  # tests: a fake Telegram
        self._yougile: dict[str, tuple[float, tuple, dict[str, Any]]] = {}  # company: seen

    def _telegram(self, token: str) -> Telegram:
        return Telegram(token, transport=self.tg_transport)

    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("/admin/tickets", "GET", self.show),
            ("/admin/tickets/telegram", "POST", self.save_telegram),
            ("/admin/tickets/account", "POST", self.save_account),
            ("/admin/tickets/webhooks", "POST", self.subscribe),
            ("/admin/tickets/sender", "POST", self.manage_sender),
        ]

    # ---------- plumbing ----------

    @property
    def store(self) -> TicketStore:
        return TicketStore(self.pages.db)

    def _owner(self, ctx: Ctx) -> bool:
        return ctx.session.company.id == self.pages.settings.tickets_company_id

    async def _page(
        self, ctx: Ctx, *, errors: list[str] | None = None, done: str | None = None
    ) -> Response:
        if not self._owner(ctx):
            return Response(status_code=404)
        status = await self.status(ctx)
        return self.pages._html(tickets_page(ctx.frame, status, errors=errors, done=done))

    def _desks(self) -> Desks:
        return Desks(
            self.pages.secrets,
            self.pages.settings.yougile_base_url,
            kv=self.pages.kv,
            rate_limit=self.pages.settings.rate_limit,
            transport=self.pages.transport,
        )

    async def status(self, ctx: Ctx) -> Status:
        store = self.store
        st = Status()
        config = await store.bot_config()
        if config:
            st.admins = sorted(config.admins)
            st.token_set = config.token_enc is not None
        if config and config.token_enc:
            try:
                tg = self._telegram(self.pages.secrets.decrypt(config.token_enc))
                try:
                    me = await tg.call("getMe")
                finally:
                    await tg.aclose()
                st.bot = me.get("username")
                st.topics = bool(me.get("has_topics_enabled"))
            except (TelegramError, ValueError) as exc:
                st.telegram_error = str(exc)

        st.account = await store.account_of_company(ctx.session.company.id)
        await self._from_yougile(ctx, st)
        st.senders = [
            s
            for s in await store.senders()
            if s.account_id in (None, st.account.id if st.account else None)
        ]
        st.waiting = await store.waiting_actions()
        return st

    async def _from_yougile(self, ctx: Ctx, st: Status) -> None:
        """The bot account, the projects taking tickets and the webhook subscriptions: from
        YouGile, or from what was read in the last YOUGILE_TTL seconds (errors are not kept)."""
        company = ctx.session.company.id
        key = (st.account.id, st.account.api_key_enc) if st.account else ()
        seen = self._yougile.get(company)
        if seen and seen[1] == key and time.monotonic() - seen[0] < YOUGILE_TTL:
            for name, value in seen[2].items():
                setattr(st, name, value)
            return
        if st.account:
            desks = self._desks()
            try:
                desk = desks.get(st.account)
                me = await desk.client.request("GET", "/users/me")
                st.bot_name = me.get("realName") or me.get("email")
                st.bot_is_admin = bool(me.get("isAdmin"))
                st.visible = sorted(
                    p.get("title") or p["id"]
                    for p in await fetch_all(desk.client, "/projects")
                    if not p.get("deleted")
                )
                requests = (await desk.projects(fresh=True)).values()
                st.requests = sorted(p.title for p in requests)
                st.choices = sorted(
                    ((f"{st.account.id}:{p.id}", p.title) for p in requests),
                    key=lambda c: c[1].lower(),
                )
            except (YouGileError, ValueError) as exc:
                st.account_error = str(exc)
            finally:
                await desks.close()

        url = hook_url(self.pages.settings)
        try:
            listed = await ctx.client.request("GET", "/webhooks")
            hooks = listed.get("content", []) if isinstance(listed, dict) else listed or []
            st.hooks = [h for h in hooks if h.get("url") == url and not h.get("deleted")]
        except YouGileError as exc:
            st.hooks_error = str(exc)
        if st.account_error or st.hooks_error:
            self._yougile.pop(company, None)
        else:
            fields = {name: getattr(st, name) for name in YOUGILE_FIELDS}
            self._yougile[company] = (time.monotonic(), key, fields)

    # ---------- handlers ----------

    async def show(self, request: Request) -> Response:
        async def handler(request: Request, ctx: Ctx) -> Response:
            done = request.query_params.get("done")
            return await self._page(ctx, done=done)

        return await self.pages._guarded(request, handler)

    async def save_telegram(self, request: Request) -> Response:
        async def handler(_request: Request, ctx: Ctx) -> Response:
            if not self._owner(ctx):
                return Response(status_code=404)
            form = ctx.form
            assert form is not None
            token = str(form.get("token", "")).strip()
            admins = parse_admins(str(form.get("admins", "")))
            errors: list[str] = []
            if admins is None:
                errors.append("Telegram id — только цифры, через запятую или с новой строки.")
            username = None
            if token:
                if not TOKEN_RE.match(token):
                    errors.append("Это не похоже на токен бота: он выглядит как 123456:ABC-…")
                else:
                    tg = self._telegram(token)
                    try:
                        username = (await tg.call("getMe")).get("username") or ""
                    except TelegramError as exc:
                        errors.append(f"Telegram не принял токен: {exc.description}")
                    finally:
                        await tg.aclose()
            if errors:
                return await self._page(ctx, errors=errors)
            await self.store.save_bot_config(
                admins=admins or [],
                token_enc=self.pages.secrets.encrypt(token) if token else None,
                bot_username=username,
            )
            await self.pages.db.audit(
                "tickets_telegram",
                company_id=ctx.session.company.id,
                user_id=ctx.session.user.id,
                token_changed=bool(token),
                admins=len(admins or []),
            )
            return ui_redirect("tickets_telegram")

        return await self.pages._guarded(request, handler, post=True)

    async def save_account(self, request: Request) -> Response:
        async def handler(request: Request, ctx: Ctx) -> Response:
            if not self._owner(ctx):
                return Response(status_code=404)
            form = ctx.form
            assert form is not None
            login = str(form.get("login", "")).strip()
            password = str(form.get("password", ""))
            if not login or not password:
                return await self._page(ctx, errors=["Введите логин и пароль учётки бота."])
            company = ctx.session.company
            auth = self.pages.auth
            try:
                ip = request.client.host if request.client else "?"
                await self.pages.signin.count_attempt(ip, login)
                companies = await self.pages.signin.companies(login, password, ip)
                if not any(c.id == company.id for c in companies):
                    return await self._page(
                        ctx, errors=[f"Эта учётка не состоит в компании «{company.name}»."]
                    )
                key = await auth.create_key(login, password, company.id)
                me = await auth.me(key)
            except TooManyAttempts:
                return await self._page(ctx, errors=["Слишком много попыток. Подождите 15 минут."])
            except BadCredentials:
                return await self._page(ctx, errors=["Неверный логин или пароль YouGile."])
            except KeyLimitReached:
                return await self._page(
                    ctx, errors=["У учётки уже 30 ключей API: удалите лишние в YouGile."]
                )
            except YouGileError as exc:
                log.warning("bot account: YouGile failed: %s", exc)
                return await self._page(
                    ctx, errors=["YouGile не ответил. Попробуйте через минуту."]
                )
            if me.id == ctx.session.user.yougile_user_id:
                await auth.delete_key(key)
                return await self._page(
                    ctx,
                    errors=[
                        "Это ваша собственная учётка. Бот должен писать от отдельной: "
                        "заведите в YouGile сотрудника «Заявки (бот)» и войдите им."
                    ],
                )
            store = self.store
            previous = await store.account_of_company(company.id)
            await store.add_account(
                name=company.name,
                company_id=company.id,
                bot_user_id=me.id,
                api_key_enc=self.pages.secrets.encrypt(key),
            )
            if previous:  # one key per account for the bot: don't let them pile up
                try:
                    old = self.pages.secrets.decrypt(previous.api_key_enc)
                except ValueError:
                    old = None
                if old and old != key:
                    await auth.delete_key(old)
            self._yougile.pop(company.id, None)
            await self.pages.db.audit(
                "tickets_account",
                company_id=company.id,
                user_id=ctx.session.user.id,
                bot_user=me.id,
            )
            return ui_redirect("tickets_account")

        return await self.pages._guarded(request, handler, post=True)

    async def subscribe(self, request: Request) -> Response:
        async def handler(_request: Request, ctx: Ctx) -> Response:
            if not self._owner(ctx):
                return Response(status_code=404)
            self._yougile.pop(ctx.session.company.id, None)  # what is subscribed now, shown
            try:
                await _subscribe(ctx.client, hook_url(self.pages.settings))
            except YouGileError as exc:
                return await self._page(ctx, errors=[f"YouGile не создал подписки: {exc}"])
            await self.pages.db.audit(
                "tickets_webhooks", company_id=ctx.session.company.id, user_id=ctx.session.user.id
            )
            return ui_redirect("tickets_hooks")

        return await self.pages._guarded(request, handler, post=True)

    async def manage_sender(self, request: Request) -> Response:
        """Approve, reject, move, revoke, block or unblock a person: asked of the bot's process
        (it tells the person and makes tickets of what they wrote while waiting); the page
        waits a few seconds for the result."""

        async def handler(_request: Request, ctx: Ctx) -> Response:
            if not self._owner(ctx):
                return Response(status_code=404)
            form = ctx.form
            assert form is not None
            store = self.store
            raw = str(form.get("tg_user_id", ""))
            action = str(form.get("action", ""))
            account = await store.account_of_company(ctx.session.company.id)
            sender = await store.sender(int(raw)) if raw.isdigit() and len(raw) <= 20 else None
            if sender is None or sender.account_id not in (None, account.id if account else None):
                return await self._page(ctx, errors=["Сотрудник не найден."])
            if action not in NEW_STATUS:
                return await self._page(ctx, errors=["Неизвестное действие."])
            if why := refused(action, sender):
                return await self._page(ctx, errors=[f"{sender.name}: {why}."])
            project_id = None
            if action in ("approve", "move"):
                acc, _, project_id = str(form.get("project", "")).partition(":")
                if account is None or acc != str(account.id) or not project_id:
                    return await self._page(ctx, errors=["Выберите проект с доской «Заявки»."])
            action_id = await store.queue_action(
                sender.tg_user_id,
                action,
                account_id=account.id if project_id and account else None,
                project_id=project_id,
                user_id=ctx.session.user.id,
            )
            result = await self._wait(action_id)
            if result is None:
                return ui_redirect("tickets_queued")
            ok, line = result
            if not ok:
                return await self._page(ctx, errors=[f"{sender.name}: {line}."])
            return ui_redirect("tickets_sender")

        return await self.pages._guarded(request, handler, post=True)

    async def _wait(self, action_id: int) -> tuple[bool, str] | None:
        loop = asyncio.get_running_loop()
        until = loop.time() + ACTION_WAIT
        while True:
            result = await self.store.action_result(action_id)
            if result is not None or loop.time() >= until:
                return result
            await asyncio.sleep(0.3)


def ui_redirect(done: str) -> Response:
    return RedirectResponse(f"/admin/tickets?done={done}", status_code=303)


# ---------- HTML ----------


def _mark(ok: bool) -> str:
    return "✅" if ok else "⬜"


def _when(seconds: Any, frame: ui.Frame) -> str:
    try:
        value = float(seconds)
    except TypeError, ValueError:
        return "—"
    if value > 10**11:  # milliseconds
        value /= 1000
    moment = datetime.fromtimestamp(value, UTC).astimezone(frame.tz)
    return f"{moment:%d.%m.%Y %H:%M}"


def _sender_actions(s: Sender, st: Status, csrf: str) -> str:
    """The buttons of a person's row: what can be done about them now."""
    if s.tg_user_id in st.waiting:
        return f'<span class="muted">ждёт бота: {e(WAITING.get(st.waiting[s.tg_user_id]))}</span>'

    def form(action: str, label: str, style: str = "secondary", select: str = "") -> str:
        return (
            f'<form method="post" action="/admin/tickets/sender">{csrf}'
            f'<input type="hidden" name="tg_user_id" value="{s.tg_user_id}">'
            f'<input type="hidden" name="action" value="{action}">{select}'
            f'<button class="inline {style}" type="submit">{label}</button></form>'
        )

    def projects(skip: str = "") -> str:
        options = "".join(
            f'<option value="{e(ref)}">{e(title)}</option>'
            for ref, title in st.choices
            if ref != skip
        )
        return f'<select name="project" aria-label="Проект">{options}</select>' if options else ""

    parts: list[str] = []
    if s.approved:
        if other := projects(f"{s.account_id}:{s.project_id}"):
            parts.append(form("move", "Перевести", select=other))
        parts.append(form("revoke", "Отозвать доступ", "danger"))
    elif s.status != "blocked":
        if choices := projects():
            parts.append(form("approve", "Одобрить", "", choices))
        if s.status == "pending":
            parts.append(form("reject", "Отклонить"))
    if s.status == "blocked":
        parts.append(form("unblock", "Разблокировать"))
    else:
        parts.append(form("block", "Заблокировать", "danger"))
    return f'<div class="actions">{"".join(parts)}</div>'


def tickets_page(
    frame: ui.Frame, st: Status, *, errors: list[str] | None = None, done: str | None = None
) -> str:
    csrf = ui._csrf(frame.csrf)
    steps = [
        (bool(st.bot), "Токен бота от @BotFather"),
        (bool(st.admins), "Кто одобряет сотрудников"),
        (bool(st.topics), "Темы для заявок (Threaded Mode в @BotFather)"),
        (bool(st.bot_name), "Учётка бота в YouGile"),
        (bool(st.requests), "Проект с доской «Заявки»"),
        (bool(st.hooks) and not st.missing_hooks, "Подписки на события YouGile"),
    ]
    checklist = "".join(f"<li>{_mark(ok)} {e(text)}</li>" for ok, text in steps)

    # Telegram
    if st.bot:
        tg_state = f"Бот: <b>@{e(st.bot)}</b>"
        tg_state += (
            " · темы включены"
            if st.topics
            else " · темы выключены: включите Threaded Mode в @BotFather → Bot Settings"
        )
    elif st.telegram_error:
        tg_state = f'<span class="error">Telegram: {e(st.telegram_error)}</span>'
    else:
        tg_state = "Токен ещё не задан."
    admins = ", ".join(str(a) for a in st.admins)
    token_hint = "задан — оставьте пустым, чтобы не менять" if st.token_set else "123456:ABC-…"
    telegram = (
        "<h2>Telegram</h2>"
        f"<p>{tg_state}</p>"
        f'<form method="post" action="/admin/tickets/telegram">{csrf}'
        '<label for="token">Токен бота</label>'
        '<input type="password" id="token" name="token" autocomplete="off" '
        f'placeholder="{e(token_hint)}">'
        '<p class="hint">@BotFather → /mybots → бот → API Token. Хранится зашифрованным, '
        "на странице не показывается. Бот подхватит новый токен в течение 20 секунд.</p>"
        '<label for="admins">Кто одобряет сотрудников — Telegram id</label>'
        f'<input type="text" id="admins" name="admins" value="{e(admins)}" '
        'placeholder="123456789, 987654321" autocomplete="off">'
        '<p class="hint">Свой id пришлёт бот в ответ на команду /id. Этим людям приходят '
        "запросы доступа с кнопками проектов; у них работает /senders — сотрудники с теми же "
        "действиями, что внизу этой страницы.</p>"
        '<button class="inline" type="submit">Сохранить</button></form>'
    )

    # YouGile account
    if st.account_error:
        acc_state = f'<div class="error">Учётка бота: {e(st.account_error)}</div>'
    elif st.bot_name:
        no_requests = [p for p in st.visible if p not in st.requests]
        acc_state = f"<p>Бот пишет в YouGile как <b>{e(st.bot_name)}</b>.</p>"
        if st.bot_is_admin:
            acc_state += (
                '<p class="hint">У этой учётки права администратора компании. Боту они не '
                "нужны: достаточно доступа к проектам заказчиков.</p>"
            )
        acc_state += (
            f"<p>Заявки принимают проекты: <b>{e(', '.join(st.requests) or 'пока ни один')}</b>."
            "</p>"
        )
        if no_requests:
            acc_state += (
                f'<p class="hint">Учётка видит и проекты без доски «Заявки»: '
                f"{e(', '.join(no_requests))}. Заявки туда не попадут и бот о них не сообщает, "
                "но если там нет ничего для заказчиков — уберите учётке доступ к ним.</p>"
            )
    else:
        acc_state = (
            "<p>Учётка ещё не подключена. Заведите в YouGile отдельного сотрудника, например "
            "«Заявки (бот)», дайте ему доступ к проектам заказчиков (не к внутренним) и "
            "войдите им здесь.</p>"
        )
    account = (
        "<h2>Учётка бота в YouGile</h2>"
        + acc_state
        + f'<form method="post" action="/admin/tickets/account">{csrf}'
        '<label for="login">Логин (почта) учётки бота</label>'
        '<input type="email" id="login" name="login" autocomplete="off">'
        '<label for="password">Пароль</label>'
        '<input type="password" id="password" name="password" autocomplete="new-password">'
        '<p class="hint">Пароль не сохраняется: по нему выпускается ключ API учётки, ключ '
        "хранится зашифрованным. Проект для заявок — любой, где есть доска «Заявки».</p>"
        f'<button class="inline" type="submit">{"Заменить" if st.account else "Подключить"}'
        "</button></form>"
    )

    # Webhooks
    if st.hooks_error:
        hooks_state = f'<div class="error">{e(st.hooks_error)}</div>'
    elif st.hooks:
        rows = "".join(
            f'<tr><td data-label="Событие"><code>{e(h.get("event"))}</code></td>'
            f'<td data-label="Состояние">{"выключена" if h.get("disabled") else "работает"}</td>'
            f'<td data-label="Последний вызов">{e(_when(h.get("lastSuccess"), frame))}</td>'
            f'<td data-label="Ошибок подряд">{e(h.get("failuresSinceLastSuccess") or 0)}</td></tr>'
            for h in st.hooks
        )
        hooks_state = (
            "<table><thead><tr><th>Событие</th><th>Состояние</th><th>Последний успешный вызов"
            f"</th><th>Ошибок подряд</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        hooks_state = "<p>Подписок ещё нет.</p>"
    hooks = (
        "<h2>Подписки на события YouGile</h2>"
        + hooks_state
        + '<p class="hint">По ним бот сразу узнаёт о переносах заявок и сообщениях в их чатах. '
        "Без них — раз в 30 минут. Подписки создаются вашим ключом администратора.</p>"
        f'<form method="post" action="/admin/tickets/webhooks">{csrf}'
        f'<button class="inline{" secondary" if not st.missing_hooks else ""}" type="submit">'
        f"{'Подписать' if st.missing_hooks else 'Проверить подписки'}</button></form>"
    )

    # Senders
    if st.senders:
        rows = "".join(
            f'<tr><td data-label="Сотрудник">{e(s.name)}'
            f"{f' <span class=muted>@{e(s.username)}</span>' if s.username else ''}"
            f'<br><span class="muted">id {s.tg_user_id}</span></td>'
            f'<td data-label="Статус">{e(STATUS.get(s.status, s.status))}</td>'
            f'<td data-label="Проект">{e(s.project_name if s.approved else "—")}</td>'
            f'<td data-label="Действия">{_sender_actions(s, st, csrf)}</td></tr>'
            for s in st.senders
        )
        senders = (
            "<h2>Сотрудники в боте</h2><table><thead><tr><th>Сотрудник</th><th>Статус</th>"
            f"<th>Проект</th><th>Действия</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        senders = "<h2>Сотрудники в боте</h2><p>Пока никто не просил доступ.</p>"
    senders += (
        '<p class="hint">Запросы доступа приходят одобряющим в бот; решать их и управлять '
        "сотрудниками можно и здесь, и в боте командой /senders. Об одобрении, отказе, переводе "
        "и отзыве доступа бот пишет сотруднику сам; заблокированному бот просто перестаёт "
        "отвечать. После отзыва или разблокировки человек может запросить доступ заново.</p>"
    )

    body = (
        ui._errors(errors or [])
        + f'<p class="lead">Что ещё нужно сделать:</p><ul class="checks-list">{checklist}</ul>'
        + telegram
        + account
        + hooks
        + senders
    )
    return ui._shell(frame, "tickets", "Бот заявок", body, done)
