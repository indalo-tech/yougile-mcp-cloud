"""Assembly: the core MCP server + OAuth + per-request tenancy + sign-in and admin pages."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from starlette.applications import Starlette
from yougile_mcp.server import build_server

from .admin import AdminPages
from .crypto import Secrets
from .db import Database
from .kv import KV
from .oauth import CloudOAuthProvider
from .settings import Settings
from .signin import SignIn
from .tenancy import Tenancy, TenantMiddleware
from .web import SignInPages, healthz
from .yougile_auth import YouGileAuth


class Services:
    """Everything with connections; created on startup, closed on shutdown."""

    def __init__(self, settings: Settings, transport: Any = None) -> None:
        self.settings = settings
        self.transport = transport  # tests pass a fake YouGile (httpx2 transport)
        self.secrets = Secrets(settings.encryption_keys, settings.jwt_secret)
        self.provider = CloudOAuthProvider(settings, self.secrets)
        self.pages = SignInPages(settings, self.secrets, self.provider)
        self.admin = AdminPages(settings, self.secrets)
        self.db: Database | None = None
        self.kv: KV | None = None
        self.tenancy: Tenancy | None = None

    async def start(self) -> None:
        self.db = Database(self.settings.database_url)
        await self.db.open()
        await self.db.migrate()
        self.kv = await KV.connect(self.settings.valkey_url)
        auth = YouGileAuth(self.settings.yougile_base_url, self.transport)
        self.tenancy = Tenancy(self.settings, self.db, self.kv, self.secrets, self.transport)
        self.provider.attach(self.db, self.kv)
        signin = SignIn(self.settings, self.db, self.kv, self.secrets, auth)
        self.pages.attach(signin, self.kv)
        self.admin.attach(self.db, self.kv, signin, auth, self.tenancy, self.transport)

    async def stop(self) -> None:
        if self.tenancy:
            await self.tenancy.close()
        if self.kv:
            await self.kv.close()
        if self.db:
            await self.db.close()


def create_server(settings: Settings, *, transport: Any = None) -> tuple[FastMCP, Services]:
    services = Services(settings, transport)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict]:
        await services.start()
        try:
            yield {}
        finally:
            await services.stop()

    mcp = build_server(
        auth=services.provider,
        middleware=[TenantMiddleware(lambda: services.tenancy)],
        lifespan=lifespan,
    )
    mcp.custom_route("/", methods=["GET"])(services.pages.home)
    mcp.custom_route("/signin", methods=["GET"])(services.pages.show)
    mcp.custom_route("/signin", methods=["POST"])(services.pages.submit)
    mcp.custom_route("/signin/company", methods=["POST"])(services.pages.choose_company)
    for path, method, endpoint in services.admin.routes():
        mcp.custom_route(path, methods=[method])(endpoint)
    mcp.custom_route("/healthz", methods=["GET"])(healthz)
    return mcp, services


def create_app(settings: Settings | None = None) -> Starlette:
    mcp, _ = create_server(settings or Settings.from_env())
    return mcp.http_app(path="/mcp")
