"""Where OAuth may send a person back to, and how the sign-in page names it.

Client registration is open (AI clients register themselves), so a registered redirect URI
proves nothing: anyone could register «Claude» with a callback on their own site and send a
person a sign-in link to this server. Only known clients' callbacks and the person's own
machine (loopback, as native clients like Claude Code use) are accepted; more can be added
with OAUTH_REDIRECT_ORIGINS.
"""

from __future__ import annotations

from urllib.parse import urlparse

LOOPBACK = {"localhost", "127.0.0.1", "::1"}
KNOWN_ORIGINS = ("https://claude.ai", "https://claude.com")


def parse_allowed(value: str | None) -> tuple[str, ...]:
    """OAUTH_REDIRECT_ORIGINS: origins like ``https://chatgpt.com`` or app schemes like
    ``cursor:``; paths are ignored, so a prefix trick (``https://chatgpt.com.evil``) fails."""
    allowed: list[str] = []
    for item in (value or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        if "//" not in item:  # a custom scheme: cursor: / cursor://
            allowed.append(item.split(":", 1)[0] + ":")
            continue
        parsed = urlparse(item)
        if parsed.scheme and parsed.netloc:
            allowed.append(f"{parsed.scheme}://{parsed.netloc}")
    return tuple(allowed)


def _origin(uri: str) -> tuple[str, str | None, str]:
    parsed = urlparse(uri)
    return parsed.scheme.lower(), parsed.hostname, f"{parsed.scheme}://{parsed.netloc}".lower()


def allowed(uri: str, extra: tuple[str, ...] = ()) -> bool:
    scheme, host, origin = _origin(uri)
    if scheme in ("http", "https") and host in LOOPBACK:
        return True
    if origin in KNOWN_ORIGINS or origin in extra:
        return True
    return f"{scheme}:" in extra and scheme not in ("http", "https", "javascript", "data")


def destination(uri: str) -> str:
    """What the sign-in page says the person goes back to."""
    scheme, host, _origin_ = _origin(uri)
    if host in LOOPBACK:
        return "программа на этом компьютере"
    if scheme in ("http", "https"):
        return host or uri
    return f"приложение {scheme}:"
