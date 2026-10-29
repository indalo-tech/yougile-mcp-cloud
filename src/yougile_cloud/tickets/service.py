"""The ticket bot's process: Telegram long polling, the YouGile webhook endpoint, a periodic
check of open tickets. Without a token (admin page or TICKETS_BOT_TOKEN) it stays idle and
healthy, so the container can be deployed before the bot is configured."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ..crypto import Secrets
from ..db import Database
from ..kv import KV
from ..settings import Settings, SettingsError
from .bot import TicketBot
from .desk import Desks
from .store import TicketStore
from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

HOOK_PREFIX = "/tickets/hook/"
RECONCILE_PAUSE = 2.0  # seconds between tickets in the periodic check
CONCURRENT_UPDATES = 8  # Telegram updates handled at once (one person's stay in order)
FIRST_CHECK = 60.0  # seconds after start: what a restart missed goes out within minutes
DRAIN_SECONDS = 8.0  # on stop, updates being handled get this long (Docker waits 10 s)
ACTIONS_SECONDS = 2.0  # how often the admin page's requests are looked for
WEBHOOK_EVENTS = ("task-.*", "chat_message-created")


@dataclass(frozen=True)
class TicketSettings:
    bot_token: str | None
    admins: frozenset[int]  # Telegram ids of those who approve senders
    reconcile_seconds: int = 1800

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TicketSettings:
        env = os.environ if env is None else env
        try:
            admins = frozenset(
                int(x) for x in (env.get("TICKETS_ADMINS") or "").replace(" ", "").split(",") if x
            )
            reconcile = int(env.get("TICKETS_RECONCILE_SECONDS") or 1800)
        except ValueError as exc:
            raise SettingsError(f"TICKETS_ADMINS / TICKETS_RECONCILE_SECONDS: {exc}") from exc
        return cls((env.get("TICKETS_BOT_TOKEN") or "").strip() or None, admins, reconcile)


def _person(update: dict) -> int:
    """Whose update it is: they are handled in order, everyone else alongside."""
    for kind in ("message", "callback_query"):
        if who := (update.get(kind) or {}).get("from"):
            return int(who.get("id") or 0)
    return 0


def hook_secret(jwt_secret: str) -> str:
    """The webhook path's secret, derived so that it needs no setting of its own."""
    return hmac.new(jwt_secret.encode(), b"yougile-cloud/tickets-hook", hashlib.sha256).hexdigest()[
        :40
    ]


def hook_url(settings: Settings) -> str:
    return settings.public_url + HOOK_PREFIX + hook_secret(settings.jwt_secret)


class TicketService:
    """Runs the bot with the token and approvers from the admin page (the ``ticket_bot`` row),
    or from the environment when the page has none; re-reads them every WATCH_SECONDS."""

    WATCH_SECONDS = 20
    RETRY_SECONDS = 60

    def __init__(
        self,
        settings: Settings,
        tickets: TicketSettings,
        *,
        transport: Any = None,  # tests: a fake YouGile
    ) -> None:
        self.settings = settings
        self.tickets = tickets
        self.transport = transport
        self.secret = hook_secret(settings.jwt_secret)
        self.secrets = Secrets(settings.encryption_keys, settings.jwt_secret)
        self.db: Database | None = None
        self.kv: KV | None = None
        self.store: TicketStore | None = None
        self.desks: Desks | None = None
        self.tg: Telegram | None = None
        self.bot: TicketBot | None = None
        self._token: str | None = None
        self._poller: asyncio.Task | None = None
        self._polling = True
        self._tasks: set[asyncio.Task] = set()

    async def start(self, *, use_kv: bool = True, poll: bool = True) -> None:
        self._polling = poll
        self.db = Database(self.settings.database_url, max_size=5)
        await self.db.open()
        await self.db.migrate()
        self.kv = await KV.connect(self.settings.valkey_url) if use_kv else None
        self.store = TicketStore(self.db)
        self.desks = Desks(
            self.secrets,
            self.settings.yougile_base_url,
            kv=self.kv,
            rate_limit=self.settings.rate_limit,
            transport=self.transport,
        )
        await self.apply_config()
        if poll:
            self._spawn(self._watch())
            self._spawn(self._reconcile())
            self._spawn(self._retry())
            self._spawn(self._actions())

    async def config(self) -> tuple[str | None, frozenset[int]]:
        """The token and the approvers: the admin page's, else the environment's."""
        assert self.store
        stored = await self.store.bot_config()
        token = None
        if stored and stored.token_enc:
            try:
                token = self.secrets.decrypt(stored.token_enc)
            except ValueError:
                log.error("the stored bot token cannot be decrypted with ENCRYPTION_KEYS")
        admins = (stored.admins if stored else frozenset()) | self.tickets.admins
        return token or self.tickets.bot_token, admins

    async def apply_config(self) -> None:
        """Start, restart (new token) or stop the bot; keep its approvers current."""
        assert self.store and self.desks
        token, admins = await self.config()
        if token != self._token:
            await self._stop_bot()
            self._token = token
            if token:
                self.tg = Telegram(token)
                self.bot = TicketBot(self.tg, self.store, self.desks, admins)
                if self._polling:
                    self._poller = self._spawn(self._poll(self.tg, self.bot))
                log.info("the ticket bot is running")
            else:
                log.warning("no bot token (admin page or TICKETS_BOT_TOKEN): the bot is idle")
        if self.bot is not None:
            if not admins:
                log.warning("nobody approves senders: add approvers on the admin page")
            changed = self.bot.admins != admins
            self.bot.admins = admins
            if changed and self._polling and self._poller:
                self._spawn(self.bot.setup())

    async def _stop_bot(self) -> None:
        if self._poller:
            self._poller.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._poller
            self._poller = None
        if self.tg:
            await self.tg.aclose()
        self.tg = self.bot = None

    async def stop(self) -> None:
        await self._stop_bot()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.desks:
            await self.desks.close()
        if self.kv:
            await self.kv.close()
        if self.db:
            await self.db.close()

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.WATCH_SECONDS)
            try:
                await self.apply_config()
            except Exception:
                log.exception("re-reading the bot settings failed")

    async def _actions(self) -> None:
        """The admin page's requests about people (approve, revoke, block…): carried out here,
        where the bot can tell the person."""
        while True:
            await asyncio.sleep(ACTIONS_SECONDS)
            if self.bot is None:
                continue
            try:
                await self.bot.run_actions()
            except Exception:
                log.exception("the admin page's requests failed")

    @staticmethod
    async def _poll(tg: Telegram, bot: TicketBot) -> None:
        offset: int | None = None
        people: dict[int, list] = {}  # person -> [their lock, updates of theirs not done]
        running: set[asyncio.Task] = set()
        slots = asyncio.Semaphore(CONCURRENT_UPDATES)

        async def _one(update: dict, person: int) -> None:
            entry = people[person]
            try:
                async with entry[0], slots:
                    await bot.handle(update)
            except Exception:
                log.exception("update %s failed", update.get("update_id"))
            finally:
                entry[1] -= 1
                if entry[1] == 0:
                    people.pop(person, None)

        with contextlib.suppress(TelegramError):
            await tg.call("deleteWebhook")  # long polling and a webhook exclude each other
        await bot.setup()
        try:
            while True:
                try:
                    updates = await tg.updates(offset)
                except TelegramError as exc:
                    log.warning("getUpdates: %s", exc)
                    await asyncio.sleep(5)
                    continue
                for update in updates:
                    offset = update["update_id"] + 1
                    # Handled side by side (a ticket with files must not hold everyone up), but
                    # in order for each person: their lock is taken in arrival order.
                    person = _person(update)
                    people.setdefault(person, [asyncio.Lock(), 0])[1] += 1
                    task = asyncio.create_task(_one(update, person))
                    running.add(task)
                    task.add_done_callback(running.discard)
        finally:
            # Telegram already counts these as delivered: a stop (a deploy) lets them finish
            # instead of dropping what someone just wrote.
            if running:
                log.info("stopping: %d update(s) still being handled", len(running))
                _done, left = await asyncio.wait(set(running), timeout=DRAIN_SECONDS)
                for task in left:
                    task.cancel()
                if left:
                    log.warning("stopped with %d update(s) unfinished", len(left))

    async def _reconcile(self) -> None:
        """Webhooks can be lost: look at every open ticket now and then — and soon after a
        start, since webhooks and failed reports of the previous run are gone with it."""
        first = True
        while True:
            await asyncio.sleep(FIRST_CHECK if first else self.tickets.reconcile_seconds)
            if self.bot is None:
                continue
            try:
                await self.bot.store.drop_stale_drafts()
                await self.bot.store.prune_messages()
                failed = await self.bot.refresh_open(
                    fresh_within=0 if first else self.tickets.reconcile_seconds,
                    pause=RECONCILE_PAUSE,
                )
                log.info("open tickets checked (%d failed)", failed)
                first = False
            except Exception:
                log.exception("checking open tickets failed")

    async def _retry(self) -> None:
        """Reports that failed (Telegram unreachable) go out within a minute of its return."""
        while True:
            await asyncio.sleep(self.RETRY_SECONDS)
            if self.bot is None or not self.bot.pending:
                continue
            try:
                await self.bot.retry_pending()
            except Exception:
                log.exception("retrying failed reports failed")

    async def _handle_event(self, event: dict) -> None:
        bot = self.bot
        if bot is None:
            return
        try:
            await bot.on_event(event)
        except Exception:
            log.exception("YouGile event %s failed", event.get("event"))

    # ---------- HTTP ----------

    async def hook(self, request: Request) -> Response:
        if not hmac.compare_digest(request.path_params.get("secret", ""), self.secret):
            return Response(status_code=404)
        if self.bot is None:
            return Response(status_code=503)
        try:
            event = await request.json()
        except ValueError:
            return Response(status_code=400)
        if isinstance(event, dict):
            # Answer at once: YouGile should not wait for Telegram and YouGile round trips.
            self._spawn(self._handle_event(event))
        return JSONResponse({"ok": True})

    async def healthz(self, _request: Request) -> Response:
        return JSONResponse({"ok": True, "bot": self.bot is not None})

    def app(self) -> Starlette:
        @contextlib.asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            await self.start()
            try:
                yield
            finally:
                await self.stop()

        return Starlette(
            routes=[
                Route(HOOK_PREFIX + "{secret}", self.hook, methods=["POST"]),
                Route("/healthz", self.healthz, methods=["GET"]),
            ],
            lifespan=lifespan,
        )


def create_app() -> Starlette:
    return TicketService(Settings.from_env(), TicketSettings.from_env()).app()
