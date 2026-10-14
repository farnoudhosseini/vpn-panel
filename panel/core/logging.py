"""Structured logging used by both the panel and the node agent.

Console output is human readable; a rotating JSON-lines file is written for
auditing. Sensitive values are never logged by callers (see ``redact``).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

_CONFIGURED = False
DEFAULT_LOG_DIR = Path("/var/log/panah")

# Patterns that must never reach a log file. Order matters: the bearer-token
# rule runs first so "Authorization: Bearer x" does not keep the raw token.
_REDACT_PATTERNS = [
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)"
               r"([\"'\s:=]+)([^\s\"',}]+)"),
]


def redact(text: str) -> str:
    """Mask credentials that accidentally appear in a log line."""
    out = text
    for pattern in _REDACT_PATTERNS:
        out = pattern.sub(lambda m: m.group(1) + m.group(2) + "***", out)
    return out


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in ("request_id", "user", "ip", "action", "target"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class _ConsoleFormatter(logging.Formatter):
    COLOURS: ClassVar[dict[str, str]] = {
        "DEBUG": "\033[38;5;245m",
        "INFO": "\033[38;5;45m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[48;5;203;38;5;231m",
    }
    RESET = "\033[0m"

    def __init__(self, colour: bool = True) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%H:%M:%S")
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        text = text.replace(record.getMessage(), redact(record.getMessage()))
        if not self.colour:
            return text
        colour = self.COLOURS.get(record.levelname, "")
        return f"{colour}{text}{self.RESET}"


def configure_logging(
    *,
    level: str | None = None,
    log_dir: Path | None = None,
    filename: str = "panah.log",
    force: bool = False,
) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    _CONFIGURED = True

    root = logging.getLogger()
    root.setLevel((level or os.environ.get("PANAH_LOG_LEVEL") or "INFO").upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(_ConsoleFormatter(colour=os.environ.get("NO_COLOR") is None))
    root.addHandler(console)

    directory = log_dir or Path(os.environ.get("PANAH_LOG_DIR") or DEFAULT_LOG_DIR)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            directory / filename, maxBytes=16 * 1024 * 1024, backupCount=7, encoding="utf-8"
        )
        file_handler.setFormatter(_JsonFormatter())
        root.addHandler(file_handler)
    except OSError:
        # Development machines and containers may not allow /var/log writes.
        root.warning("file logging disabled: cannot write to %s", directory)

    # Uvicorn access logs duplicate our own request logging middleware.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for noisy in ("apscheduler", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("panel") else f"panel.{name}")
