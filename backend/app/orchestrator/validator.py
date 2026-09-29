"""Result Validator: checks agent step results before they are combined into the final answer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Validation:
    valid: bool
    retryable: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_step_result(result: dict[str, Any]) -> Validation:
    issues, warnings = [], []
    summary = (result.get("summary") or "").strip()
    if not summary or summary == "(empty answer)":
        issues.append("empty summary")
    tools = result.get("tools_used") or []
    failed = [t for t in tools if t.get("status") not in ("success",)]
    if tools and len(failed) == len(tools):
        warnings.append(f"all {len(tools)} tool call(s) failed or were blocked")
    elif failed:
        warnings.append(f"{len(failed)} of {len(tools)} tool call(s) failed or were blocked")
    for c in result.get("configuration_required") or []:
        warnings.append(c)
    if result.get("offline"):
        warnings.append("produced in offline mode (no LLM reasoning)")
    conf = float(result.get("confidence") or 0.5)
    if tools and not failed:
        conf = min(1.0, conf + 0.1)
    if failed:
        conf = max(0.0, conf - 0.1 * len(failed))
    return Validation(valid=not issues, retryable=bool(issues), issues=issues, warnings=warnings,
                      confidence=round(conf, 3))


def validate_task(step_results: list[dict[str, Any]]) -> Validation:
    if not step_results:
        return Validation(valid=False, retryable=False, issues=["no step results"])
    vals = [validate_step_result(r) for r in step_results]
    issues = [i for v in vals for i in v.issues]
    warnings = sorted({w for v in vals for w in v.warnings})
    conf = sum(v.confidence for v in vals) / len(vals)
    return Validation(valid=not issues, retryable=False, issues=issues, warnings=warnings, confidence=round(conf, 3))
