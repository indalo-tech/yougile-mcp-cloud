"""OAuth 2.1 authorization server for MCP clients (claude.ai, Claude Desktop, Claude Code).

- clients register dynamically (RFC 7591) and are stored in Postgres;
- /authorize parks the request in Valkey and sends the browser to our YouGile sign-in page;
- authorization codes live in Valkey for 5 minutes and can be exchanged once;
- access tokens are short JWTs with iss, aud (the MCP resource) and token_use checked;
- refresh tokens are opaque, stored hashed, and rotate on every use.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

from fastmcp.server.auth.auth import (
    AccessToken,
    ClientRegistrationOptions,
    OAuthProvider,
    RevocationOptions,
)
from fastmcp.server.auth.jwt_issuer import JWTIssuer
from joserfc.errors import JoseError
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .access import access_of
from .crypto import Secrets, new_token, token_hash
from .db import Database
from .kv import KV
from .settings import Settings

AUTH_REQUEST_TTL = 15 * 60
AUTH_CODE_TTL = 5 * 60


class CloudOAuthProvider(OAuthProvider):
    def __init__(self, settings: Settings, secrets: Secrets) -> None:
        super().__init__(
            base_url=settings.public_url,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        )
        self.settings = settings
        self.issuer = JWTIssuer(
            issuer=settings.public_url, audience=settings.mcp_url, signing_key=secrets.jwt_key
        )
        self.db: Database | None = None  # attached on startup
        self.kv: KV | None = None

    def attach(self, db: Database, kv: KV) -> None:
        self.db, self.kv = db, kv

    # ---------- clients ----------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = await self.db.get_client(client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            raise ValueError("client_id is required for client registration")
        await self.db.save_client(
            client_info.client_id, client_info.model_dump(mode="json", exclude_none=True)
        )

    # ---------- authorization ----------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource and params.resource.rstrip("/") != self.settings.mcp_url:
            raise AuthorizeError(
                error="invalid_request", error_description="unknown resource for this server"
            )
        flow = new_token(24)
        await self.kv.put_json(
            self.kv.key("authreq", flow),
            {
                "client_id": client.client_id,
                "client_name": client.client_name or "",
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "scopes": params.scopes or [],
                "state": params.state,
                "resource": params.resource,
            },
            AUTH_REQUEST_TTL,
        )
        return f"{self.settings.public_url}/signin?flow={flow}"

    async def issue_code(self, request: dict, *, user_id: int, company_id: str) -> str:
        """Called by the sign-in page once the person is authenticated."""
        code = new_token(32)
        await self.kv.put_json(
            self.kv.key("code", code),
            {
                **request,
                "user_id": user_id,
                "company_id": company_id,
                "expires_at": time.time() + AUTH_CODE_TTL,
            },
            AUTH_CODE_TTL,
        )
        return code

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = await self.kv.get_json(self.kv.key("code", authorization_code))
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=data["expires_at"],
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data.get("resource"),
            subject=str(data["user_id"]),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        data = await self.kv.take_json(self.kv.key("code", authorization_code.code))
        if not data or data["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "authorization code not found or already used")
        return await self._issue(
            client_id=data["client_id"],
            user_id=int(data["user_id"]),
            company_id=data["company_id"],
            scopes=data["scopes"],
            resource=data.get("resource"),
        )

    # ---------- tokens ----------

    async def _issue(
        self,
        *,
        client_id: str,
        user_id: int,
        company_id: str,
        scopes: list[str],
        resource: str | None,
    ) -> OAuthToken:
        ttl = self.settings.access_token_ttl
        access = self.issuer.issue_access_token(
            client_id=client_id,
            scopes=scopes,
            jti=uuid.uuid4().hex,
            expires_in=ttl,
            subject=str(user_id),
            extra_claims={"cid": company_id},
        )
        refresh = new_token(48)
        await self.db.save_refresh(
            token_hash(refresh),
            user_id=user_id,
            client_id=client_id,
            scopes=scopes,
            resource=resource,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.settings.refresh_token_ttl),
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",  # noqa: S106 - not a secret
            expires_in=ttl,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = await self.db.get_refresh(token_hash(refresh_token))
        if not row or row["client_id"] != client.client_id:
            return None
        if row["expires_at"] < datetime.now(UTC):
            await self.db.take_refresh(token_hash(refresh_token))
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=list(row["scopes"] or []),
            expires_at=int(row["expires_at"].timestamp()),
            resource=row["resource"],
            subject=str(row["user_id"]),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        row = await self.db.take_refresh(token_hash(refresh_token.token))  # rotation: used once
        if not row or row["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "refresh token not found or already used")
        granted = list(row["scopes"] or [])
        if not set(scopes) <= set(granted):
            raise TokenError("invalid_scope", "requested scopes exceed the original grant")
        user = await self.db.get_user(row["user_id"])
        company = await self.db.get_company(user.company_id) if user else None
        if user is None or company is None:
            raise TokenError("invalid_grant", "the account is no longer connected")
        if not access_of(company, self.settings.free_company_ids).allowed:
            raise TokenError("invalid_grant", "the company has no active access")
        return await self._issue(
            client_id=row["client_id"],
            user_id=user.id,
            company_id=company.id,
            scopes=scopes or granted,
            resource=row["resource"],
        )

    async def load_access_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        try:
            claims = self.issuer.verify_token(token, expected_token_use="access")  # noqa: S106
        except JoseError:
            return None
        if not claims.get("sub") or not claims.get("cid"):
            return None
        if await self.kv.get_bytes(self.kv.key("revoked", claims["jti"])):
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=[s for s in (claims.get("scope") or "").split() if s],
            expires_at=claims["exp"],
            resource=claims["aud"],
            subject=claims["sub"],
            claims=claims,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            await self.db.take_refresh(token_hash(token.token))
            return
        jti = (token.claims or {}).get("jti") if isinstance(token, AccessToken) else None
        if jti:
            remaining = max(1, int((token.expires_at or time.time()) - time.time()))
            await self.kv.put_bytes(self.kv.key("revoked", jti), b"1", remaining)
