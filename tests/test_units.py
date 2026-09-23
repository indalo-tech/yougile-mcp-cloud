from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import DB_URL, VALKEY_URL
from cryptography.fernet import Fernet
from yougile_mcp.client import YouGileError
from yougile_mcp.directory import Structure

from yougile_cloud.access import access_of
from yougile_cloud.crypto import Secrets
from yougile_cloud.db import Company, Database, Rights, User
from yougile_cloud.kv import KV, CompanyRateLimiter
from yougile_cloud.permissions import (
    all_write_actions,
    denied_keys,
    deny_list,
    format_workflows,
    parse_workflows,
    restrictions,
)
from yougile_cloud.settings import Settings, SettingsError
from yougile_cloud.tenancy import Tenancy, workspace_config

NOW = datetime(2026, 9, 23, tzinfo=UTC)


def company(**kw) -> Company:  # noqa: ANN003
    base = dict(
        id="c",
        name="C",
        status="trial",
        trial_ends_at=None,
        paid_until=None,
        settings={},
        settings_version=1,
    )
    return Company(**{**base, **kw})


def test_access_states():
    free = frozenset({"c-free"})
    assert access_of(company(status="blocked"), free, NOW).state == "blocked"
    assert access_of(company(id="c-free"), free, NOW).state == "exempt"
    assert access_of(company(status="exempt"), free, NOW).allowed
    assert access_of(company(paid_until=NOW + timedelta(days=1)), free, NOW).state == "active"
    assert access_of(company(trial_ends_at=NOW + timedelta(days=1)), free, NOW).state == "trial"
    expired = access_of(company(trial_ends_at=NOW - timedelta(days=1)), free, NOW)
    assert not expired.allowed and "Пробный период" in expired.message()


def test_workspace_config_merges_company_and_user():
    c = company(
        settings={
            "timezone": "Asia/Yerevan",
            "confirm_projects": ["Клиенты"],
            "default_role": "reader",
            "deny": ["users.*"],
        }
    )
    cfg = workspace_config(c, None)
    assert (cfg.role, cfg.timezone, cfg.confirm_projects, cfg.deny) == (
        "reader",
        "Asia/Yerevan",
        ["Клиенты"],
        ["users.*"],
    )
    cfg = workspace_config(c, Rights(role="member", projects=["Разработка"], deny=["tasks.delete"]))
    assert (cfg.role, cfg.projects, cfg.deny) == (
        "member",
        ["Разработка"],
        ["users.*", "tasks.delete"],
    )
    broken = workspace_config(company(settings={"timezone": "Mars/Base"}), None)
    assert broken.role == "reader", "a bad setting falls back to read-only, never to admin"


async def test_runtime_sends_people_to_the_admin_page(settings):
    secrets = Secrets(settings.encryption_keys, settings.jwt_secret)
    tenancy = Tenancy(settings, None, KV(None), secrets)  # type: ignore[arg-type]
    user = User(
        id=1,
        company_id="c",
        yougile_user_id="u",
        email="",
        name="",
        is_admin=False,
        api_key_enc=secrets.encrypt("key"),
        updated_at=NOW,
    )
    rt = tenancy._build(user, company(), None)
    try:
        assert f"{settings.public_url}/admin" in rt.settings_hint
        assert rt.allow_local_files is False
    finally:
        await rt.client.aclose()


def test_restrictions_cover_every_write():
    keys = [r.key for r in restrictions()]
    assert len(keys) == len(set(keys))
    assert {a for r in restrictions() for a in r.actions} == all_write_actions(), (
        "every write the core can do must be switchable off on the admin page"
    )
    assert denied_keys(["tasks.delete"]) == {"task_delete"}
    masked = denied_keys(["yougile_users.*"])
    assert "people" in masked and "task_delete" not in masked
    assert deny_list(["task_delete", "no-such-key"]) == ["tasks.delete"]


def test_workflows_are_checked_against_boards():
    s = Structure(
        projects={"p": {"id": "p", "title": "Клиенты"}},
        boards={"b": {"id": "b", "title": "Сайт", "projectId": "p"}},
        columns={
            "k1": {"id": "k1", "title": "Очередь", "boardId": "b"},
            "k2": {"id": "k2", "title": "В работе", "boardId": "b"},
        },
    )
    chains, errors = parse_workflows("  клиенты / сайт: очередь → в работе\n\n", s)
    assert chains == {"Клиенты / Сайт": ["Очередь", "В работе"]} and errors == []
    assert format_workflows(chains) == "Клиенты / Сайт: Очередь → В работе"
    _, errors = parse_workflows(
        "Сайт: Очередь\nЛевая: А -> Б\nСайт: Очередь -> Нет\n"
        "Сайт: Очередь -> В работе\nСайт: В работе -> Очередь",
        s,
    )
    assert [err.split(":")[0] for err in errors] == ["Строка 1", "Строка 2", "Строка 3", "Строка 5"]


def test_settings_validation():
    env = {
        "PUBLIC_URL": "https://y.example.com",
        "DATABASE_URL": "postgresql://x",
        "VALKEY_URL": "valkey://v",
        "ENCRYPTION_KEYS": Fernet.generate_key().decode(),
        "JWT_SECRET": "s" * 40,
        "FREE_COMPANY_IDS": "a, b",
    }
    s = Settings.from_env(env)
    assert s.mcp_url == "https://y.example.com/mcp" and s.free_company_ids == {"a", "b"}
    with pytest.raises(SettingsError, match="https"):
        Settings.from_env({**env, "PUBLIC_URL": "http://y.example.com"})
    with pytest.raises(SettingsError, match="32"):
        Settings.from_env({**env, "JWT_SECRET": "short"})


def test_secrets_roundtrip_and_rotation():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    blob = Secrets([old], "j" * 40).encrypt("api-key")
    rotated_secrets = Secrets([new, old], "j" * 40)
    assert rotated_secrets.decrypt(blob) == "api-key"
    assert Secrets([new], "j" * 40).decrypt(rotated_secrets.rotate(blob)) == "api-key"
    s = Secrets([old], "j" * 40)
    assert s.csrf_ok("flow1", s.csrf_token("flow1")) and not s.csrf_ok(
        "flow2", s.csrf_token("flow1")
    )


async def test_rate_limit_is_shared_per_company(clean):
    kv1, kv2 = await KV.connect(VALKEY_URL), await KV.connect(VALKEY_URL)
    try:
        a = CompanyRateLimiter(kv1, "c1", limit=3)
        b = CompanyRateLimiter(kv2, "c1", limit=3)
        other = CompanyRateLimiter(kv2, "c2", limit=3)
        for limiter in (a, b, a):
            assert await limiter.kv.acquire_slot(limiter.bucket, 3) == 0
        assert await b.kv.acquire_slot(b.bucket, 3) > 50, "4th request in a minute must wait"
        assert await other.kv.acquire_slot(other.bucket, 3) == 0
        await a.penalize(30)
        assert await b.kv.acquire_slot(b.bucket, 3) > 25
        b.MAX_WAIT = 1.0  # the 30 s penalty is longer than we are willing to wait
        with pytest.raises(YouGileError) as err:
            await b.acquire()
        assert err.value.status == 429
    finally:
        await kv1.close()
        await kv2.close()


async def test_one_time_values(clean):
    kv = await KV.connect(VALKEY_URL)
    try:
        key = kv.key("code", "abc")
        await kv.put_json(key, {"a": 1}, 60)
        assert await kv.take_json(key) == {"a": 1}
        assert await kv.take_json(key) is None
        assert [await kv.hit(kv.key("try", "x"), 60) for _ in range(3)] == [1, 2, 3]
    finally:
        await kv.close()


async def test_database_basics(clean):
    db = Database(DB_URL, max_size=2)
    await db.open()
    try:
        assert await db.migrate() == [], "migrations are idempotent"
        c = await db.ensure_company("c1", "One", trial_days=14, exempt=False)
        assert c.status == "trial" and c.trial_ends_at > datetime.now(UTC) + timedelta(days=13)
        c = await db.ensure_company("c1", "One renamed", trial_days=1, exempt=True)
        assert c.name == "One renamed" and c.status == "trial", "re-login does not reset access"
        user, previous = await db.upsert_user(
            company_id="c1",
            yougile_user_id="u1",
            email="e",
            name="n",
            is_admin=False,
            api_key_enc=b"k1",
        )
        assert previous is None
        user2, previous = await db.upsert_user(
            company_id="c1",
            yougile_user_id="u1",
            email="e",
            name="n",
            is_admin=True,
            api_key_enc=b"k2",
        )
        assert user2.id == user.id and previous == b"k1" and user2.is_admin
        await db.set_rights("c1", "u1", role="reader", projects=None, deny=["users.*"])
        rights = await db.get_rights("c1", "u1")
        assert (rights.role, rights.projects, rights.deny) == ("reader", None, ["users.*"])
    finally:
        await db.close()
