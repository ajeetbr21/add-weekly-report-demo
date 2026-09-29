"""Memory consolidation:  raw task -> candidate memories -> evaluation -> classification ->
store / update / reject.  Prevents memory pollution: trivial exchanges, low-confidence claims and
duplicates never become long-term memory. Session memory is never copied wholesale."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import EventType, MemoryType
from app.memory.service import MemoryService
from app.models import AgentDefinition, Task
from app.services import journal

_TRIVIAL = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|test|ping|yes|no)[\s!.?]*$", re.I)
_HEDGE = re.compile(r"\b(maybe|might|possibly|probably|i think|unclear|unknown|not sure)\b", re.I)


@dataclass
class Candidate:
    type: MemoryType
    content: str
    title: str | None
    tags: list[str]
    confidence: float
    importance: float
    source: str
    agent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConsolidationReport:
    stored: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)


def _agent_tags(agent_defs: dict[str, AgentDefinition], agent_ids: list[str]) -> list[str]:
    tags = set(agent_ids)
    for aid in agent_ids:
        a = agent_defs.get(aid)
        if a:
            tags.update((a.memory_policy or {}).get("tags", []))
    return sorted(tags)


def build_candidates(task: Task, step_results: list[dict[str, Any]], final: dict[str, Any],
                     agent_defs: dict[str, AgentDefinition]) -> list[Candidate]:
    agent_ids = [r.get("agent_id") for r in step_results if r.get("agent_id")]
    tags = _agent_tags(agent_defs, agent_ids)
    cands: list[Candidate] = []
    tools_used = sorted({t["tool_id"] for r in step_results for t in r.get("tools_used") or []})
    failed_tools = sorted({t["tool_id"] for r in step_results for t in r.get("tools_used") or []
                           if t.get("status") != "success"})
    # 1) episodic: what was attempted, what worked, what failed, final result
    ep = (f"Task {task.id} ({task.status}): {task.original_request[:500]}\n"
          f"Agents: {', '.join(agent_ids) or 'none'}. Tools used: {', '.join(tools_used) or 'none'}."
          f"{' Failed/blocked tools: ' + ', '.join(failed_tools) + '.' if failed_tools else ''}\n"
          f"Outcome: {str(final.get('summary', ''))[:700]}")
    cands.append(Candidate(type=MemoryType.EPISODIC, content=ep, title=f"Episode {task.id}", tags=tags,
                           confidence=float(final.get("validation", {}).get("confidence", 0.5)),
                           importance=0.55 if task.status == "FAILED" or failed_tools else 0.4,
                           source="task_consolidation", agent_id=agent_ids[0] if len(agent_ids) == 1 else None,
                           metadata={"status": task.status, "tools": tools_used, "offline": final.get("offline")}))
    # 2) evidence: provenance of external sources actually retrieved
    for r in step_results:
        for ev in r.get("evidence") or []:
            if ev.get("source") != "url" or not ev.get("url"):
                continue
            content = f"Evidence source: {ev['url']}" + (f" - {ev['title']}" if ev.get("title") else "")
            cands.append(Candidate(type=MemoryType.EVIDENCE, content=content, title=ev.get("title"),
                                   tags=tags + ["evidence"], confidence=0.9, importance=0.35, source="tool_evidence",
                                   agent_id=r.get("agent_id"),
                                   metadata={"url": ev["url"], "tool_id": ev.get("tool_id"),
                                             "tool_execution_id": ev.get("tool_execution_id"),
                                             "retrieved_at": ev.get("retrieved_at"), "task_id": task.id}))
    # 3) semantic: durable facts proposed by agents (LLM mode only)
    for r in step_results:
        for fact in r.get("facts") or []:
            cands.append(Candidate(type=MemoryType.SEMANTIC, content=str(fact), title=None, tags=tags,
                                   confidence=min(0.8, float(r.get("confidence") or 0.5)), importance=0.5,
                                   source="agent_fact", agent_id=r.get("agent_id"), metadata={"task_id": task.id}))
    return cands


def evaluate(c: Candidate, task: Task, final: dict[str, Any]) -> str | None:
    """Return a rejection reason or None if the candidate should be stored."""
    if c.type == MemoryType.EPISODIC:
        if _TRIVIAL.match(task.original_request) or len(task.original_request.strip()) < 8:
            return "trivial exchange"
        if task.status not in ("COMPLETED", "FAILED"):
            return "task not finished"
    if c.type == MemoryType.SEMANTIC:
        if not (15 <= len(c.content) <= 600):
            return "fact length out of bounds"
        if _HEDGE.search(c.content):
            return "speculative statement"
        if c.confidence < 0.6 or not final.get("validation", {}).get("valid", False):
            return "insufficient confidence / unvalidated result"
    return None


async def consolidate(session: AsyncSession, task: Task, step_results: list[dict[str, Any]], final: dict[str, Any],
                      agent_defs: dict[str, AgentDefinition], memory: MemoryService) -> ConsolidationReport:
    report = ConsolidationReport()
    for cand in build_candidates(task, step_results, final, agent_defs):
        reason = evaluate(cand, task, final)
        if reason:
            report.rejected.append({"type": str(cand.type), "reason": reason, "content": cand.content[:120]})
            continue
        mem, created = await memory.store(type_=cand.type, content=cand.content, title=cand.title, source=cand.source,
                                          task_id=task.id, project_id=task.project_id, agent_id=cand.agent_id,
                                          tags=cand.tags, metadata=cand.metadata, confidence=cand.confidence,
                                          importance=cand.importance)
        (report.stored if created else report.updated).append(str(mem.id))
    await journal.record(session, task.id, EventType.MEMORY_CREATED,
                         f"Memory consolidation: {len(report.stored)} stored, {len(report.updated)} reinforced, "
                         f"{len(report.rejected)} rejected", stored=report.stored, updated=report.updated,
                         rejected=report.rejected)
    return report
