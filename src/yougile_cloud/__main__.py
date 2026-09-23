"""yougile-cloud: run the hosted server and manage companies (platform operator commands)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta

from . import __version__
from .access import access_of
from .crypto import new_fernet_key, new_token
from .db import Database
from .settings import Settings, SettingsError


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except SettingsError as exc:
        sys.exit(f"yougile-cloud: {exc}")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    settings = _settings()
    uvicorn.run(
        "yougile_cloud.app:create_app",
        factory=True,
        host=args.host or settings.host,
        port=args.port or settings.port,
        proxy_headers=True,  # client IPs from the reverse proxy, for sign-in attempt limits
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        server_header=False,
    )


async def _with_db(fn):  # noqa: ANN001, ANN202
    db = Database(_settings().database_url, max_size=2)
    await db.open()
    try:
        return await fn(db)
    finally:
        await db.close()


def cmd_migrate(_args: argparse.Namespace) -> None:
    applied = asyncio.run(_with_db(lambda db: db.migrate()))
    print("applied:", ", ".join(applied) or "nothing new")


def cmd_genkeys(_args: argparse.Namespace) -> None:
    print(f"ENCRYPTION_KEYS={new_fernet_key()}")
    print(f"JWT_SECRET={new_token(48)}")


def cmd_companies(_args: argparse.Namespace) -> None:
    settings = _settings()

    async def run(db: Database) -> None:
        for c in await db.list_companies():
            access = access_of(c, settings.free_company_ids)
            until = f" until {access.until:%Y-%m-%d}" if access.until else ""
            users = len(await db.list_users(c.id))
            print(f"{c.id}  {access.state:<8}{until:<18} users={users:<4} {c.name}")

    asyncio.run(_with_db(run))


def cmd_company(args: argparse.Namespace) -> None:
    now = datetime.now(UTC)

    async def run(db: Database) -> None:
        changes: dict = {}
        if args.exempt:
            changes["status"] = "exempt"
        if args.block:
            changes["status"] = "blocked"
        if args.trial_days is not None:
            changes.update(status="trial", trial_ends_at=now + timedelta(days=args.trial_days))
        if args.paid_until:
            changes.update(
                status="active",
                paid_until=datetime.fromisoformat(args.paid_until).replace(tzinfo=UTC),
            )
        if not changes:
            sys.exit("nothing to change: use --exempt, --block, --trial-days N or --paid-until")
        company = await db.set_company_access(args.company_id, **changes)
        if company is None:
            sys.exit(f"company {args.company_id} not found (it appears after the first sign-in)")
        await db.audit(
            "company_access", company_id=company.id, **{k: str(v) for k, v in changes.items()}
        )
        print(f"{company.id} {company.name}: {company.status}")

    asyncio.run(_with_db(run))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(stream=sys.stderr, level=os.environ.get("LOG_LEVEL", "INFO").upper())
    # yougile_mcp.client already logs each YouGile call (method, path, status, time); httpx2
    # would repeat it with the full URL, query strings included.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="yougile-cloud")
    parser.add_argument("--version", action="version", version=f"yougile-cloud {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP server")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=cmd_serve)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("genkeys", help="print new ENCRYPTION_KEYS and JWT_SECRET").set_defaults(
        func=cmd_genkeys
    )
    sub.add_parser("companies", help="list companies and their access").set_defaults(
        func=cmd_companies
    )
    company = sub.add_parser("company", help="change a company's access")
    company.add_argument("company_id")
    company.add_argument("--exempt", action="store_true", help="free forever (own companies)")
    company.add_argument("--block", action="store_true")
    company.add_argument("--trial-days", type=int, help="(re)start a trial of N days from now")
    company.add_argument("--paid-until", help="YYYY-MM-DD: paid access until this date")
    company.set_defaults(func=cmd_company)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
