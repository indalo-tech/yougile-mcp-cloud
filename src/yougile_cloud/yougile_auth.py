"""YouGile's own sign-in endpoints: list companies and issue/revoke API keys by login+password.

The password is used for these calls only and is never stored or logged.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from yougile_mcp.client import YouGileClient, YouGileError


class BadCredentials(Exception):  # noqa: N818 - reads better at call sites
    pass


class KeyLimitReached(Exception):  # noqa: N818
    pass


@dataclass(frozen=True)
class YouGileCompany:
    id: str
    name: str
    is_admin: bool


@dataclass(frozen=True)
class YouGileMe:
    id: str
    email: str
    name: str
    is_admin: bool


class YouGileAuth:
    def __init__(self, base_url: str, transport=None) -> None:  # noqa: ANN001 - httpx2 transport
        self.base_url = base_url
        self.transport = transport

    def _client(self, api_key: str | None = None) -> YouGileClient:
        return YouGileClient(api_key, self.base_url, transport=self.transport, max_attempts=2)

    async def companies(self, login: str, password: str) -> list[YouGileCompany]:
        async with self._client() as client:
            try:
                page = await client.request(
                    "POST",
                    "/auth/companies",
                    json={"login": login, "password": password},
                    query={"limit": 1000},
                    auth=False,
                )
            except YouGileError as exc:
                if exc.status in (400, 401, 403, 404):
                    raise BadCredentials() from exc
                raise
        return [
            YouGileCompany(c["id"], c.get("name") or c["id"], bool(c.get("isAdmin")))
            for c in (page or {}).get("content", [])
        ]

    async def create_key(self, login: str, password: str, company_id: str) -> str:
        async with self._client() as client:
            try:
                created = await client.request(
                    "POST",
                    "/auth/keys",
                    json={"login": login, "password": password, "companyId": company_id},
                    auth=False,
                )
            except YouGileError as exc:
                if exc.status in (401, 403):
                    raise BadCredentials() from exc
                if exc.status == 400 and "30" in exc.message:
                    raise KeyLimitReached() from exc
                raise
        return created["key"]

    async def delete_key(self, key: str) -> None:
        """Best effort: a key the user already deleted in YouGile is fine."""
        async with self._client() as client:
            with contextlib.suppress(YouGileError):
                await client.request("DELETE", f"/auth/keys/{key}", auth=False)

    async def me(self, api_key: str) -> YouGileMe:
        async with self._client(api_key) as client:
            me = await client.request("GET", "/users/me")
        return YouGileMe(
            id=me["id"],
            email=me.get("email") or "",
            name=me.get("realName") or me.get("email") or "",
            is_admin=bool(me.get("isAdmin")),
        )
