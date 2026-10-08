"""A small Telegram Bot API client: the few methods the ticket bot uses.

The token is part of every Bot API URL, so nothing here logs or raises with a URL in it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx2

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_DOWNLOAD = 20 * 1024 * 1024  # getFile serves files up to 20 MB


class TelegramError(Exception):
    def __init__(self, code: int, description: str) -> None:
        super().__init__(f"Telegram {code}: {description}")
        self.code = code
        self.description = description


class Telegram:
    def __init__(self, token: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self._http = httpx2.AsyncClient(timeout=httpx2.Timeout(70.0), transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, method: str, **kwargs: Any) -> httpx2.Response:
        try:
            return await self._http.post(f"{API}/bot{self._token}/{method}", **kwargs)
        except httpx2.TransportError as exc:
            raise TelegramError(0, f"network error in {method}: {type(exc).__name__}") from None

    async def call(self, method: str, **params: Any) -> Any:
        """Call a Bot API method; waits out a flood limit once."""
        body = {k: v for k, v in params.items() if v is not None}
        for attempt in (1, 2):
            resp = await self._post(method, json=body)
            data = _json(resp)
            if data.get("ok"):
                return data.get("result")
            code = int(data.get("error_code") or resp.status_code)
            retry = (data.get("parameters") or {}).get("retry_after")
            if code == 429 and retry and attempt == 1:
                await asyncio.sleep(min(float(retry), 30.0))
                continue
            raise TelegramError(code, str(data.get("description") or "error"))
        raise AssertionError("unreachable")

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        markup: dict | None = None,
        thread: int | None = None,
        reply_to: int | None = None,
    ) -> dict:
        """An HTML message, into a topic when ``thread`` is given, as a reply to ``reply_to``
        (which also keeps it in that message's topic); the caller escapes what came from
        people."""
        return await self.call(
            "sendMessage",
            chat_id=chat_id,
            message_thread_id=thread,
            text=text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            reply_markup=markup,
            reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
            if reply_to
            else None,
        )

    async def download(self, file_id: str) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        path = info.get("file_path")
        if not path:
            raise TelegramError(400, "the file is not available (too big?)")
        try:
            resp = await self._http.get(f"{API}/file/bot{self._token}/{path}")
        except httpx2.TransportError as exc:
            raise TelegramError(0, f"network error in download: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise TelegramError(resp.status_code, "cannot download the file")
        return resp.content

    async def updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        return await self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )


def _json(resp: httpx2.Response) -> dict:
    try:
        data = resp.json()
    except ValueError, json.JSONDecodeError:
        return {"ok": False, "error_code": resp.status_code, "description": "not JSON"}
    return data if isinstance(data, dict) else {"ok": False, "description": "unexpected answer"}


def inline(rows: list[list[tuple[str, str]]]) -> dict:
    """Inline keyboard from rows of (text, callback data)."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}
