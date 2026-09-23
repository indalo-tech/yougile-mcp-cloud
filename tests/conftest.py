"""Integration fixtures: real Postgres and Valkey (see README for the test containers),
a fake YouGile behind an httpx2 MockTransport, and the real app served by uvicorn."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets as pysecrets
import socket
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
import uvicorn
from cryptography.fernet import Fernet

from yougile_cloud.app import create_server
from yougile_cloud.db import Database
from yougile_cloud.kv import KV
from yougile_cloud.settings import Settings

DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:test@127.0.0.1:55432/ygc")
VALKEY_URL = os.environ.get("TEST_VALKEY_URL", "valkey://127.0.0.1:56379/0")
FERNET_KEY = Fernet.generate_key().decode()


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


SERVICES_UP = "TEST_DATABASE_URL" in os.environ or (
    _reachable("127.0.0.1", 55432) and _reachable("127.0.0.1", 56379)
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeYouGile:
    """YouGile auth + a tiny company, enough for sign-in and a few tool calls."""

    def __init__(self) -> None:
        self.accounts = {
            "anna@example.com": {
                "password": "right",
                "memberships": {"c-main": "u-anna", "c-free": "u-anna-free", "c-two": "u-anna-two"},
            },
            "bob@example.com": {"password": "pw", "memberships": {"c-main": "u-bob"}},
            "solo@example.com": {"password": "pw", "memberships": {"c-solo": "u-solo"}},
        }
        self.company_names = {
            "c-main": "Main Co",
            "c-free": "Own Co",
            "c-solo": "Solo Co",
            "c-two": "Two Co",
        }
        self.admins = {"u-anna": True, "u-anna-two": True}  # tests may demote someone
        self.names = {"u-bob": "Bob <img src=x onerror=alert(1)>"}
        self.keys: dict[str, tuple[str, str]] = {}  # key -> (company, user)
        self.deleted: list[str] = []
        self.calls: list[tuple[str, str]] = []

    def _user(self, user: str) -> dict:
        return {
            "id": user,
            "email": f"{user}@example.com",
            "realName": self.names.get(user, user),
            "isAdmin": self.admins.get(user, False),
        }

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.removeprefix("/api-v2")
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.content else {}
        if path == "/auth/companies" and request.method == "POST":
            account = self.accounts.get(body.get("login", ""))
            if not account or account["password"] != body.get("password"):
                return httpx2.Response(401, json={"error": "Unauthorized"})
            content = [
                {"id": cid, "name": self.company_names[cid], "isAdmin": self.admins.get(uid, False)}
                for cid, uid in account["memberships"].items()
            ]
            return httpx2.Response(200, json={"paging": {"next": False}, "content": content})
        if path == "/auth/keys" and request.method == "POST":
            account = self.accounts.get(body.get("login", ""))
            if not account or account["password"] != body.get("password"):
                return httpx2.Response(401, json={"error": "Unauthorized"})
            user_id = account["memberships"][body["companyId"]]
            key = pysecrets.token_hex(16)
            self.keys[key] = (body["companyId"], user_id)
            return httpx2.Response(201, json={"key": key})
        if path.startswith("/auth/keys/") and request.method == "DELETE":
            key = path.rsplit("/", 1)[1]
            self.keys.pop(key, None)
            self.deleted.append(key)
            return httpx2.Response(200, json={})
        auth = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if auth not in self.keys:
            return httpx2.Response(401, json={"error": "Unauthorized"})
        company, user = self.keys[auth]
        if path == "/users/me":
            return httpx2.Response(200, json=self._user(user))
        members = [
            uid
            for account in self.accounts.values()
            for cid, uid in account["memberships"].items()
            if cid == company
        ]
        lists = {
            "/projects": [{"id": "p1", "title": "Проект"}],
            "/boards": [{"id": "b1", "title": "Доска", "projectId": "p1"}],
            "/columns": [
                {"id": "k1", "title": "Очередь", "boardId": "b1"},
                {"id": "k2", "title": "Готово", "boardId": "b1"},
            ],
            "/users": [self._user(uid) for uid in members],
        }
        if path in lists:
            return httpx2.Response(200, json={"paging": {"next": False}, "content": lists[path]})
        return httpx2.Response(404, json={"error": f"fake: {request.method} {path}"})


@pytest.fixture
def fake() -> FakeYouGile:
    return FakeYouGile()


@pytest.fixture
def settings() -> Settings:
    port = free_port()
    return Settings(
        public_url=f"http://127.0.0.1:{port}",
        database_url=DB_URL,
        valkey_url=VALKEY_URL,
        encryption_keys=[FERNET_KEY],
        jwt_secret="t" * 48,
        free_company_ids=frozenset({"c-free"}),
        trial_days=14,
        host="127.0.0.1",
        port=port,
    )


@pytest.fixture
async def clean() -> None:
    if not SERVICES_UP:
        pytest.skip("test Postgres/Valkey are not running")
    db = Database(DB_URL, max_size=2)
    await db.open()
    await db.migrate()
    await db._exec(
        "TRUNCATE audit_log, refresh_tokens, oauth_clients, user_rights, users, companies CASCADE"
    )
    await db.close()
    kv = await KV.connect(VALKEY_URL)
    await kv.client.custom_command(["FLUSHDB"])
    await kv.close()


@pytest.fixture
async def server(settings, fake, clean):
    """The real ASGI app on a real port, talking to the fake YouGile."""
    mcp, services = create_server(settings, transport=httpx2.MockTransport(fake))
    app = mcp.http_app(path="/mcp")
    config = uvicorn.Config(app, host="127.0.0.1", port=settings.port, log_level="warning")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    for _ in range(100):
        if srv.started:
            break
        await asyncio.sleep(0.05)
    try:
        yield services
    finally:
        srv.should_exit = True
        await task


@pytest.fixture
async def http(settings):
    async with httpx2.AsyncClient(base_url=settings.public_url, follow_redirects=False) as client:
        yield client


def pkce() -> tuple[str, str]:
    verifier = pysecrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    return verifier, challenge.rstrip("=")


def field(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]*)"', html)
    assert match, f"no hidden field {name}"
    return match.group(1)


REDIRECT = "http://localhost:9999/callback"


async def register_client(http: httpx2.AsyncClient) -> str:
    resp = await http.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT],
            "client_name": '<script>alert("x")</script>',
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["client_id"]


async def start_flow(
    http: httpx2.AsyncClient, settings: Settings, client_id: str
) -> tuple[str, str]:
    verifier, challenge = pkce()
    resp = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": settings.mcp_url,
        },
    )
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    assert location.startswith(f"{settings.public_url}/signin?flow=")
    return location.removeprefix(settings.public_url), verifier


async def sign_in(
    http: httpx2.AsyncClient,
    settings: Settings,
    signin_path: str,
    *,
    login: str,
    password: str,
    company: str | None = None,
) -> httpx2.Response:
    page = await http.get(signin_path)
    assert page.status_code == 200
    origin = {"Origin": settings.public_url}
    form = {
        "flow": field(page.text, "flow"),
        "csrf": field(page.text, "csrf"),
        "login": login,
        "password": password,
    }
    resp = await http.post("/signin", data=form, headers=origin)
    if company is None or resp.status_code != 200 or "Выберите компанию" not in resp.text:
        return resp
    return await http.post(
        "/signin/company",
        data={
            "flow": field(resp.text, "flow"),
            "csrf": field(resp.text, "csrf"),
            "company": company,
        },
        headers=origin,
    )


async def exchange(
    http: httpx2.AsyncClient, client_id: str, code: str, verifier: str
) -> dict[str, Any]:
    resp = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def connect(
    http: httpx2.AsyncClient,
    settings: Settings,
    *,
    login: str,
    password: str,
    company: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """The whole OAuth dance of an AI client; returns its client_id and tokens."""
    client_id = await register_client(http)
    signin_path, verifier = await start_flow(http, settings, client_id)
    resp = await sign_in(
        http, settings, signin_path, login=login, password=password, company=company
    )
    assert resp.status_code == 302, resp.text
    code = parse_qs(urlparse(resp.headers["location"]).query)["code"][0]
    return client_id, await exchange(http, client_id, code, verifier)


async def admin_login(
    http: httpx2.AsyncClient,
    settings: Settings,
    *,
    login: str = "anna@example.com",
    password: str = "right",
    company: str | None = "c-main",
) -> httpx2.Response:
    page = await http.get("/admin/login")
    assert page.status_code == 200, page.text
    origin = {"Origin": settings.public_url}
    resp = await http.post(
        "/admin/login",
        data={"csrf": field(page.text, "csrf"), "login": login, "password": password},
        headers=origin,
    )
    if company is None or resp.status_code != 200 or "Выберите компанию" not in resp.text:
        return resp
    return await http.post(
        "/admin/login/company",
        data={"csrf": field(resp.text, "csrf"), "company": company},
        headers=origin,
    )
