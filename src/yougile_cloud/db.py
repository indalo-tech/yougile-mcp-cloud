"""PostgreSQL storage: companies, users, rights, OAuth clients, refresh tokens, audit log."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

MIGRATIONS = "yougile_cloud.migrations"


@dataclass(frozen=True)
class Company:
    id: str
    name: str
    status: str
    trial_ends_at: datetime | None
    paid_until: datetime | None
    settings: dict[str, Any]
    settings_version: int


@dataclass(frozen=True)
class User:
    id: int
    company_id: str
    yougile_user_id: str
    email: str
    name: str
    is_admin: bool
    api_key_enc: bytes
    updated_at: datetime


@dataclass(frozen=True)
class Rights:
    role: str | None = None
    projects: list[str] | None = None
    deny: list[str] = field(default_factory=list)
    updated_at: datetime | None = None


def _company(row: dict) -> Company:
    return Company(
        id=row["id"],
        name=row["name"],
        status=row["status"],
        trial_ends_at=row["trial_ends_at"],
        paid_until=row["paid_until"],
        settings=row["settings"] or {},
        settings_version=row["settings_version"],
    )


def _user(row: dict) -> User:
    return User(
        id=row["id"],
        company_id=row["company_id"],
        yougile_user_id=row["yougile_user_id"],
        email=row["email"],
        name=row["name"],
        is_admin=row["is_admin"],
        api_key_enc=bytes(row["api_key_enc"]),
        updated_at=row["updated_at"],
    )


class Database:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self.pool = AsyncConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )

    async def open(self) -> None:
        await self.pool.open(wait=True)

    async def close(self) -> None:
        await self.pool.close()

    async def _one(self, sql: str, params: Any = None) -> dict | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchone()

    async def _all(self, sql: str, params: Any = None) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def _exec(self, sql: str, params: Any = None) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount

    # ---------- migrations ----------

    async def migrate(self) -> list[str]:
        """Apply new migrations in order, each in a transaction. Returns the names applied."""
        files = sorted(f for f in resources.files(MIGRATIONS).iterdir() if f.name.endswith(".sql"))
        applied: list[str] = []
        async with self.pool.connection() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            await conn.execute("SELECT pg_advisory_lock(7271726)")  # one migrator at a time
            try:
                cur = await conn.execute("SELECT name FROM schema_migrations")
                done = {row["name"] for row in await cur.fetchall()}
                for file in files:
                    if file.name in done:
                        continue
                    async with conn.transaction():
                        await conn.execute(file.read_text("utf-8"))
                        await conn.execute(
                            "INSERT INTO schema_migrations (name) VALUES (%s)", (file.name,)
                        )
                    applied.append(file.name)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(7271726)")
        return applied

    # ---------- companies ----------

    async def get_company(self, company_id: str) -> Company | None:
        row = await self._one("SELECT * FROM companies WHERE id = %s", (company_id,))
        return _company(row) if row else None

    async def ensure_company(
        self, company_id: str, name: str, *, trial_days: int, exempt: bool
    ) -> Company:
        """Create a company on first sign-in (trial or exempt); keep the name up to date."""
        trial_ends = datetime.now(UTC) + timedelta(days=trial_days)
        row = await self._one(
            """
            INSERT INTO companies (id, name, status, trial_ends_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, updated_at = now()
            RETURNING *
            """,
            (company_id, name, "exempt" if exempt else "trial", None if exempt else trial_ends),
        )
        assert row is not None
        return _company(row)

    async def list_companies(self) -> list[Company]:
        return [_company(r) for r in await self._all("SELECT * FROM companies ORDER BY created_at")]

    async def set_company_access(
        self,
        company_id: str,
        *,
        status: str | None = None,
        trial_ends_at: datetime | None = None,
        paid_until: datetime | None = None,
    ) -> Company | None:
        row = await self._one(
            """
            UPDATE companies SET
                status = COALESCE(%s, status),
                trial_ends_at = COALESCE(%s, trial_ends_at),
                paid_until = COALESCE(%s, paid_until),
                updated_at = now()
            WHERE id = %s RETURNING *
            """,
            (status, trial_ends_at, paid_until, company_id),
        )
        return _company(row) if row else None

    async def save_company_settings(self, company_id: str, settings: dict[str, Any]) -> None:
        await self._exec(
            "UPDATE companies SET settings = %s, settings_version = settings_version + 1, "
            "updated_at = now() WHERE id = %s",
            (Jsonb(settings), company_id),
        )

    # ---------- users ----------

    async def upsert_user(
        self,
        *,
        company_id: str,
        yougile_user_id: str,
        email: str,
        name: str,
        is_admin: bool,
        api_key_enc: bytes,
    ) -> tuple[User, bytes | None]:
        """Store the user with a fresh key; returns the user and the previous key (to revoke)."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT api_key_enc FROM users WHERE company_id = %s AND yougile_user_id = %s "
                "FOR UPDATE",
                (company_id, yougile_user_id),
            )
            previous = await cur.fetchone()
            cur = await conn.execute(
                """
                INSERT INTO users (company_id, yougile_user_id, email, name, is_admin, api_key_enc)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (company_id, yougile_user_id) DO UPDATE SET
                    email = EXCLUDED.email, name = EXCLUDED.name, is_admin = EXCLUDED.is_admin,
                    api_key_enc = EXCLUDED.api_key_enc, updated_at = now()
                RETURNING *
                """,
                (company_id, yougile_user_id, email, name, is_admin, api_key_enc),
            )
            row = await cur.fetchone()
        assert row is not None
        return _user(row), (bytes(previous["api_key_enc"]) if previous else None)

    async def get_user(self, user_id: int) -> User | None:
        row = await self._one("SELECT * FROM users WHERE id = %s", (user_id,))
        return _user(row) if row else None

    async def list_users(self, company_id: str) -> list[User]:
        rows = await self._all(
            "SELECT * FROM users WHERE company_id = %s ORDER BY name, email", (company_id,)
        )
        return [_user(r) for r in rows]

    async def touch_user(self, user_id: int) -> None:
        await self._exec("UPDATE users SET last_seen_at = now() WHERE id = %s", (user_id,))

    async def delete_user(self, user_id: int) -> None:
        await self._exec("DELETE FROM users WHERE id = %s", (user_id,))

    # ---------- rights ----------

    async def get_rights(self, company_id: str, yougile_user_id: str) -> Rights | None:
        row = await self._one(
            "SELECT * FROM user_rights WHERE company_id = %s AND yougile_user_id = %s",
            (company_id, yougile_user_id),
        )
        if not row:
            return None
        return Rights(row["role"], row["projects"], row["deny"] or [], row["updated_at"])

    async def list_rights(self, company_id: str) -> dict[str, Rights]:
        rows = await self._all("SELECT * FROM user_rights WHERE company_id = %s", (company_id,))
        return {
            r["yougile_user_id"]: Rights(r["role"], r["projects"], r["deny"] or [], r["updated_at"])
            for r in rows
        }

    async def set_rights(
        self,
        company_id: str,
        yougile_user_id: str,
        *,
        role: str | None,
        projects: list[str] | None,
        deny: list[str],
    ) -> None:
        await self._exec(
            """
            INSERT INTO user_rights (company_id, yougile_user_id, role, projects, deny)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (company_id, yougile_user_id) DO UPDATE SET
                role = EXCLUDED.role, projects = EXCLUDED.projects, deny = EXCLUDED.deny,
                updated_at = now()
            """,
            (
                company_id,
                yougile_user_id,
                role,
                None if projects is None else Jsonb(projects),
                Jsonb(deny),
            ),
        )

    # ---------- OAuth clients and refresh tokens ----------

    async def get_client(self, client_id: str) -> dict | None:
        row = await self._one("SELECT info FROM oauth_clients WHERE client_id = %s", (client_id,))
        return row["info"] if row else None

    async def save_client(self, client_id: str, info: dict) -> None:
        await self._exec(
            "INSERT INTO oauth_clients (client_id, info) VALUES (%s, %s) "
            "ON CONFLICT (client_id) DO UPDATE SET info = EXCLUDED.info",
            (client_id, Jsonb(info)),
        )

    async def save_refresh(
        self,
        token_hash: str,
        *,
        user_id: int,
        client_id: str,
        scopes: list[str],
        resource: str | None,
        expires_at: datetime,
    ) -> None:
        await self._exec(
            "INSERT INTO refresh_tokens "
            "(token_hash, user_id, client_id, scopes, resource, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (token_hash, user_id, client_id, Jsonb(scopes), resource, expires_at),
        )

    async def get_refresh(self, token_hash: str) -> dict | None:
        return await self._one("SELECT * FROM refresh_tokens WHERE token_hash = %s", (token_hash,))

    async def take_refresh(self, token_hash: str) -> dict | None:
        """Delete and return a refresh token atomically (rotation: a token works once)."""
        return await self._one(
            "DELETE FROM refresh_tokens WHERE token_hash = %s RETURNING *", (token_hash,)
        )

    async def revoke_user_tokens(self, user_id: int) -> int:
        return await self._exec("DELETE FROM refresh_tokens WHERE user_id = %s", (user_id,))

    async def purge_expired(self) -> int:
        return await self._exec("DELETE FROM refresh_tokens WHERE expires_at < now()")

    # ---------- audit ----------

    async def audit(
        self,
        event: str,
        *,
        company_id: str | None = None,
        user_id: int | None = None,
        **detail: Any,
    ) -> None:
        await self._exec(
            "INSERT INTO audit_log (company_id, user_id, event, detail) VALUES (%s, %s, %s, %s)",
            (company_id, user_id, event, Jsonb(detail)),
        )
