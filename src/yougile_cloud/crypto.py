"""Encryption of stored API keys, hashing of opaque tokens, CSRF tokens."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from fastmcp.server.auth.jwt_issuer import derive_jwt_key


class Secrets:
    def __init__(self, encryption_keys: list[str], jwt_secret: str) -> None:
        self._fernet = MultiFernet([Fernet(k.encode()) for k in encryption_keys])
        self._hmac_key = derive_jwt_key(high_entropy_material=jwt_secret, salt="yougile-cloud/csrf")
        self.jwt_key = derive_jwt_key(high_entropy_material=jwt_secret, salt="yougile-cloud/jwt")

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(bytes(token)).decode()
        except InvalidToken as exc:
            raise ValueError("stored secret cannot be decrypted with ENCRYPTION_KEYS") from exc

    def rotate(self, token: bytes) -> bytes:
        """Re-encrypt with the newest key (after prepending a key to ENCRYPTION_KEYS)."""
        return self._fernet.rotate(bytes(token))

    def csrf_token(self, bound_to: str) -> str:
        return hmac.new(self._hmac_key, bound_to.encode(), hashlib.sha256).hexdigest()

    def csrf_ok(self, bound_to: str, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(self.csrf_token(bound_to), token or "")


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_fernet_key() -> str:
    return Fernet.generate_key().decode()
