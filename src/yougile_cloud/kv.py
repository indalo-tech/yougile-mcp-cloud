"""Valkey: short-lived OAuth state, login attempt counters and the per-company rate limit."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from glide import (
    ExpirySet,
    ExpiryType,
    GlideClient,
    GlideClientConfiguration,
    NodeAddress,
    Script,
    ServerCredentials,
)
from yougile_mcp import progress
from yougile_mcp.client import YouGileError

# Sliding window per bucket. Uses the server clock so every worker agrees on time.
# Returns "0" when the request may go, otherwise the seconds to wait.
_ACQUIRE = Script(
    """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local blocked = tonumber(redis.call('GET', key .. ':block') or '0')
if blocked > now then return tostring(blocked - now) end
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
if redis.call('ZCARD', key) < limit then
  redis.call('ZADD', key, now, ARGV[3])
  redis.call('EXPIRE', key, math.ceil(window) + 1)
  return '0'
end
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
return tostring(tonumber(oldest[2]) + window - now + 0.05)
"""
)

_PENALIZE = Script(
    """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local until_ts = now + tonumber(ARGV[1])
if until_ts > current then
  redis.call('SET', KEYS[1], tostring(until_ts), 'EX', math.ceil(tonumber(ARGV[1])) + 1)
end
return 1
"""
)


def config_from_url(url: str) -> GlideClientConfiguration:
    """``valkey://[user:password@]host:port[/db]`` (``valkeys://`` for TLS)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("valkey", "valkeys", "redis", "rediss"):
        raise ValueError("VALKEY_URL must start with valkey:// or valkeys://")
    credentials = (
        ServerCredentials(password=parsed.password, username=parsed.username or None)
        if parsed.password
        else None
    )
    db = int(parsed.path.lstrip("/") or 0)
    return GlideClientConfiguration(
        addresses=[NodeAddress(parsed.hostname or "localhost", parsed.port or 6379)],
        use_tls=parsed.scheme in ("valkeys", "rediss"),
        credentials=credentials,
        database_id=db or None,
        client_name="yougile-cloud",
        request_timeout=2000,
    )


class KV:
    def __init__(self, client: GlideClient, prefix: str = "yc:") -> None:
        self.client = client
        self.prefix = prefix

    @classmethod
    async def connect(cls, url: str, prefix: str = "yc:") -> KV:
        return cls(await GlideClient.create(config_from_url(url)), prefix)

    async def close(self) -> None:
        await self.client.close()

    def key(self, *parts: str) -> str:
        return self.prefix + ":".join(parts)

    async def put_json(self, key: str, value: Any, ttl: int) -> None:
        await self.client.set(
            key, json.dumps(value, ensure_ascii=False), expiry=ExpirySet(ExpiryType.SEC, ttl)
        )

    async def get_json(self, key: str) -> Any:
        raw = await self.client.get(key)
        return json.loads(raw) if raw else None

    async def take_json(self, key: str) -> Any:
        """Read and delete in one step: one-time values (auth codes) cannot be replayed."""
        raw = await self.client.getdel(key)
        return json.loads(raw) if raw else None

    async def put_bytes(self, key: str, value: bytes, ttl: int) -> None:
        await self.client.set(key, value, expiry=ExpirySet(ExpiryType.SEC, ttl))

    async def get_bytes(self, key: str) -> bytes | None:
        return await self.client.get(key)

    async def delete(self, *keys: str) -> None:
        if keys:
            await self.client.delete(list(keys))

    async def hit(self, key: str, window: int) -> int:
        """Count an event in a fixed window; returns the count including this one."""
        count = await self.client.incr(key)
        if count == 1:
            await self.client.expire(key, window)
        return count

    async def acquire_slot(self, bucket: str, limit: int, window: float = 60.0) -> float:
        result = await self.client.invoke_script(
            _ACQUIRE, keys=[bucket], args=[str(window), str(limit), uuid.uuid4().hex]
        )
        return float(result.decode() if isinstance(result, bytes) else result)

    async def penalize(self, bucket: str, seconds: float) -> None:
        await self.client.invoke_script(_PENALIZE, keys=[bucket + ":block"], args=[str(seconds)])


class CompanyRateLimiter:
    """yougile_mcp RateLimiter shared by every worker and user of one YouGile company."""

    MAX_SLEEP = 5.0
    MAX_WAIT = 90.0  # give up (as HTTP 429) rather than hang a tool call for minutes

    def __init__(self, kv: KV, company_id: str, limit: int, window: float = 60.0) -> None:
        self.kv = kv
        self.bucket = kv.key("rl", company_id)
        self.limit = limit
        self.window = window

    async def acquire(self) -> None:
        deadline = time.monotonic() + self.MAX_WAIT
        while True:
            wait = await self.kv.acquire_slot(self.bucket, self.limit, self.window)
            if wait <= 0:
                return
            if time.monotonic() + wait > deadline:
                raise YouGileError(
                    429, "the company's YouGile request budget is exhausted; retry in a minute"
                )
            await progress.report(progress.rate_limit_note(wait))
            await asyncio.sleep(min(wait, self.MAX_SLEEP))

    async def penalize(self, seconds: float) -> None:
        await self.kv.penalize(self.bucket, seconds)
