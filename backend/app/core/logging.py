"""Structured JSON logging with correlation/task/agent context and secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("correlation_id", default=None)
task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("task_id", default=None)
agent_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent_id", default=None)

_SECRET_KEY_RE = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|credential|private[_-]?key)",
                            re.IGNORECASE)
# keys that merely contain a secret-looking word but are plain numbers/metadata worth keeping in the journal
_SAFE_KEYS = frozenset({"prompt_tokens", "completion_tokens", "total_tokens", "max_tokens", "tokens",
                        "token_count", "tokens_used", "max_output_tokens", "max_input_tokens"})
_SECRET_VALUE_RES = [
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]{8,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),        # telegram bot token
    re.compile(r"\b(sk|pk|ak|ghp|gho|ghs|xox[bap])[-_][A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                   # AWS access key id
]
REDACTED = "***REDACTED***"


def redact_text(text: str) -> str:
    for rx in _SECRET_VALUE_RES:
        text = rx.sub(REDACTED, text)
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact secret-looking keys/values. Used for logs and stored tool arguments."""
    if _depth > 8:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k) and k.lower() not in _SAFE_KEYS:
                out[k] = REDACTED
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [redact(v, _depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_text(record.getMessage()),
        }
        for key, var in (("correlation_id", correlation_id_var), ("task_id", task_id_var), ("agent_id", agent_id_var)):
            val = var.get()
            if val:
                payload[key] = val
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{datetime.fromtimestamp(record.created, UTC).strftime('%H:%M:%S')} {record.levelname:<7} " \
               f"{record.name}: {redact_text(record.getMessage())}"
        tid = task_id_var.get()
        if tid:
            base += f" [task={tid}]"
        extra = getattr(record, "extra_fields", None)
        if extra:
            base += " " + json.dumps(redact(extra), default=str)
        if record.exc_info:
            base += "\n" + redact_text(self.formatException(record.exc_info))
        return base


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # keep noisy libraries quiet (log volume control)
    for noisy in ("httpx", "httpcore", "sqlalchemy.engine", "uvicorn.access", "mcp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"extra_fields": fields})
