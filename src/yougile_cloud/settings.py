"""Configuration from the environment. Secrets never come from files or the database."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from yougile_mcp.client import DEFAULT_BASE_URL, normalize_base_url


class SettingsError(ValueError):
    pass


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise SettingsError(f"{name} is required")
    return value


def _csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    public_url: str  # https://yougile.example.com — OAuth issuer, token audience base
    database_url: str
    valkey_url: str  # valkey://host:port[/db]
    encryption_keys: list[str]  # Fernet keys, first one encrypts (rotation: prepend a new key)
    jwt_secret: str  # high-entropy secret for signing tokens and CSRF
    free_company_ids: frozenset[str] = frozenset()  # YouGile companies that never pay
    trial_days: int = 14
    yougile_base_url: str = DEFAULT_BASE_URL
    rate_limit: int = 45  # YouGile allows 50/min per company; keep headroom for the web UI
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 30 * 24 * 3600
    admin_session_ttl: int = 12 * 3600
    host: str = "127.0.0.1"
    port: int = 8000
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def mcp_url(self) -> str:
        return f"{self.public_url}/mcp"

    @property
    def admin_audience(self) -> str:
        return f"{self.public_url}/admin"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        public_url = _required(env, "PUBLIC_URL").rstrip("/")
        if not public_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise SettingsError("PUBLIC_URL must be https (http only for localhost)")
        keys = _csv(env.get("ENCRYPTION_KEYS"))
        if not keys:
            raise SettingsError("ENCRYPTION_KEYS is required (comma-separated Fernet keys)")
        secret = _required(env, "JWT_SECRET")
        if len(secret) < 32:
            raise SettingsError("JWT_SECRET must be at least 32 characters")
        try:
            return cls(
                public_url=public_url,
                database_url=_required(env, "DATABASE_URL"),
                valkey_url=_required(env, "VALKEY_URL"),
                encryption_keys=keys,
                jwt_secret=secret,
                free_company_ids=frozenset(_csv(env.get("FREE_COMPANY_IDS"))),
                trial_days=int(env.get("TRIAL_DAYS", 14)),
                yougile_base_url=normalize_base_url(
                    env.get("YOUGILE_BASE_URL") or DEFAULT_BASE_URL
                ),
                rate_limit=int(env.get("RATE_LIMIT_PER_COMPANY", 45)),
                access_token_ttl=int(env.get("ACCESS_TOKEN_TTL", 3600)),
                refresh_token_ttl=int(env.get("REFRESH_TOKEN_TTL", 30 * 24 * 3600)),
                admin_session_ttl=int(env.get("ADMIN_SESSION_TTL", 12 * 3600)),
                host=env.get("HOST", "127.0.0.1"),
                port=int(env.get("PORT", 8000)),
            )
        except ValueError as exc:
            raise SettingsError(f"invalid number in settings: {exc}") from exc
