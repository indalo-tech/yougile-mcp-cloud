"""Public HTML routes: the YouGile sign-in step of the OAuth flow, and a health check."""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from mcp.server.auth.provider import construct_redirect_uri
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from . import pages
from .crypto import Secrets
from .kv import KV
from .oauth import CloudOAuthProvider
from .settings import Settings
from .signin import SignIn, TooManyAttempts
from .yougile_auth import BadCredentials, KeyLimitReached, YouGileCompany

log = logging.getLogger(__name__)

CRED_TTL = 5 * 60  # company choice must happen within 5 minutes of entering the password
SECURITY_HEADERS = {
    # No scripts at all; forms may post to ourselves and redirect to the client's callback.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
    "frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
LEAD = "Подключение YouGile к AI-ассистенту. Войдите своей учётной записью YouGile."


def html(body: str, status: int = 200, headers: dict[str, str] = SECURITY_HEADERS) -> HTMLResponse:
    return HTMLResponse(body, status_code=status, headers=headers)


def same_origin(request: Request, public_url: str) -> bool:
    """Form posts must come from our own pages (a missing Origin is allowed, "null" is not)."""
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin is None  # "null" origins (sandboxed frames) are refused
    ours = urlparse(public_url)
    theirs = urlparse(origin)
    return (theirs.scheme, theirs.netloc) == (ours.scheme, ours.netloc)


class SignInPages:
    def __init__(
        self,
        settings: Settings,
        secrets: Secrets,
        provider: CloudOAuthProvider,
        signin: SignIn | None = None,
        kv: KV | None = None,
    ) -> None:
        self.settings, self.secrets, self.provider = settings, secrets, provider
        self.signin, self.kv = signin, kv  # attached on startup

    def attach(self, signin: SignIn, kv: KV) -> None:
        self.signin, self.kv = signin, kv

    def _same_origin(self, request: Request) -> bool:
        return same_origin(request, self.settings.public_url)

    def _form(self, flow: str, *, error: str | None = None, login: str = "", status: int = 200):
        return html(
            pages.login_page(
                action="/signin",
                hidden={"flow": flow, "csrf": self.secrets.csrf_token("signin:" + flow)},
                heading="Вход в YouGile MCP",
                lead=LEAD,
                error=error,
                login=login,
            ),
            status,
        )

    async def _request(self, flow: str) -> dict | None:
        return await self.kv.get_json(self.kv.key("authreq", flow)) if flow else None

    async def show(self, request: Request) -> Response:
        flow = request.query_params.get("flow", "")
        if not await self._request(flow):
            return html(
                pages.message_page(
                    "Ссылка устарела", "Начните подключение заново в вашем AI-клиенте."
                ),
                400,
            )
        return self._form(flow)

    async def submit(self, request: Request) -> Response:
        form = await request.form()
        flow = str(form.get("flow", ""))
        auth_request = await self._request(flow)
        if not auth_request:
            return html(pages.message_page("Ссылка устарела", "Начните подключение заново."), 400)
        if not self._same_origin(request) or not self.secrets.csrf_ok(
            "signin:" + flow, str(form.get("csrf", ""))
        ):
            return html(
                pages.message_page("Ошибка формы", "Обновите страницу и попробуйте снова."), 403
            )
        login = str(form.get("login", "")).strip()
        password = str(form.get("password", ""))
        if not login or not password:
            return self._form(flow, error="Введите логин и пароль.", login=login, status=400)
        try:
            await self.signin.count_attempt(request.client.host if request.client else "?", login)
            companies = await self.signin.companies(login, password)
        except TooManyAttempts:
            return self._form(
                flow, error="Слишком много попыток. Подождите 15 минут.", login=login, status=429
            )
        except BadCredentials:
            return self._form(
                flow, error="Неверный логин или пароль YouGile.", login=login, status=401
            )
        if not companies:
            return self._form(
                flow, error="У этой учётной записи нет компаний в YouGile.", login=login
            )
        if len(companies) == 1:
            return await self._finish(flow, auth_request, login, password, companies[0])
        blob = json.dumps(
            {
                "login": login,
                "password": password,
                "companies": [[c.id, c.name, c.is_admin] for c in companies],
            }
        )
        await self.kv.put_bytes(self.kv.key("cred", flow), self.secrets.encrypt(blob), CRED_TTL)
        return html(
            pages.company_page(
                action="/signin/company",
                hidden={"flow": flow, "csrf": self.secrets.csrf_token("company:" + flow)},
                companies=[(c.id, c.name) for c in companies],
            )
        )

    async def choose_company(self, request: Request) -> Response:
        form = await request.form()
        flow = str(form.get("flow", ""))
        auth_request = await self._request(flow)
        raw = await self.kv.get_bytes(self.kv.key("cred", flow)) if flow else None
        if not auth_request or not raw:
            return html(pages.message_page("Время вышло", "Начните подключение заново."), 400)
        if not self._same_origin(request) or not self.secrets.csrf_ok(
            "company:" + flow, str(form.get("csrf", ""))
        ):
            return html(pages.message_page("Ошибка формы", "Начните подключение заново."), 403)
        creds = json.loads(self.secrets.decrypt(raw))
        chosen = str(form.get("company", ""))
        match = [c for c in creds["companies"] if c[0] == chosen]
        if not match:
            return html(pages.message_page("Ошибка", "Такой компании нет в списке."), 400)
        company = YouGileCompany(*match[0])
        return await self._finish(flow, auth_request, creds["login"], creds["password"], company)

    async def _finish(
        self, flow: str, auth_request: dict, login: str, password: str, company: YouGileCompany
    ) -> Response:
        try:
            connected = await self.signin.connect(login, password, company)
        except KeyLimitReached:
            return html(
                pages.message_page(
                    "Слишком много ключей",
                    "У вашей учётной записи уже 30 ключей API — это предел YouGile. "
                    "Удалите лишние в YouGile и попробуйте снова.",
                ),
                409,
            )
        except BadCredentials:
            return self._form(
                flow, error="Неверный логин или пароль YouGile.", login=login, status=401
            )
        finally:
            await self.kv.delete(self.kv.key("cred", flow))
        if not connected.access.allowed or connected.user is None:
            return html(pages.message_page("Доступ закрыт", connected.access.message()), 402)
        code = await self.provider.issue_code(
            auth_request, user_id=connected.user.id, company_id=connected.company.id
        )
        await self.kv.delete(self.kv.key("authreq", flow))
        target = construct_redirect_uri(
            auth_request["redirect_uri"], code=code, state=auth_request.get("state")
        )
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})


async def healthz(_request: Request) -> Response:
    return JSONResponse({"ok": True})
