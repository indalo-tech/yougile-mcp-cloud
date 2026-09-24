"""One log line per MCP request: the method, the tool, and what the client announced.

HTTP access logs show only "POST /mcp". This says which tool was called and, for listings and
the handshake, which client it is and which extensions it announced (the core shows the MCP Apps
screens only to clients that announce them). No arguments or results are logged.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

log = logging.getLogger(__name__)

DESCRIBED = {"initialize", "tools/list"}  # requests that also log the client


def describe_client(ctx: Any) -> str:
    try:
        session = ctx.session
    except AttributeError, RuntimeError:
        return "no session"
    params = getattr(session, "client_params", None)
    info = getattr(params, "client_info", None)
    caps = getattr(session, "client_capabilities", None) or getattr(params, "capabilities", None)
    extensions = getattr(caps, "extensions", None) if caps else None
    if extensions is None and caps is not None:
        extensions = (caps.model_extra or {}).get("extensions")
    experimental = getattr(caps, "experimental", None) if caps else None
    return (
        f"client={getattr(info, 'name', '?')}/{getattr(info, 'version', '?')} "
        f"protocol={getattr(session, 'protocol_version', '?')} "
        f"extensions={sorted(extensions or {})} experimental={sorted(experimental or {})}"
    )


class RequestLog(Middleware):
    async def on_request(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        method = context.method or "?"
        line = method
        if method == "tools/call":
            line += f" {getattr(context.message, 'name', '?')}"
        elif method == "resources/read":
            line += f" {getattr(context.message, 'uri', '?')}"
        if method in DESCRIBED:
            line += " " + describe_client(context.fastmcp_context)
        log.info("mcp %s", line)
        return await call_next(context)
