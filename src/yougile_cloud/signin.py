"""Sign-in with YouGile credentials: attempt limits, company access, a dedicated API key."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .access import Access, access_of
from .crypto import Secrets
from .db import Company, Database, User
from .kv import KV
from .settings import Settings
from .yougile_auth import YouGileAuth, YouGileCompany

log = logging.getLogger(__name__)

IP_LIMIT, IP_WINDOW = 20, 600  # sign-in attempts per client IP per 10 minutes
LOGIN_LIMIT, LOGIN_WINDOW = 8, 900  # per YouGile login per 15 minutes


class TooManyAttempts(Exception):  # noqa: N818
    pass


@dataclass(frozen=True)
class Connected:
    user: User | None  # None when the company has no access: no key is issued then
    company: Company
    access: Access


class SignIn:
    def __init__(
        self, settings: Settings, db: Database, kv: KV, secrets: Secrets, auth: YouGileAuth
    ) -> None:
        self.settings, self.db, self.kv, self.secrets, self.auth = settings, db, kv, secrets, auth

    async def count_attempt(self, ip: str, login: str) -> None:
        by_ip = await self.kv.hit(self.kv.key("try", "ip", ip), IP_WINDOW)
        by_login = await self.kv.hit(
            self.kv.key("try", "login", login.strip().lower()), LOGIN_WINDOW
        )
        if by_ip > IP_LIMIT or by_login > LOGIN_LIMIT:
            raise TooManyAttempts()

    async def companies(self, login: str, password: str) -> list[YouGileCompany]:
        return await self.auth.companies(login, password)

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
        me = await self.auth.me(key)
        user, previous = await self.db.upsert_user(
            company_id=company.id,
            yougile_user_id=me.id,
            email=me.email,
            name=me.name,
            is_admin=me.is_admin,
            api_key_enc=self.secrets.encrypt(key),
        )
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
