"""Result Normalizer: converts provider-specific tool output into the common shape

    {status, summary, data, evidence, errors, metadata}

so agents never depend on Composio / MCP / provider response formats.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from app.core.logging import redact
from app.schemas.common import NormalizedResult
from app.tools.base import RawToolResult, ToolSpec

MAX_DATA_CHARS = 12000
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")


def _truncate(value: Any, limit: int = MAX_DATA_CHARS) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


def _summarize(data: Any, text: str) -> str:
    if isinstance(data, dict):
        for key in ("summary", "title", "message", "text", "content"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:400]
        keys = ", ".join(list(data.keys())[:8])
        return f"Returned object with fields: {keys}"
    if isinstance(data, list):
        return f"Returned {len(data)} item(s)"
    return (text or "").strip()[:400] or "No content returned"


def _unwrap_composio(data: Any) -> tuple[Any, list[str]]:
    """Composio wraps results as {"data": ..., "successful"/"successfull": bool, "error": ...}."""
    errors: list[str] = []
    if isinstance(data, dict) and ("successful" in data or "successfull" in data) and "data" in data:
        ok = data.get("successful", data.get("successfull"))
        if not ok and data.get("error"):
            errors.append(str(data["error"])[:500])
        return data.get("data"), errors
    return data, errors


def normalize(spec: ToolSpec, raw: RawToolResult, *, task_id: str | None = None,
              execution_id: str | None = None) -> NormalizedResult:
    texts = [c.get("text", "") for c in raw.content if c.get("type") == "text"]
    joined = "\n".join(t for t in texts if t)
    data: Any = raw.structured
    if isinstance(data, dict) and set(data.keys()) == {"result"}:  # FastMCP wraps non-object returns
        data = data["result"]
    if data is None and joined:
        try:
            data = json.loads(joined)
        except json.JSONDecodeError:
            data = {"text": joined}
    data, errors = _unwrap_composio(data)
    if raw.is_error:
        errors.append(joined[:1000] or "Tool reported an error")

    evidence: list[dict[str, Any]] = []
    urls = set()
    if isinstance(data, dict) and isinstance(data.get("url"), str):
        urls.add(data["url"])
    urls.update(_URL.findall(joined[:20000]) if spec.toolkit != "web" else [])
    base = {"tool_id": spec.id, "provider": spec.provider, "task_id": task_id, "tool_execution_id": execution_id,
            "retrieved_at": datetime.now(UTC).isoformat()}
    for u in list(urls)[:10]:
        evidence.append({**base, "source": "url", "url": u,
                         "title": data.get("title") if isinstance(data, dict) else None})
    if not evidence and not errors:
        evidence.append({**base, "source": "tool_output"})

    status = "error" if errors else "success"
    return NormalizedResult(status=status, summary=_summarize(data, joined) if not errors else errors[0][:400],
                            data=_truncate(redact(data)), evidence=evidence, errors=errors,
                            metadata={"tool_id": spec.id, "provider": spec.provider, "toolkit": spec.toolkit,
                                      "risk_level": str(spec.risk_level), **raw.metadata})


def error_result(spec_id: str, status: str, message: str, **metadata: Any) -> NormalizedResult:
    return NormalizedResult(status=status, summary=message, data=None, evidence=[], errors=[message],
                            metadata={"tool_id": spec_id, **metadata})
