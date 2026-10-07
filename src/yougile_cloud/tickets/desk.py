"""YouGile as the bot's account sees it: create tickets, read them, talk in their chats."""

from __future__ import annotations

import html
import mimetypes
import time
from dataclasses import dataclass
from typing import Any

from yougile_mcp.client import YouGileClient, YouGileError
from yougile_mcp.directory import fetch_all
from yougile_mcp.present import html_to_text
from yougile_mcp.ratelimit import NoopRateLimiter
from yougile_mcp.smart import FILE_MARK

from ..crypto import Secrets
from ..kv import KV, CompanyRateLimiter
from .store import Account

CACHE_TTL = 600.0
REQUESTS_BOARD = "заявки"  # a project takes tickets once it has a board with this name
SKIP_COLUMNS = {"документы"}  # never the column a new ticket lands in


@dataclass(frozen=True)
class Project:
    id: str
    title: str
    column_id: str  # where its new tickets land: «Очередь» on «Заявки», or its first column


@dataclass(frozen=True)
class Column:
    id: str
    title: str
    project_id: str | None


def text_html(text: str) -> str:
    """People's plain text as YouGile HTML: escaped, paragraphs kept."""
    paragraphs = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paragraphs)


def message_text(message: dict) -> str:
    return (message.get("text") or html_to_text(message.get("textHtml")) or "").strip()


def file_url(message: dict) -> str | None:
    """The file's URL path when the message is a file attached in the chat."""
    text = (message.get("text") or "").strip()
    return text[len(FILE_MARK) :] if text.startswith(FILE_MARK) else None


class Desk:
    def __init__(self, account: Account, client: YouGileClient, base_url: str) -> None:
        self.account = account
        self.client = client
        self.base_url = base_url.rstrip("/")
        self._columns: dict[str, tuple[float, Column]] = {}
        self._users: dict[str, tuple[float, str]] = {}
        self._projects: tuple[float, dict[str, Project]] | None = None

    async def projects(self, *, fresh: bool = False) -> dict[str, Project]:
        """Projects that take tickets (they have a «Заявки» board), by id."""
        if self._projects and not fresh and time.monotonic() - self._projects[0] < CACHE_TTL:
            return self._projects[1]
        projects = await fetch_all(self.client, "/projects")
        boards = await fetch_all(self.client, "/boards")
        columns = await fetch_all(self.client, "/columns")
        found: dict[str, Project] = {}
        for p in projects:
            if p.get("deleted"):
                continue
            board = next(
                (
                    b
                    for b in boards
                    if b.get("projectId") == p["id"]
                    and not b.get("deleted")
                    and (b.get("title") or "").strip().lower() == REQUESTS_BOARD
                ),
                None,
            )
            if board is None:
                continue
            own = [c for c in columns if c.get("boardId") == board["id"] and not c.get("deleted")]
            usable = [c for c in own if (c.get("title") or "").strip().lower() not in SKIP_COLUMNS]
            queue = next(
                (c for c in usable if c.get("title", "").strip().lower() == "очередь"), None
            )
            column = queue or (usable[0] if usable else None)
            if column:
                found[p["id"]] = Project(p["id"], p.get("title") or p["id"], column["id"])
        self._projects = (time.monotonic(), found)
        return found

    async def create_task(self, column_id: str, title: str, description_html: str) -> dict:
        created = await self.client.request(
            "POST",
            "/tasks",
            json={"title": title, "columnId": column_id, "description": description_html},
        )
        return await self.client.request("GET", f"/tasks/{created['id']}")

    async def task(self, task_id: str) -> dict:
        return await self.client.request("GET", f"/tasks/{task_id}")

    async def column(self, column_id: str) -> Column:
        cached = self._columns.get(column_id)
        if cached and time.monotonic() - cached[0] < CACHE_TTL:
            return cached[1]
        column = await self.client.request("GET", f"/columns/{column_id}")
        board = await self.client.request("GET", f"/boards/{column['boardId']}")
        found = Column(column_id, column.get("title") or "", board.get("projectId"))
        self._columns[column_id] = (time.monotonic(), found)
        return found

    async def user_name(self, user_id: str) -> str:
        cached = self._users.get(user_id)
        if cached and time.monotonic() - cached[0] < CACHE_TTL:
            return cached[1]
        try:
            user = await self.client.request("GET", f"/users/{user_id}")
            name = user.get("realName") or user.get("email") or "Команда"
        except YouGileError:
            name = "Команда"
        self._users[user_id] = (time.monotonic(), name)
        return name

    async def messages_since(self, chat_id: str, since: int) -> list[dict]:
        """Messages newer than ``since`` (a message id, which is its time), oldest first."""
        page = await self.client.request(
            "GET", f"/chats/{chat_id}/messages", query={"since": since, "limit": 100}
        )
        items = page.get("content", []) if isinstance(page, dict) else []
        return sorted((m for m in items if int(m.get("id") or 0) > since), key=lambda m: m["id"])

    async def post(self, chat_id: str, text: str) -> int:
        """Post plain text into the chat; returns the message id."""
        sent = await self.client.request(
            "POST",
            f"/chats/{chat_id}/messages",
            json={"text": text, "textHtml": text_html(text), "label": ""},
        )
        return int((sent or {}).get("id") or 0)

    async def attach(self, chat_id: str, filename: str, data: bytes) -> int:
        """Upload a file and post it into the chat, where YouGile shows it as attached."""
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        uploaded = await self.client.request(
            "POST", "/upload-file", files={"file": (filename, data, ctype)}
        )
        sent = await self.client.request(
            "POST",
            f"/chats/{chat_id}/messages",
            json={"text": FILE_MARK + uploaded["url"], "textHtml": "", "label": ""},
        )
        return int((sent or {}).get("id") or 0)

    def link(self, url: str) -> str:
        return url if url.startswith("http") else self.base_url + url


class Desks:
    """One Desk per account, sharing the company's rate limit with the MCP server."""

    def __init__(
        self,
        secrets: Secrets,
        base_url: str,
        *,
        kv: KV | None,
        rate_limit: int,
        transport: Any = None,
    ) -> None:
        self.secrets = secrets
        self.base_url = base_url
        self.kv = kv
        self.rate_limit = rate_limit
        self.transport = transport
        self._desks: dict[int, Desk] = {}

    def get(self, account: Account) -> Desk:
        desk = self._desks.get(account.id)
        if desk is None or desk.account != account:
            limiter = (
                CompanyRateLimiter(self.kv, account.company_id, self.rate_limit)
                if self.kv
                else NoopRateLimiter()
            )
            client = YouGileClient(
                self.secrets.decrypt(account.api_key_enc),
                self.base_url,
                limiter=limiter,
                transport=self.transport,
            )
            desk = self._desks[account.id] = Desk(account, client, self.base_url)
        return desk

    async def close(self) -> None:
        for desk in self._desks.values():
            await desk.client.aclose()
        self._desks.clear()
