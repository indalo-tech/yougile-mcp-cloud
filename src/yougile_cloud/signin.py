"""Sign-in with YouGile credentials: attempt limits, company access, a dedicated API key."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .access import Access, access_of
from .crypto import Secrets
from .db import Company, Database, User
from .kv import KV
from .settings import Settings
from .yougile_auth import BadCredentials, YouGileAuth, YouGileCompany

log = logging.getLogger(__name__)

# Failed sign-ins only (a person signing in again and again is not an attack):
IP_LIMIT, IP_WINDOW = 20, 600  # per client IP per 10 minutes
LOGIN_LIMIT, LOGIN_WINDOW = 8, 900  # per YouGile login from one IP per 15 minutes
LOGIN_TOTAL_LIMIT = 30  # per YouGile login from anywhere per 15 minutes


class TooManyAttempts(Exception):  # noqa: N818
    pass


@dataclass(frozen=True)
class Connected:
    user: User | None  # None when the company has no access or the person is banned: no key
    company: Company
    access: Access
    banned: bool = False  # the company admin forbade this person to connect


class SignIn:
    def __init__(
        self, settings: Settings, db: Database, kv: KV, secrets: Secrets, auth: YouGileAuth
    ) -> None:
        self.settings, self.db, self.kv, self.secrets, self.auth = settings, db, kv, secrets, auth
        self.on_key_change: Callable[[int], None] | None = None  # the app: tenancy.forget

    def _keys(self, ip: str, login: str) -> list[tuple[str, int, int]]:
        name = login.strip().lower()
        return [
            (self.kv.key("try", "ip", ip), IP_WINDOW, IP_LIMIT),
            # keyed by login and IP: a stranger elsewhere cannot lock someone out by guessing
            (self.kv.key("try", "login", name, ip), LOGIN_WINDOW, LOGIN_LIMIT),
            (self.kv.key("try", "login", name), LOGIN_WINDOW, LOGIN_TOTAL_LIMIT),
        ]

    async def count_attempt(self, ip: str, login: str) -> None:
        """Refuse a sign-in once too many have failed; counts nothing itself."""
        for key, _window, limit in self._keys(ip, login):
            if await self.kv.peek(key) >= limit:
                raise TooManyAttempts()

    async def failed_attempt(self, ip: str, login: str) -> None:
        for key, window, _limit in self._keys(ip, login):
            await self.kv.hit(key, window)

    async def companies(
        self, login: str, password: str, ip: str | None = None
    ) -> list[YouGileCompany]:
        try:
            return await self.auth.companies(login, password)
        except BadCredentials:
            if ip is not None:
                await self.failed_attempt(ip, login)
            raise

    async def connect(self, login: str, password: str, company: YouGileCompany) -> Connected:
        """Create or refresh the user's own key for this service and store it encrypted."""
        exempt = company.id in self.settings.free_company_ids
        record = await self.db.ensure_company(
            company.id, company.name, trial_days=self.settings.trial_days, exempt=exempt
        )
        access = access_of(record, self.settings.free_company_ids)
        if not access.allowed:
            return Connected(user=None, company=record, access=access)

        key = await self.auth.create_key(login, password, company.id)
        try:
            me = await self.auth.me(key)
            # Who it is is known only now. A company admin is never kept out: they could lift
            # the ban themselves, and the admin page signs in this way too.
            rights = await self.db.get_rights(company.id, me.id)
            banned = bool(rights and rights.blocked and not me.is_admin)
            if not banned:
                user, previous = await self.db.upsert_user(
                    company_id=company.id,
                    yougile_user_id=me.id,
                    email=me.email,
                    name=me.name,
                    is_admin=me.is_admin,
                    api_key_enc=self.secrets.encrypt(key),
                )
        except BaseException:
            # Not stored: the key would be an orphan counting towards YouGile's 30 per account.
            await self.auth.delete_key(key)
            raise
        if banned:
            await self.auth.delete_key(key)
            await self.db.audit("sign_in_banned", company_id=company.id, target=me.id)
            log.info("banned user %s refused for company %s", me.id, company.id)
            return Connected(user=None, company=record, access=access, banned=True)
        if self.on_key_change:
            self.on_key_change(user.id)  # a cached session must not keep the revoked key
        if previous:
            # One key per person for this service: drop the old one so keys don't pile up
            # towards YouGile's limit of 30 per account.
            try:
                old = self.secrets.decrypt(previous)
            except ValueError:
                old = None
            if old and old != key:
                await self.auth.delete_key(old)
        await self.db.audit("sign_in", company_id=company.id, user_id=user.id, admin=me.is_admin)
        log.info("user %s signed in for company %s", user.id, company.id)
        return Connected(user=user, company=record, access=access)
