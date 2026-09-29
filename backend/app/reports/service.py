"""Structured report generation (objective, executive summary, agents, actions, tools, results, errors,
evidence, lessons, final status). Metadata + content stored in `reports`; Markdown rendered for humans."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import EventType
from app.models import Report, Task, ToolExecution
from app.services import journal


def render_markdown(c: dict[str, Any]) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) if items else "- none"

    tools = [f"`{t['tool_id']}` - {t['status']} ({t.get('duration_ms') or 0} ms)" for t in c["tools"]]
    evidence = [f"{e.get('url') or e.get('tool_id')}" + (f" - {e['title']}" if e.get("title") else "")
                for e in c["evidence"]]
    steps = [f"**Step {s['step_index']} - {s['agent_id']}** ({s['status']}): {s.get('summary', '')}" for s in c["steps"]]
    return f"""# {c['title']}

**Task:** {c['task_id']}  |  **Final status:** {c['final_status']}  |  **Mode:** {c['mode']}

## Objective
{c['objective']}

## Executive summary
{c['executive_summary']}

## Agents
{bullets(c['agents'])}

## Steps and results
{bullets(steps)}

## Actions
{bullets(c['actions'])}

## Tools
{bullets(tools)}

## Findings
{bullets(c['findings'])}

## Recommendations
{bullets(c['recommendations'])}

## Errors and configuration gaps
{bullets(c['errors'])}

## Evidence
{bullets(evidence)}

## Lessons applied
{bullets(c['lessons'])}
"""


async def generate_report(session: AsyncSession, task: Task, final: dict[str, Any]) -> Report:
    tool_rows = list((await session.execute(select(ToolExecution).where(ToolExecution.task_id == task.id)
                                            .order_by(ToolExecution.started_at))).scalars())
    steps = final.get("steps", [])
    content = {
        "task_id": task.id,
        "title": f"Report: {task.original_request[:90]}",
        "objective": task.normalized_request or task.original_request,
        "executive_summary": final.get("summary", ""),
        "final_status": task.status,
        "mode": "offline (no LLM)" if final.get("offline") else "LLM via OmniRoute",
        "agents": sorted({s["agent_id"] for s in steps}),
        "steps": [{"step_index": s["step_index"], "agent_id": s["agent_id"], "status": s.get("status"),
                   "summary": (s.get("summary") or "")[:800]} for s in steps],
        "actions": [a for s in steps for a in s.get("actions_taken", [])][:40],
        "tools": [{"tool_id": t.tool_id, "status": t.status, "risk_level": t.risk_level,
                   "duration_ms": t.duration_ms} for t in tool_rows],
        "findings": [f for s in steps for f in s.get("findings", [])][:40],
        "recommendations": [r for s in steps for r in s.get("recommendations", [])][:30],
        "errors": sorted(set(final.get("validation", {}).get("issues", []) +
                             final.get("validation", {}).get("warnings", []) +
                             [t.error for t in tool_rows if t.error]))[:30],
        "evidence": [e for s in steps for e in s.get("evidence", []) if e.get("source") == "url"][:30],
        "lessons": [m["content"][:300] for m in final.get("applied_lessons", [])],
    }
    existing = (await session.execute(select(Report).where(Report.task_id == task.id))).scalar_one_or_none()
    report = existing or Report(task_id=task.id)
    report.project_id = task.project_id
    report.title = content["title"]
    report.objective = content["objective"]
    report.executive_summary = content["executive_summary"][:8000]
    report.final_status = task.status
    report.content = content
    report.markdown = render_markdown(content)
    if existing is None:
        session.add(report)
    await session.flush()
    await journal.record(session, task.id, EventType.REPORT_CREATED, f"Report generated: {report.title}",
                         report_id=str(report.id))
    return report
