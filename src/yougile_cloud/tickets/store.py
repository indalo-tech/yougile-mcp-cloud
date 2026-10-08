"""PostgreSQL storage of the ticket bot (tables from migration 003_tickets.sql)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from ..db import Database


@dataclass(frozen=True)
class Account:
    """The bot's account in a YouGile company: its customers are that company's projects."""

    id: int
    name: str
    company_id: str
    bot_user_id: str
    api_key_enc: bytes


@dataclass(frozen=True)
class BotConfig:
    token_enc: bytes | None
    bot_username: str
    admins: frozenset[int]


@dataclass(frozen=True)
class Sender:
    tg_user_id: int
    account_id: int | None
    project_id: str | None  # the customer: tickets land on this project's «Заявки» board
    project_name: str
    name: str
    username: str
    status: str

    @property
    def approved(self) -> bool:
        return self.status == "approved" and bool(self.account_id and self.project_id)


@dataclass(frozen=True)
class Ticket:
    task_id: str
    account_id: int
    project_id: str
    tg_user_id: int
    number: str
    title: str
    column_id: str | None
    completed: bool
    deleted: bool
    last_message_id: int
    created_at: datetime
    tg_thread_id: int | None  # the ticket's topic in the sender's chat


def _account(row: dict) -> Account:
    return Account(
        id=row["id"],
        name=row["name"],
        company_id=row["company_id"],
        bot_user_id=row["bot_user_id"],
        api_key_enc=bytes(row["api_key_enc"]),
    )


def _sender(row: dict) -> Sender:
    return Sender(
        tg_user_id=row["tg_user_id"],
        account_id=row["account_id"],
        project_id=row["project_id"],
        project_name=row["project_name"],
        name=row["name"],
        username=row["username"],
        status=row["status"],
    )


def _ticket(row: dict) -> Ticket:
    return Ticket(**{k: row[k] for k in Ticket.__dataclass_fields__})


class TicketStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------- bot settings ----------

    async def bot_config(self) -> BotConfig | None:
        row = await self.db._one("SELECT * FROM ticket_bot WHERE id = 1")
        if row is None:
            return None
        token = row["token_enc"]
        return BotConfig(
            bytes(token) if token is not None else None,
            row["bot_username"],
            frozenset(int(x) for x in row["admins"] or []),
        )

    async def save_bot_config(
        self, *, admins: list[int], token_enc: bytes | None = None, bot_username: str | None = None
    ) -> None:
        """Save the approvers, and the token when one is given (None keeps the stored one)."""
        await self.db._exec(
            """
            INSERT INTO ticket_bot (id, token_enc, bot_username, admins)
            VALUES (1, %s, COALESCE(%s, ''), %s)
            ON CONFLICT (id) DO UPDATE SET
                token_enc = COALESCE(EXCLUDED.token_enc, ticket_bot.token_enc),
                bot_username = COALESCE(%s, ticket_bot.bot_username),
                admins = EXCLUDED.admins, updated_at = now()
            """,
            (token_enc, bot_username, admins, bot_username),
        )

    # ---------- accounts ----------

    async def account_of_company(self, company_id: str) -> Account | None:
        row = await self.db._one(
            "SELECT * FROM ticket_accounts WHERE company_id = %s", (company_id,)
        )
        return _account(row) if row else None

    async def add_account(
        self, *, name: str, company_id: str, bot_user_id: str, api_key_enc: bytes
    ) -> Account:
        """Create the company's account or replace its key (one account per company)."""
        row = await self.db._one(
            """
            INSERT INTO ticket_accounts (name, company_id, bot_user_id, api_key_enc)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (company_id) DO UPDATE SET
                name = EXCLUDED.name, bot_user_id = EXCLUDED.bot_user_id,
                api_key_enc = EXCLUDED.api_key_enc
            RETURNING *
            """,
            (name, company_id, bot_user_id, api_key_enc),
        )
        assert row is not None
        return _account(row)

    async def accounts(self) -> list[Account]:
        rows = await self.db._all("SELECT * FROM ticket_accounts ORDER BY id")
        return [_account(r) for r in rows]

    async def account(self, account_id: int) -> Account | None:
        row = await self.db._one("SELECT * FROM ticket_accounts WHERE id = %s", (account_id,))
        return _account(row) if row else None

    # ---------- senders ----------

    async def sender(self, tg_user_id: int) -> Sender | None:
        row = await self.db._one(
            "SELECT * FROM ticket_senders WHERE tg_user_id = %s", (tg_user_id,)
        )
        return _sender(row) if row else None

    async def request_access(self, tg_user_id: int, name: str, username: str) -> Sender:
        """Record (or renew) a request; an approved or blocked sender keeps their status."""
        row = await self.db._one(
            """
            INSERT INTO ticket_senders (tg_user_id, name, username) VALUES (%s, %s, %s)
            ON CONFLICT (tg_user_id) DO UPDATE SET
                name = EXCLUDED.name, username = EXCLUDED.username,
                status = CASE WHEN ticket_senders.status = 'rejected' THEN 'pending'
                              ELSE ticket_senders.status END
            RETURNING *
            """,
            (tg_user_id, name, username),
        )
        assert row is not None
        return _sender(row)

    async def decide(
        self,
        tg_user_id: int,
        status: str,
        *,
        by: int | None,
        account_id: int | None = None,
        project_id: str | None = None,
        project_name: str = "",
    ) -> Sender | None:
        """Approve (binding the sender to a project), reject or block."""
        row = await self.db._one(
            """
            UPDATE ticket_senders SET status = %s,
                account_id = COALESCE(%s, account_id),
                project_id = COALESCE(%s, project_id),
                project_name = CASE WHEN %s::text IS NULL THEN project_name ELSE %s END,
                decided_at = now(), decided_by = %s
            WHERE tg_user_id = %s RETURNING *
            """,
            (status, account_id, project_id, project_id, project_name, by, tg_user_id),
        )
        return _sender(row) if row else None

    async def senders(self) -> list[Sender]:
        rows = await self.db._all("SELECT * FROM ticket_senders ORDER BY created_at")
        return [_sender(r) for r in rows]

    # ---------- tickets ----------

    async def add_ticket(
        self,
        *,
        task_id: str,
        account_id: int,
        project_id: str,
        tg_user_id: int,
        number: str,
        title: str,
        column_id: str,
        last_message_id: int,
    ) -> Ticket:
        row = await self.db._one(
            """
            INSERT INTO tickets (task_id, account_id, project_id, tg_user_id, number, title,
                                 column_id, last_message_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (
                task_id,
                account_id,
                project_id,
                tg_user_id,
                number,
                title,
                column_id,
                last_message_id,
            ),
        )
        assert row is not None
        return _ticket(row)

    async def ticket(self, task_id: str) -> Ticket | None:
        row = await self.db._one("SELECT * FROM tickets WHERE task_id = %s", (task_id,))
        return _ticket(row) if row else None

    async def known(self, ids: list[str]) -> list[str]:
        """Those of ``ids`` that are tickets: a cheap filter for every YouGile event."""
        if not ids:
            return []
        rows = await self.db._all("SELECT task_id FROM tickets WHERE task_id = ANY(%s)", (ids,))
        return [r["task_id"] for r in rows]

    async def tickets_of(self, tg_user_id: int, limit: int = 15) -> list[Ticket]:
        rows = await self.db._all(
            "SELECT * FROM tickets WHERE tg_user_id = %s AND NOT deleted "
            "ORDER BY completed, created_at DESC LIMIT %s",
            (tg_user_id, limit),
        )
        return [_ticket(r) for r in rows]

    async def open_tickets(self, max_age_days: int = 90) -> list[Ticket]:
        rows = await self.db._all(
            "SELECT * FROM tickets WHERE NOT completed AND NOT deleted "
            "AND created_at > now() - make_interval(days => %s) ORDER BY created_at",
            (max_age_days,),
        )
        return [_ticket(r) for r in rows]

    async def update_ticket(self, task_id: str, **fields: Any) -> None:
        allowed = {"title", "column_id", "completed", "deleted", "last_message_id", "tg_thread_id"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"cannot update {sorted(set(fields) - allowed)}")
        sets = ", ".join(f"{k} = %s" for k in fields)
        await self.db._exec(
            f"UPDATE tickets SET {sets} WHERE task_id = %s",  # noqa: S608 - names are allowlisted
            (*fields.values(), task_id),
        )

    # ---------- Telegram messages and drafts ----------

    async def link_message(self, tg_chat_id: int, tg_message_id: int, task_id: str) -> None:
        await self.db._exec(
            "INSERT INTO ticket_tg_messages (tg_chat_id, tg_message_id, task_id) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (tg_chat_id, tg_message_id, task_id),
        )

    async def ticket_of_message(self, tg_chat_id: int, tg_message_id: int) -> str | None:
        row = await self.db._one(
            "SELECT task_id FROM ticket_tg_messages WHERE tg_chat_id = %s AND tg_message_id = %s",
            (tg_chat_id, tg_message_id),
        )
        return row["task_id"] if row else None

    async def ticket_of_thread(self, tg_user_id: int, thread: int) -> str | None:
        row = await self.db._one(
            "SELECT task_id FROM tickets WHERE tg_user_id = %s AND tg_thread_id = %s",
            (tg_user_id, thread),
        )
        return row["task_id"] if row else None

    async def draft(self, tg_user_id: int) -> dict[str, Any]:
        row = await self.db._one(
            "SELECT state FROM ticket_drafts WHERE tg_user_id = %s "
            "AND updated_at > now() - interval '2 days'",
            (tg_user_id,),
        )
        return dict(row["state"]) if row else {}

    async def save_draft(self, tg_user_id: int, state: dict[str, Any]) -> None:
        if not state:
            await self.db._exec("DELETE FROM ticket_drafts WHERE tg_user_id = %s", (tg_user_id,))
            return
        await self.db._exec(
            """
            INSERT INTO ticket_drafts (tg_user_id, state) VALUES (%s, %s)
            ON CONFLICT (tg_user_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()
            """,
            (tg_user_id, Jsonb(state)),
        )

    async def drop_stale_drafts(self) -> None:
        await self.db._exec(
            "DELETE FROM ticket_drafts WHERE updated_at < now() - interval '2 days'"
        )
