"""Keep YouGile API keys out of the log.

The core client logs every request's path, and revoking a key is ``DELETE /auth/keys/<key>``:
without this filter each key rotation and each disconnected employee would leave a key in the
container log (a live one, when the deletion failed)."""

from __future__ import annotations

import logging
import re

KEY_IN_PATH = re.compile(r"(/auth/keys/)[^\s/?#'\"]+")


def redact(value: object) -> object:
    return KEY_IN_PATH.sub(r"\1***", value) if isinstance(value, str) else value


class RedactKeys(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact(v) for k, v in record.args.items()}
        return True


def install() -> None:
    for name in ("yougile_mcp.client", "httpx2", "httpx"):
        logging.getLogger(name).addFilter(RedactKeys())
