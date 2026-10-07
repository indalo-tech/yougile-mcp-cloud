"""`yougile-cloud tickets …`: run the ticket bot and set up its accounts (operator commands)."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from yougile_mcp.client import YouGileClient, YouGileError

from ..crypto import Secrets
from ..db import Database
from ..settings import Settings, SettingsError
from ..yougile_auth import BadCredentials, YouGileAuth
from .desk import Desks
from .service import WEBHOOK_EVENTS, hook_url
from .store import TicketStore


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except SettingsError as exc:
        sys.exit(f"yougile-cloud: {exc}")


def _run(fn: Callable[[Settings, TicketStore], Awaitable[Any]]) -> Any:
    settings = _settings()

    async def go() -> Any:
        db = Database(settings.database_url, max_size=2)
        await db.open()
        try:
            await db.migrate()
            return await fn(settings, TicketStore(db))
        finally:
            await db.close()

    return asyncio.run(go())


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    settings = _settings()
    uvicorn.run(
        "yougile_cloud.tickets.service:create_app",
        factory=True,
        host=args.host or settings.host,
        port=args.port or settings.port,
        proxy_headers=False,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        server_header=False,
        access_log=False,  # health checks every 15 s would drown the log
    )


async def _login(settings: Settings, what: str, company: str | None) -> tuple[str, str]:
    """Ask for a YouGile login and password (never stored), return (api key, company id)."""
    auth = YouGileAuth(settings.yougile_base_url)
    login = input(f"Логин (почта) {what} в YouGile: ").strip()
    password = getpass.getpass("Пароль (не сохраняется): ")
    try:
        companies = await auth.companies(login, password)
    except BadCredentials:
        sys.exit("неверный логин или пароль")
    if company:
        companies = [c for c in companies if c.id == company or c.name == company]
    if len(companies) != 1:
        names = ", ".join(f"{c.name} ({c.id})" for c in companies) or "нет"
        sys.exit(f"укажите компанию --company; доступны: {names}")
    key = await auth.create_key(login, password, companies[0].id)
    return key, companies[0].id


def cmd_account_add(args: argparse.Namespace) -> None:
    async def run(settings: Settings, store: TicketStore) -> None:
        key, company_id = await _login(settings, "учётки бота", args.company)
        me = await YouGileAuth(settings.yougile_base_url).me(key)
        secrets = Secrets(settings.encryption_keys, settings.jwt_secret)
        account = await store.add_account(
            name=args.name,
            company_id=company_id,
            bot_user_id=me.id,
            api_key_enc=secrets.encrypt(key),
        )
        desk = Desks(secrets, settings.yougile_base_url, kv=None, rate_limit=settings.rate_limit)
        try:
            projects = await desk.get(account).projects()
        finally:
            await desk.close()
        names = ", ".join(p.title for p in projects.values()) or "пока нет"
        print(
            f"учётка #{account.id} «{account.name}»: бот пишет как {me.name or me.email}; "
            f"проекты с доской «Заявки»: {names}"
        )

    _run(run)


def cmd_accounts(_args: argparse.Namespace) -> None:
    async def run(_settings: Settings, store: TicketStore) -> None:
        for a in await store.accounts():
            print(f"#{a.id}  {a.name}  company={a.company_id}  bot_user={a.bot_user_id}")

    _run(run)


def cmd_senders(_args: argparse.Namespace) -> None:
    async def run(_settings: Settings, store: TicketStore) -> None:
        for s in await store.senders():
            who = f"@{s.username}" if s.username else ""
            print(f"{s.tg_user_id:<12} {s.status:<9} {s.project_name or '-':<20} {s.name} {who}")

    _run(run)


def cmd_block(args: argparse.Namespace) -> None:
    async def run(_settings: Settings, store: TicketStore) -> None:
        sender = await store.decide(args.tg_user_id, "blocked", by=None)
        print("заблокирован" if sender else "нет такого отправителя")

    _run(run)


def cmd_webhooks(args: argparse.Namespace) -> None:
    """Subscribe the bot to YouGile events of each account's company (idempotent)."""

    async def run(settings: Settings, store: TicketStore) -> None:
        secrets = Secrets(settings.encryption_keys, settings.jwt_secret)
        url = hook_url(settings)
        companies: dict[str, bytes] = {}
        for a in await store.accounts():
            companies.setdefault(a.company_id, a.api_key_enc)
        if not companies:
            sys.exit("сначала добавьте учётку бота: yougile-cloud tickets account-add")
        for company_id, key_enc in companies.items():
            temporary = None
            if args.admin:
                temporary, _ = await _login(settings, "администратора", company_id)
            key = temporary or secrets.decrypt(key_enc)
            try:
                async with YouGileClient(key, settings.yougile_base_url) as client:
                    print(company_id, ": ", ", ".join(await _subscribe(client, url)), sep="")
            except YouGileError as exc:
                if exc.status == 403 and not args.admin:
                    sys.exit(
                        "у учётки бота нет прав на подписки: запустите с --admin и войдите "
                        "администратором компании (его ключ удалится сразу после)"
                    )
                raise
            finally:
                if temporary:
                    await YouGileAuth(settings.yougile_base_url).delete_key(temporary)

    _run(run)


async def _subscribe(client: YouGileClient, url: str) -> list[str]:
    listed = await client.request("GET", "/webhooks")
    hooks = listed.get("content", []) if isinstance(listed, dict) else listed or []
    done: list[str] = []
    for event in WEBHOOK_EVENTS:
        mine = [h for h in hooks if h.get("url") == url and h.get("event") == event]
        alive = [h for h in mine if not h.get("deleted")]
        if not alive:
            await client.request(
                "POST", "/webhooks", json={"url": url, "event": event, "filters": []}
            )
            done.append(f"{event}: создана")
        elif alive[0].get("disabled"):
            await client.request("PUT", f"/webhooks/{alive[0]['id']}", json={"disabled": False})
            done.append(f"{event}: включена")
        else:
            done.append(f"{event}: уже есть")
    return done


def register(sub: Any) -> None:
    tickets = sub.add_parser("tickets", help="the Telegram ticket bot")
    cmds = tickets.add_subparsers(dest="tickets_command", required=True)
    serve = cmds.add_parser("serve", help="run the bot and its webhook endpoint")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)
    add = cmds.add_parser(
        "account-add", help="add the bot's YouGile account in a company (asks for its login)"
    )
    add.add_argument("name", help="a name for the account, e.g. the company's")
    add.add_argument("--company", help="YouGile company id or name, if the login has several")
    add.set_defaults(func=cmd_account_add)
    cmds.add_parser("accounts", help="list the bot's accounts").set_defaults(func=cmd_accounts)
    cmds.add_parser("senders", help="list senders and their status").set_defaults(func=cmd_senders)
    block = cmds.add_parser("block", help="block a sender")
    block.add_argument("tg_user_id", type=int)
    block.set_defaults(func=cmd_block)
    hooks = cmds.add_parser("webhooks", help="subscribe to YouGile events")
    hooks.add_argument("--admin", action="store_true", help="sign in as a company admin")
    hooks.set_defaults(func=cmd_webhooks)
