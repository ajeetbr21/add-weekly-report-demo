"""Deterministic offline engine used when no LLM is available through OmniRoute.

It does NOT imitate a model. It (1) selects tools whose required arguments can be derived directly from
the request, (2) executes them through the normal tool pipeline, and (3) assembles a transparent,
rule-based report from real tool results and retrieved memory. Every result produced this way is
labelled `offline: true` / "Offline mode".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.agents.registry import strip_literals
from app.core.enums import RiskLevel
from app.memory.context import StepContext
from app.memory.embeddings import _stem
from app.tools.base import ToolSpec

_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_TOKEN = re.compile(r"[a-z0-9]+")
_REGION = re.compile(r"\b([a-z]{2}-(?:north|south|east|west|central|northeast|southeast)-\d)\b")
_DELETE = re.compile(r"\b(?:delete|remove)\s+(?:the\s+)?(?:file\s+)?[`'\"]?([\w./\-]+\.\w+)[`'\"]?", re.I)
_TESTS = re.compile(r"\brun\s+(?:the\s+)?(?:py)?tests?\b(?:\s+(?:in|for)\s+[`'\"]?([\w./\-]+)[`'\"]?)?", re.I)
_QUOTED = re.compile(r"[\"“']([^\"”']{3,80})[\"”']")


@dataclass
class PlannedCall:
    spec: ToolSpec
    arguments: dict[str, Any]
    reason: str


def _infer_args(spec: ToolSpec, text: str) -> dict[str, Any] | None:
    """Return arguments if every required argument can be derived from the request, else None."""
    schema = spec.input_schema or {}
    props: dict[str, Any] = schema.get("properties") or {}
    required: list[str] = list(schema.get("required") or [])
    args: dict[str, Any] = {}
    urls = _URL.findall(text)
    for name in required:
        lname = name.lower()
        if lname in ("url", "uri", "link") and urls:
            args[name] = urls[0].rstrip(".,")
        elif lname in ("region", "aws_region") and _REGION.search(text):
            args[name] = _REGION.search(text).group(1)  # type: ignore[union-attr]
        elif lname == "path" and spec.name == "delete_workspace_file" and _DELETE.search(text):
            args[name] = _DELETE.search(text).group(1)  # type: ignore[union-attr]
        elif lname == "query" and _QUOTED.search(text):
            args[name] = _QUOTED.search(text).group(1)  # type: ignore[union-attr]
        elif "default" in (props.get(name) or {}):
            args[name] = props[name]["default"]
        else:
            return None
    if spec.name == "run_pytest":
        m = _TESTS.search(text)
        if m and m.group(1):
            args["path"] = m.group(1)
    return args


_GENERIC = {"get", "workspace", "current", "http", "run", "aws", "github", "google", "composio", "the", "a"}


def _relevance(spec: ToolSpec, words: set[str]) -> float:
    """Overlap between request words and the tool's *name* tokens (stemmed, generic tokens ignored),
    plus a smaller weight for capability tags. Descriptions are ignored to avoid spurious matches."""
    name_toks = {_stem(t) for t in _TOKEN.findall(spec.name.lower().replace("_", " "))} - _GENERIC
    caps = {_stem(t) for c in spec.capabilities for t in _TOKEN.findall(c.lower())} - _GENERIC
    return float(len(words & name_toks)) + 0.25 * len(words & caps)


def select_tool_calls(ctx: StepContext, tools: list[ToolSpec], limit: int = 3) -> list[PlannedCall]:
    text = f"{ctx.request}\n{ctx.objective}"
    # relevance uses intent words only (URLs / paths / file names removed); arguments use the raw text
    words = {_stem(w) for w in _TOKEN.findall(strip_literals(text).lower()) if len(w) > 2}
    calls: list[PlannedCall] = []
    explicit_delete = bool(_DELETE.search(text))
    explicit_tests = bool(_TESTS.search(text))
    ranked = sorted(tools, key=lambda s: _relevance(s, words), reverse=True)
    for spec in ranked:
        if len(calls) >= limit:
            break
        # mutations only when the user explicitly asked for exactly that action
        if spec.risk_level != RiskLevel.READ:
            if not ((spec.name == "delete_workspace_file" and explicit_delete)
                    or (spec.name == "run_pytest" and explicit_tests)):
                continue
        if spec.name == "get_current_time" and not ({"today", "time", "date", "now", "current", "todays", "tonight",
                                                     "yesterday"} & words):
            continue
        rel = _relevance(spec, words)
        must = spec.name in ("http_fetch",) and _URL.search(text)
        if rel < 1 and not must and spec.risk_level == RiskLevel.READ and spec.name != "get_current_time":
            continue
        args = _infer_args(spec, text)
        if args is None:
            continue
        calls.append(PlannedCall(spec=spec, arguments=args, reason=f"relevance={rel:.0f}"))
    return calls


def short_reason(reason: str | None) -> str:
    """Human-readable fallback reason; raw gateway error bodies stay in the MODEL_CALL journal metadata."""
    text = (reason or "OmniRoute not configured").strip()
    text = re.sub(r":\s*[\[{].*$", "", text, flags=re.S)
    return text[:160]


def compose_offline_result(agent_name: str, ctx: StepContext, tool_results: list[dict[str, Any]],
                           unavailable: list[str], reason: str | None) -> dict[str, Any]:
    findings: list[str] = []
    for tr in tool_results:
        res = tr["result"]
        prefix = f"{tr['tool_id']}: "
        if res.get("status") == "success":
            findings.append(prefix + (res.get("summary") or "ok")[:400])
        else:
            findings.append(prefix + "FAILED - " + "; ".join(res.get("errors") or [res.get("summary", "")])[:300])
    lessons = ctx.lessons
    recommendations = [f"Apply learned procedure: {m.content[:300]}" for m in lessons]
    related = [m for m in ctx.memories if m.type in ("EPISODIC", "SEMANTIC")][:3]
    for m in related:
        findings.append(f"Related memory ({m.type.lower()}): {m.content[:250]}")
    config_required = [f"CONFIGURATION REQUIRED: {u}" for u in unavailable]
    ok = [t for t in tool_results if t["result"].get("status") == "success"]
    parts = [f"Offline mode (no LLM available: {short_reason(reason)}). {agent_name} handled: "
             f"{ctx.objective[:200].rstrip('.?!')}."]
    if ok:
        parts.append(f"Executed {len(ok)} tool call(s) successfully: " + "; ".join(t["tool_id"] for t in ok) + ".")
    elif tool_results:
        parts.append("Tool calls were attempted but none succeeded.")
    else:
        parts.append("No tool could be executed automatically for this request.")
    if lessons:
        parts.append(f"{len(lessons)} learned procedure(s) from previous feedback apply to this task.")
    if config_required:
        parts.append(" ".join(config_required) + ".")
    return {
        "summary": " ".join(parts),
        "findings": findings,
        "actions_taken": [f"{t['tool_id']}({', '.join(f'{k}={v}' for k, v in t['arguments'].items())})"[:200]
                          for t in tool_results],
        "recommendations": recommendations,
        "facts": [],
        "confidence": 0.35 if not ok else 0.55,
        "configuration_required": config_required,
    }
