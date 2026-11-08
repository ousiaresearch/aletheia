"""Phase 6: structured logging with redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any


class RedactingFormatter(logging.Formatter):
    """JSON formatter that redacts untrusted content fields."""

    _PATTERNS = [
        re.compile(r"<\s*UNTRUSTED_DOCUMENT\s*>", re.IGNORECASE),
        re.compile(r"<\s*/\s*UNTRUSTED_DOCUMENT\s*>", re.IGNORECASE),
        re.compile(r"\[\s*REDACTED\s+INJECTION\s*\]", re.IGNORECASE),
    ]

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        for pat in self._PATTERNS:
            message = pat.sub("[REDACTED]", message)
        return message


def setup_logging(level: str = "INFO") -> None:
    """Configure structured JSON logging with redaction."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))


def log_event(
    operation: str,
    record_id: str | None = None,
    scope: str | None = None,
    detail: str = "",
    level: str = "INFO",
) -> None:
    """Emit a structured log event."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "operation": operation,
        "record_id": record_id,
        "scope": scope,
        "detail": detail,
    }
    logging.getLogger("aletheia").log(getattr(logging, level, logging.INFO), json.dumps(entry))
