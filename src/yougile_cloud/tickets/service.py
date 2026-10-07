"""The ticket bot's process: Telegram long polling, the YouGile webhook endpoint, a periodic
check of open tickets. Without TICKETS_BOT_TOKEN it stays idle (and healthy), so the container
can be deployed before the bot is configured."""

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


def hook_secret(jwt_secret: str) -> str:
    """The webhook path's secret, derived so that it needs no setting of its own."""
    return hmac.new(jwt_secret.encode(), b"yougile-cloud/tickets-hook", hashlib.sha256).hexdigest()[
        :40
    ]


def hook_url(settings: Settings) -> str:
    return settings.public_url + HOOK_PREFIX + hook_secret(settings.jwt_secret)


class TicketService:
    def __init__(
        self,
        settings: Settings,
        tickets: TicketSettings,
        *,
        transport: Any = None,  # tests: a fake YouGile
        tg: Telegram | None = None,  # tests: a Telegram with a fake transport
    ) -> None:
        self.settings = settings
        self.tickets = tickets
        self.transport = transport
        self.tg = tg
        self.secret = hook_secret(settings.jwt_secret)
        self.db: Database | None = None
        self.kv: KV | None = None
        self.desks: Desks | None = None
        self.bot: TicketBot | None = None
        self._tasks: set[asyncio.Task] = set()

    async def start(self, *, use_kv: bool = True, poll: bool = True) -> None:
        if not self.tickets.bot_token and self.tg is None:
            log.warning("TICKETS_BOT_TOKEN is not set: the ticket bot is idle")
            return
        if not self.tickets.admins:
            log.warning("TICKETS_ADMINS is empty: nobody can approve senders")
        self.db = Database(self.settings.database_url, max_size=5)
        await self.db.open()
        await self.db.migrate()
        self.kv = await KV.connect(self.settings.valkey_url) if use_kv else None
        secrets = Secrets(self.settings.encryption_keys, self.settings.jwt_secret)
        self.desks = Desks(
            secrets,
            self.settings.yougile_base_url,
            kv=self.kv,
            rate_limit=self.settings.rate_limit,
            transport=self.transport,
        )
        self.tg = self.tg or Telegram(self.tickets.bot_token or "")
        self.bot = TicketBot(self.tg, TicketStore(self.db), self.desks, self.tickets.admins)
        if poll:
            self._spawn(self._poll())
            self._spawn(self._reconcile())

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.desks:
            await self.desks.close()
        if self.tg:
            await self.tg.aclose()
        if self.kv:
            await self.kv.close()
        if self.db:
            await self.db.close()

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _poll(self) -> None:
        assert self.tg and self.bot
        offset: int | None = None
        with contextlib.suppress(TelegramError):
            await self.tg.call("deleteWebhook")  # long polling and a webhook exclude each other
        while True:
            try:
                updates = await self.tg.updates(offset)
            except TelegramError as exc:
                log.warning("getUpdates: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await self.bot.handle(update)
                except Exception:
                    log.exception("update %s failed", update.get("update_id"))

    async def _reconcile(self) -> None:
        """Webhooks can be lost: look at every open ticket now and then."""
        assert self.bot
        while True:
            await asyncio.sleep(self.tickets.reconcile_seconds)
            try:
                await self.bot.store.drop_stale_drafts()
                await self.bot.refresh_open()
            except Exception:
                log.exception("checking open tickets failed")

    async def _handle_event(self, event: dict) -> None:
        assert self.bot
        try:
            await self.bot.on_event(event)
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
