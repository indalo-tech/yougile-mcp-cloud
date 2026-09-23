"""Company admin pages: a YouGile admin sets the company's rules and employees' MCP rights.

Sign-in is the YouGile login, limited to companies where the person is an admin; like
connecting an AI client, it issues the person's own key. The session is an opaque cookie
backed by Valkey, and every request asks YouGile whether the person is still an admin.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from yougile_mcp.client import YouGileClient, YouGileError
from yougile_mcp.config import DEFAULT_TIMEZONE, ConfigError, WorkspaceConfig
from yougile_mcp.directory import Directory, fetch_all

from . import admin_pages as ui
from . import pages
from .access import Access, access_of
from .crypto import Secrets, new_token, token_hash
from .db import Company, Database, User
from .kv import KV, CompanyRateLimiter
from .permissions import denied_keys, deny_list, format_workflows, parse_workflows
from .settings import Settings
from .signin import SignIn, TooManyAttempts
from .tenancy import Tenancy
from .web import CRED_TTL, SECURITY_HEADERS, html, same_origin
from .yougile_auth import BadCredentials, KeyLimitReached, YouGileAuth, YouGileCompany

log = logging.getLogger(__name__)

HEADERS = {
    **SECURITY_HEADERS,
    "Content-Security-Policy": SECURITY_HEADERS["Content-Security-Policy"] + "; form-action 'self'",
}
LOGIN_TTL = 15 * 60
MAX_INSTRUCTIONS = 4000
LEAD = "Для администраторов компании в YouGile: права сотрудников и правила для ассистента."


@dataclass(frozen=True)
class Session:
    key: str  # Valkey key of the session
    user: User
    company: Company
    access: Access


@dataclass(frozen=True)
class Ctx:
    session: Session
    client: YouGileClient  # the admin's own key, under the company's rate limit
    form: FormData | None
    frame: ui.Frame


Handler = Callable[[Request, Ctx], Awaitable[Response]]


def _projects(items: Iterable[dict]) -> list[tuple[str, str]]:
    live = [(p["id"], p.get("title") or p["id"]) for p in items if not p.get("deleted")]
    return sorted(live, key=lambda p: p[1].casefold())


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _project_ids(refs: Iterable[str], projects: list[tuple[str, str]]) -> set[str]:
    """Stored refs may be ids (from this page) or titles (typed by hand): both count."""
    by_title = {_norm(title): pid for pid, title in projects}
    ids = {pid for pid, _ in projects}
    return {ref if ref in ids else by_title.get(_norm(ref), ref) for ref in refs}


def _column_titles(columns: Iterable[dict]) -> list[str]:
    """Distinct column titles of the company (case-insensitively), sorted."""
    titles: dict[str, str] = {}
    for c in columns:
        if c.get("title") and not c.get("deleted"):
            titles.setdefault(_norm(c["title"]), c["title"])
    return sorted(titles.values(), key=str.casefold)


def _pick_titles(refs: Iterable[str], titles: list[str]) -> set[str]:
    """The known titles among ``refs``, whatever their case or spacing."""
    wanted = {_norm(ref) for ref in refs}
    return {title for title in titles if _norm(title) in wanted}


def _choice(value: Any, allowed: Iterable[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})


class AdminPages:
    def __init__(self, settings: Settings, secrets: Secrets) -> None:
        self.settings, self.secrets = settings, secrets
        self.secure = settings.public_url.startswith("https://")
        # __Host- cookies: https only, no Domain, Path=/ — nobody else can set or scope them.
        prefix = "__Host-" if self.secure else ""
        self.session_cookie = f"{prefix}ygc-admin"
        self.login_cookie = f"{prefix}ygc-admin-login"
        self.db: Database | None = None  # attached on startup
        self.kv: KV | None = None
        self.signin: SignIn | None = None
        self.auth: YouGileAuth | None = None
        self.tenancy: Tenancy | None = None
        self.transport: Any = None

    def attach(
        self,
        db: Database,
        kv: KV,
        signin: SignIn,
        auth: YouGileAuth,
        tenancy: Tenancy,
        transport: Any = None,
    ) -> None:
        self.db, self.kv, self.signin, self.auth = db, kv, signin, auth
        self.tenancy, self.transport = tenancy, transport

    def routes(self) -> list[tuple[str, str, Callable[[Request], Awaitable[Response]]]]:
        return [
            ("/admin", "GET", self.users),
            ("/admin/login", "GET", self.login_form),
            ("/admin/login", "POST", self.login),
            ("/admin/login/company", "POST", self.choose_company),
            ("/admin/logout", "POST", self.logout),
            ("/admin/settings", "GET", self.settings_form),
            ("/admin/settings", "POST", self.save_settings),
            ("/admin/users/{user_id}", "GET", self.user_form),
            ("/admin/users/{user_id}", "POST", self.save_user),
            ("/admin/users/{user_id}/reset", "POST", self.reset_user),
            ("/admin/users/{user_id}/disconnect", "POST", self.disconnect_user),
        ]

    # ---------- plumbing ----------

    def _html(self, body: str, status: int = 200) -> Response:
        return html(body, status, HEADERS)

    def _message(self, title: str, text: str, status: int, *, relogin: bool = False) -> Response:
        link = ("/admin/login", "Войти") if relogin else None
        return self._html(pages.message_page(title, text, link=link), status)

    def _set_cookie(self, response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            path="/",
            secure=self.secure,
            httponly=True,
            samesite="lax",
        )

    def _drop_cookie(self, response: Response, name: str) -> None:
        response.delete_cookie(name, path="/", secure=self.secure, httponly=True, samesite="lax")

    def _csrf(self, session: Session) -> str:
        return self.secrets.csrf_token("admin:" + session.key)

    def _client(self, user: User) -> YouGileClient:
        return YouGileClient(
            self.secrets.decrypt(user.api_key_enc),
            self.settings.yougile_base_url,
            limiter=CompanyRateLimiter(self.kv, user.company_id, self.settings.rate_limit),
            transport=self.transport,
        )

    async def _session(self, request: Request) -> Session | None:
        token = request.cookies.get(self.session_cookie)
        if not token:
            return None
        key = self.kv.key("admin", token_hash(token))
        data = await self.kv.get_json(key)
        if not data:
            return None
        user = await self.db.get_user(int(data["user_id"]))
        if user is None or user.company_id != data["company_id"] or not user.is_admin:
            await self.kv.delete(key)
            return None
        company = await self.db.get_company(user.company_id)
        assert company is not None
        return Session(key, user, company, access_of(company, self.settings.free_company_ids))

    def _frame(self, session: Session) -> ui.Frame:
        s = session.company.settings
        return ui.Frame(
            company=session.company,
            access=session.access,
            admin=session.user,
            csrf=self._csrf(session),
            tz=ZoneInfo(_choice(s.get("timezone"), ui.timezones(), DEFAULT_TIMEZONE)),
            default_role=_choice(s.get("default_role"), ui.ROLES, "member"),
            company_denied=denied_keys(s.get("deny", [])),
        )

    async def _guarded(self, request: Request, handler: Handler, *, post: bool = False) -> Response:
        """Session, CSRF and Origin for forms, company access, and "still an admin in YouGile"."""
        session = await self._session(request)
        if session is None:
            return _redirect("/admin/login")
        form = None
        if post:
            form = await request.form()
            if not same_origin(request, self.settings.public_url) or not self.secrets.csrf_ok(
                "admin:" + session.key, str(form.get("csrf", ""))
            ):
                return self._message("Ошибка формы", "Обновите страницу и попробуйте снова.", 403)
        if not session.access.allowed:
            return self._message("Доступ закрыт", session.access.message(), 402)
        try:
            client = self._client(session.user)
        except ValueError:  # the stored key cannot be decrypted (keys were rotated away)
            await self.kv.delete(session.key)
            return self._message("Войдите снова", "Сессия устарела.", 401, relogin=True)
        async with client:
            try:
                me = await client.request("GET", "/users/me")
                if not (
                    isinstance(me, dict)
                    and me.get("isAdmin")
                    and me.get("id") == session.user.yougile_user_id
                ):
                    await self.kv.delete(session.key)
                    return self._message(
                        "Нет прав администратора",
                        "В YouGile у вас больше нет прав администратора этой компании.",
                        403,
                    )
                return await handler(request, Ctx(session, client, form, self._frame(session)))
            except YouGileError as exc:
                if exc.status in (401, 403):
                    await self.kv.delete(session.key)
                    return self._message(
                        "Войдите снова", "YouGile больше не принимает ваш ключ.", 401, relogin=True
                    )
                log.warning("admin page: %s", exc)
                return self._message("YouGile не ответил", "Попробуйте через минуту.", 502)

    async def _people(self, company_id: str, yougile_users: list[dict]) -> list[ui.Person]:
        connected = {u.yougile_user_id: u for u in await self.db.list_users(company_id)}
        rights = await self.db.list_rights(company_id)
        people = [
            ui.Person(
                id=u["id"],
                name=u.get("realName") or u.get("email") or u["id"],
                email=u.get("email") or "",
                yougile_admin=bool(u.get("isAdmin")),
                in_yougile=True,
                user=connected.get(u["id"]),
                rights=rights.get(u["id"]),
            )
            for u in yougile_users
        ]
        seen = {p.id for p in people}
        people += [
            ui.Person(
                id=u.yougile_user_id,
                name=u.name or u.email or u.yougile_user_id,
                email=u.email,
                yougile_admin=u.is_admin,
                in_yougile=False,
                user=u,
                rights=rights.get(u.yougile_user_id),
            )
            for u in connected.values()
            if u.yougile_user_id not in seen
        ]
        return sorted(people, key=lambda p: (p.user is None, p.name.casefold()))

    async def _person(self, ctx: Ctx, person_id: str) -> ui.Person | None:
        people = await self._people(ctx.session.company.id, await fetch_all(ctx.client, "/users"))
        return next((p for p in people if p.id == person_id), None)

    # ---------- sign-in ----------

    def _login_page(
        self, nonce: str, *, error: str | None = None, login: str = "", status: int = 200
    ) -> Response:
        return self._html(
            pages.login_page(
                action="/admin/login",
                hidden={"csrf": self.secrets.csrf_token("adminlogin:" + nonce)},
                heading="Управление YouGile MCP",
                lead=LEAD,
                error=error,
                login=login,
            ),
            status,
        )

    def _login_ok(self, request: Request, form: FormData, purpose: str) -> str | None:
        """The login nonce cookie, if the form came from our page in this browser."""
        nonce = request.cookies.get(self.login_cookie, "")
        if (
            nonce
            and same_origin(request, self.settings.public_url)
            and self.secrets.csrf_ok(f"{purpose}:{nonce}", str(form.get("csrf", "")))
        ):
            return nonce
        return None

    async def login_form(self, request: Request) -> Response:
        if await self._session(request):
            return _redirect("/admin")
        nonce = request.cookies.get(self.login_cookie) or new_token(24)
        response = self._login_page(nonce)
        self._set_cookie(response, self.login_cookie, nonce, LOGIN_TTL)
        return response

    async def login(self, request: Request) -> Response:
        form = await request.form()
        nonce = self._login_ok(request, form, "adminlogin")
        if nonce is None:
            return self._message(
                "Ошибка формы", "Откройте страницу входа заново.", 403, relogin=True
            )
        login = str(form.get("login", "")).strip()
        password = str(form.get("password", ""))
        if not login or not password:
            return self._login_page(nonce, error="Введите логин и пароль.", login=login, status=400)
        try:
            await self.signin.count_attempt(request.client.host if request.client else "?", login)
            companies = await self.signin.companies(login, password)
        except TooManyAttempts:
            return self._login_page(
                nonce, error="Слишком много попыток. Подождите 15 минут.", login=login, status=429
            )
        except BadCredentials:
            return self._login_page(
                nonce, error="Неверный логин или пароль YouGile.", login=login, status=401
            )
        admin_of = [c for c in companies if c.is_admin]
        if not admin_of:
            return self._login_page(
                nonce,
                error="Вы не администратор ни в одной компании YouGile. Подключить ассистента "
                "можно прямо в AI-клиенте, эта страница — для администраторов.",
                login=login,
                status=403,
            )
        if len(admin_of) == 1:
            return await self._finish(nonce, login, password, admin_of[0])
        blob = json.dumps(
            {
                "login": login,
                "password": password,
                "companies": [[c.id, c.name, c.is_admin] for c in admin_of],
            }
        )
        await self.kv.put_bytes(
            self.kv.key("admincred", token_hash(nonce)), self.secrets.encrypt(blob), CRED_TTL
        )
        return self._html(
            pages.company_page(
                action="/admin/login/company",
                hidden={"csrf": self.secrets.csrf_token("admincompany:" + nonce)},
                companies=[(c.id, c.name) for c in admin_of],
                lead="Вы администратор в нескольких компаниях YouGile. Выберите, какой управлять.",
            )
        )

    async def choose_company(self, request: Request) -> Response:
        form = await request.form()
        nonce = self._login_ok(request, form, "admincompany")
        if nonce is None:
            return self._message(
                "Ошибка формы", "Откройте страницу входа заново.", 403, relogin=True
            )
        raw = await self.kv.get_bytes(self.kv.key("admincred", token_hash(nonce)))
        if not raw:
            return self._message("Время вышло", "Войдите заново.", 400, relogin=True)
        creds = json.loads(self.secrets.decrypt(raw))
        match = [c for c in creds["companies"] if c[0] == str(form.get("company", ""))]
        if not match:
            return self._message("Ошибка", "Такой компании нет в списке.", 400, relogin=True)
        return await self._finish(
            nonce, creds["login"], creds["password"], YouGileCompany(*match[0])
        )

    async def _finish(
        self, nonce: str, login: str, password: str, company: YouGileCompany
    ) -> Response:
        try:
            connected = await self.signin.connect(login, password, company)
        except KeyLimitReached:
            return self._message(
                "Слишком много ключей",
                "У вашей учётной записи уже 30 ключей API — это предел YouGile. "
                "Удалите лишние в YouGile и попробуйте снова.",
                409,
            )
        except BadCredentials:
            return self._login_page(
                nonce, error="Неверный логин или пароль YouGile.", login=login, status=401
            )
        finally:
            await self.kv.delete(self.kv.key("admincred", token_hash(nonce)))
        if connected.user is None or not connected.access.allowed:
            return self._message("Доступ закрыт", connected.access.message(), 402)
        if not connected.user.is_admin:
            return self._message(
                "Нет прав администратора", "YouGile не подтвердил права администратора.", 403
            )
        self.tenancy.forget(connected.user.id)  # the key was just replaced: rebuild right away
        token = new_token(32)
        await self.kv.put_json(
            self.kv.key("admin", token_hash(token)),
            {"user_id": connected.user.id, "company_id": connected.company.id},
            self.settings.admin_session_ttl,
        )
        await self.db.audit(
            "admin_session", company_id=connected.company.id, user_id=connected.user.id
        )
        response = _redirect("/admin")
        self._set_cookie(response, self.session_cookie, token, self.settings.admin_session_ttl)
        self._drop_cookie(response, self.login_cookie)
        return response

    async def logout(self, request: Request) -> Response:
        session = await self._session(request)
        if session is not None:
            form = await request.form()
            if not same_origin(request, self.settings.public_url) or not self.secrets.csrf_ok(
                "admin:" + session.key, str(form.get("csrf", ""))
            ):
                return self._message("Ошибка формы", "Обновите страницу и попробуйте снова.", 403)
            await self.kv.delete(session.key)
        response = _redirect("/admin/login")
        self._drop_cookie(response, self.session_cookie)
        return response

    # ---------- employees ----------

    async def users(self, request: Request) -> Response:
        return await self._guarded(request, self._users)

    async def _users(self, request: Request, ctx: Ctx) -> Response:
        people = await self._people(ctx.session.company.id, await fetch_all(ctx.client, "/users"))
        projects = dict(_projects(await fetch_all(ctx.client, "/projects")))
        return self._html(
            ui.users_page(ctx.frame, people, projects, request.query_params.get("done"))
        )

    # ---------- company settings ----------

    async def settings_form(self, request: Request) -> Response:
        return await self._guarded(request, self._settings_form)

    async def _settings_form(self, request: Request, ctx: Ctx) -> Response:
        structure = await Directory(ctx.client).structure()
        projects = _projects(structure.projects.values())
        columns = _column_titles(structure.columns.values())
        s = ctx.session.company.settings
        instructions = s.get("instructions", "")
        values = ui.SettingsForm(
            timezone=ctx.frame.tz.key,
            default_role=ctx.frame.default_role,
            instructions="\n".join(instructions)
            if isinstance(instructions, list)
            else instructions,
            confirm=_project_ids(s.get("confirm_projects", []), projects),
            deny=ctx.frame.company_denied,
            workflows=format_workflows(s.get("workflows", {})),
            done=_pick_titles(s.get("done_columns", []), columns),
        )
        done = request.query_params.get("done")
        return self._html(ui.settings_page(ctx.frame, values, projects, columns, done=done))

    async def save_settings(self, request: Request) -> Response:
        return await self._guarded(request, self._save_settings, post=True)

    async def _save_settings(self, request: Request, ctx: Ctx) -> Response:
        form = ctx.form
        structure = await Directory(ctx.client).structure()
        projects = _projects(structure.projects.values())
        columns = _column_titles(structure.columns.values())
        values = ui.SettingsForm(
            timezone=str(form.get("timezone", "")).strip() or DEFAULT_TIMEZONE,
            default_role=str(form.get("default_role", "")),
            instructions=str(form.get("instructions", "")).replace("\r\n", "\n").strip(),
            confirm={str(p) for p in form.getlist("confirm")} & {pid for pid, _ in projects},
            deny={str(k) for k in form.getlist("deny")},
            workflows=str(form.get("workflows", "")).replace("\r\n", "\n").strip(),
            done=_pick_titles([str(v) for v in form.getlist("done")], columns),
        )
        errors: list[str] = []
        if values.timezone not in ui.timezones():
            errors.append(f"Неизвестный часовой пояс «{values.timezone}». Пример: Europe/Moscow.")
        if values.default_role not in ui.ROLES:
            errors.append("Выберите роль по умолчанию.")
        if len(values.instructions) > MAX_INSTRUCTIONS:
            errors.append(f"Правила длиннее {MAX_INSTRUCTIONS} символов — сократите их.")
        workflows, workflow_errors = parse_workflows(values.workflows, structure)
        errors += workflow_errors
        new = {
            **ctx.session.company.settings,
            "timezone": values.timezone,
            "default_role": values.default_role,
            "instructions": values.instructions,
            "confirm_projects": sorted(values.confirm),
            "deny": deny_list(values.deny),
            "workflows": workflows,
            "done_columns": [title for title in columns if title in values.done],
        }
        if not errors:
            try:  # the same check the MCP side runs: a bad value must never reach it
                keys = ("timezone", "instructions", "workflows", "deny", "done_columns")
                WorkspaceConfig.from_dict(
                    {k: new[k] for k in keys}
                    | {"role": new["default_role"], "confirm_projects": new["confirm_projects"]}
                )
            except ConfigError as exc:
                errors.append(str(exc))
        if errors:
            page = ui.settings_page(ctx.frame, values, projects, columns, errors=errors)
            return self._html(page, 400)
        company = ctx.session.company
        await self.db.save_company_settings(company.id, new)
        await self.db.audit(
            "admin_settings",
            company_id=company.id,
            user_id=ctx.session.user.id,
            default_role=values.default_role,
            deny=new["deny"],
            confirm_projects=new["confirm_projects"],
            workflows=list(workflows),
            done_columns=new["done_columns"],
        )
        return _redirect("/admin/settings?done=settings")

    # ---------- one employee ----------

    async def user_form(self, request: Request) -> Response:
        return await self._guarded(request, self._user_form)

    async def _user_form(self, request: Request, ctx: Ctx) -> Response:
        person = await self._person(ctx, request.path_params["user_id"])
        if person is None:
            return self._message("Не найдено", "Такого сотрудника нет в компании.", 404)
        projects = _projects(await fetch_all(ctx.client, "/projects"))
        rights = person.rights
        values = ui.RightsForm(
            role=(rights.role or "") if rights else "",
            scope="some" if rights and rights.projects is not None else "all",
            projects=_project_ids(rights.projects or [], projects) if rights else set(),
            deny=denied_keys(rights.deny) if rights else set(),
        )
        done = request.query_params.get("done")
        return self._html(ui.user_page(ctx.frame, person, values, projects, done=done))

    async def save_user(self, request: Request) -> Response:
        return await self._guarded(request, self._save_user, post=True)

    async def _save_user(self, request: Request, ctx: Ctx) -> Response:
        person = await self._person(ctx, request.path_params["user_id"])
        if person is None:
            return self._message("Не найдено", "Такого сотрудника нет в компании.", 404)
        projects = _projects(await fetch_all(ctx.client, "/projects"))
        form = ctx.form
        values = ui.RightsForm(
            role=str(form.get("role", "")),
            scope="some" if form.get("scope") == "some" else "all",
            projects={str(p) for p in form.getlist("project")} & {pid for pid, _ in projects},
            deny={str(k) for k in form.getlist("deny")} - ctx.frame.company_denied,
        )
        errors: list[str] = []
        if values.role and values.role not in ui.ROLES:
            errors.append("Выберите роль из списка.")
        if values.scope == "some" and not values.projects:
            errors.append("Отметьте хотя бы один проект или выберите «Все проекты».")
        if errors:
            page = ui.user_page(ctx.frame, person, values, projects, errors=errors)
            return self._html(page, 400)
        chosen = sorted(values.projects) if values.scope == "some" else None
        deny = deny_list(values.deny)
        company = ctx.session.company
        await self.db.set_rights(
            company.id, person.id, role=values.role or None, projects=chosen, deny=deny
        )
        if person.user:
            self.tenancy.forget(person.user.id)
        await self.db.audit(
            "admin_rights",
            company_id=company.id,
            user_id=ctx.session.user.id,
            target=person.id,
            role=values.role or None,
            projects=chosen,
            deny=deny,
        )
        return _redirect(f"/admin/users/{quote(person.id, safe='')}?done=rights")

    async def reset_user(self, request: Request) -> Response:
        return await self._guarded(request, self._reset_user, post=True)

    async def _reset_user(self, request: Request, ctx: Ctx) -> Response:
        person_id = request.path_params["user_id"]
        company = ctx.session.company
        await self.db.delete_rights(company.id, person_id)
        for user in await self.db.list_users(company.id):
            if user.yougile_user_id == person_id:
                self.tenancy.forget(user.id)
        await self.db.audit(
            "admin_reset", company_id=company.id, user_id=ctx.session.user.id, target=person_id
        )
        return _redirect(f"/admin/users/{quote(person_id, safe='')}?done=reset")

    async def disconnect_user(self, request: Request) -> Response:
        return await self._guarded(request, self._disconnect_user, post=True)

    async def _disconnect_user(self, request: Request, ctx: Ctx) -> Response:
        person_id = request.path_params["user_id"]
        company = ctx.session.company
        users = [u for u in await self.db.list_users(company.id) if u.yougile_user_id == person_id]
        if not users:
            return self._message("Не найдено", "Этот сотрудник не подключён.", 404)
        user = users[0]
        try:
            key = self.secrets.decrypt(user.api_key_enc)
        except ValueError:
            key = None
        await self.db.delete_user(user.id)  # refresh tokens go with it (ON DELETE CASCADE)
        self.tenancy.forget(user.id)
        if key:
            await self.auth.delete_key(key)
        await self.db.audit(
            "admin_disconnect", company_id=company.id, user_id=ctx.session.user.id, target=person_id
        )
        if user.id == ctx.session.user.id:  # the admin disconnected themselves
            await self.kv.delete(ctx.session.key)
            response = _redirect("/admin/login")
            self._drop_cookie(response, self.session_cookie)
            return response
        return _redirect("/admin?done=disconnect")
