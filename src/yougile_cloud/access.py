"""Whether a company may use the service right now: exempt, paid, trial, expired or blocked."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .db import Company


@dataclass(frozen=True)
class Access:
    allowed: bool
    state: str  # exempt | active | trial | expired | blocked
    until: datetime | None = None

    def message(self) -> str:
        if self.state == "blocked":
            return "Доступ компании к YouGile MCP закрыт. Напишите в поддержку."
        if self.state == "expired":
            return "Пробный период закончился. Чтобы продолжить, оплатите подписку."
        return ""


def access_of(
    company: Company, free_company_ids: frozenset[str], now: datetime | None = None
) -> Access:
    now = now or datetime.now(UTC)
    if company.status == "blocked":
        return Access(False, "blocked")
    if company.status == "exempt" or company.id in free_company_ids:
        return Access(True, "exempt")
    if company.paid_until and company.paid_until > now:
        return Access(True, "active", company.paid_until)
    if company.trial_ends_at and company.trial_ends_at > now:
        return Access(True, "trial", company.trial_ends_at)
    return Access(False, "expired", company.trial_ends_at)
