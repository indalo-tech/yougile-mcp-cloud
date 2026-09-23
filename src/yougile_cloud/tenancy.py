"""Per-user runtimes for the core tools, bound per MCP request through the core's ContextVar.

Nothing tenant-specific lives in shared server state: each call resolves the user from its
access token, checks the company's access, and binds that user's runtime for this request only.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from yougile_mcp import runtime
from yougile_mcp.client import YouGileClient
from yougile_mcp.config import ConfigError, WorkspaceConfig
from yougile_mcp.directory import Directory
from yougile_mcp.policy import Policy

from .access import access_of
from .crypto import Secrets
from .db import Company, Database, Rights, User
from .kv import KV, CompanyRateLimiter
from .settings import Settings

COMPANY_KEYS = ("timezone", "instructions", "confirm_projects", "workflows", "done_columns")


def workspace_config(company: Company, rights: Rights | None) -> WorkspaceConfig:
    """Company settings plus the user's rights (or the company's defaults for new users)."""
    settings = company.settings or {}
    data: dict[str, Any] = {k: settings[k] for k in COMPANY_KEYS if k in settings}
    data["role"] = (rights.role if rights and rights.role else None) or settings.get(
        "default_role", "member"
    )
    if rights and rights.projects is not None:
        data["projects"] = rights.projects
    elif settings.get("default_projects") is not None:
        data["projects"] = settings["default_projects"]
    data["deny"] = list(settings.get("deny", [])) + list(rights.deny if rights else [])
    try:
        return WorkspaceConfig.from_dict(data, source=f"company {company.id}")
    except ConfigError:
        # Never lock people out over a bad setting: fall back to the safe read-only role.
        return WorkspaceConfig.from_dict({"role": "reader"}, source="fallback")


@dataclass
class _Entry:
    runtime: runtime.Runtime
    version: tuple
    checked_at: float


class Tenancy:
    """Builds and caches one runtime per user; rebuilds when keys, settings or rights change."""

    RECHECK = 30.0  # seconds between database checks for a cached user
    MAX_ENTRIES = 2000

    def __init__(self, settings: Settings, db: Database, kv: KV, secrets: Secrets, transport=None):  # noqa: ANN001
        self.settings, self.db, self.kv, self.secrets = settings, db, kv, secrets
        self.transport = transport  # tests inject a fake YouGile
        self._cache: OrderedDict[int, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def load(self, user_id: int) -> tuple[User, Company, Rights | None]:
        user = await self.db.get_user(user_id)
        if user is None:
            raise ToolError("Сессия устарела: подключите YouGile MCP заново.")
        company = await self.db.get_company(user.company_id)
        assert company is not None
        rights = await self.db.get_rights(company.id, user.yougile_user_id)
        return user, company, rights

    def _build(self, user: User, company: Company, rights: Rights | None) -> runtime.Runtime:
        limiter = CompanyRateLimiter(self.kv, company.id, self.settings.rate_limit)
        client = YouGileClient(
            self.secrets.decrypt(user.api_key_enc),
            self.settings.yougile_base_url,
            limiter=limiter,
            transport=self.transport,
        )
        config = workspace_config(company, rights)
        return runtime.Runtime(
            client,
            config,
            Policy.from_config(config),
            Directory(client),
            allow_local_files=False,
            settings_hint=f"the company admin page {self.settings.public_url}/admin "
            "(company admins only; Workflow chains are under Настройки)",
        )

    async def runtime_for(self, user_id: int) -> runtime.Runtime:
        now = time.monotonic()
        entry = self._cache.get(user_id)
        if entry and now - entry.checked_at < self.RECHECK:
            self._cache.move_to_end(user_id)
            return entry.runtime
        user, company, rights = await self.load(user_id)
        access = access_of(company, self.settings.free_company_ids)
        if not access.allowed:
            raise ToolError(access.message())
        await self.db.touch_user(user.id)  # "last seen" for the admin page, once per RECHECK
        version = (user.updated_at, company.settings_version, rights.updated_at if rights else None)
        async with self._lock:
            entry = self._cache.get(user_id)
            if entry and entry.version == version:
                entry.checked_at = now
            else:
                if entry:
                    asyncio.create_task(entry.runtime.client.aclose())  # noqa: RUF006
                entry = _Entry(self._build(user, company, rights), version, now)
                self._cache[user_id] = entry
            self._cache.move_to_end(user_id)
            while len(self._cache) > self.MAX_ENTRIES:
                _, old = self._cache.popitem(last=False)
                asyncio.create_task(old.runtime.client.aclose())  # noqa: RUF006
        return entry.runtime

    def forget(self, user_id: int) -> None:
        entry = self._cache.pop(user_id, None)
        if entry:
            asyncio.create_task(entry.runtime.client.aclose())  # noqa: RUF006

    async def close(self) -> None:
        for entry in self._cache.values():
            await entry.runtime.client.aclose()
        self._cache.clear()


class TenantMiddleware(Middleware):
    """Binds the caller's runtime around tool calls and prompt rendering (prompts need the
    company's time zone); listings need no tenant."""

    def __init__(self, tenancy: Callable[[], Tenancy]) -> None:
        self.tenancy = tenancy  # resolved per call: the Tenancy is created at startup

    async def _bound(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        token = get_access_token()
        if token is None or not token.subject:
            raise ToolError("Нет авторизации: подключите YouGile MCP заново.")
        rt = await self.tenancy().runtime_for(int(token.subject))
        bound = runtime.bind(rt)
        try:
            return await call_next(context)
        finally:
            runtime.reset(bound)

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        return await self._bound(context, call_next)

    async def on_get_prompt(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        return await self._bound(context, call_next)
