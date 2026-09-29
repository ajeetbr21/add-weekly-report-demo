"""Task Planner: decides simple vs complex and produces an ordered multi-agent plan.

LLM planning (reasoning tier via OmniRoute) is used when available; otherwise a deterministic
capability router is used. Plans are validated: only registered active agents, <= MAX_STEPS steps,
dependencies must point backwards (no loops, no agent-to-agent recursion).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.agents.definitions import FALLBACK_AGENT_ID
from app.agents.registry import AgentRegistry, RouteScore
from app.integrations.omniroute.client import ModelClient
from app.memory.context import MemoryItem
from app.models import AgentDefinition

MAX_STEPS = 5
# execution order preference when several specialists are involved: gather facts first, research next
ORDER = {"aws_devops": 0, "coding": 1, "research": 2, "general": 3}
_MULTI = re.compile(r"\b(and then|then|and also|also|as well as|plus|after that|and research|and prepare|"
                    r"and write|and create|and draft|and document)\b", re.I)
_REPORT = re.compile(r"\b(report|write[- ]?up|post[- ]?mortem|summary document|briefing)\b", re.I)


@dataclass
class PlanStep:
    agent_id: str
    objective: str
    depends_on: list[int] = field(default_factory=list)


@dataclass
class Plan:
    complexity: str
    steps: list[PlanStep]
    needs_report: bool
    rationale: str
    planner: str  # llm | heuristic
    guidance: list[str] = field(default_factory=list)
    routing: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _objective_for(agent: AgentDefinition, request: str, multi: bool) -> str:
    if not multi:
        return request
    focus = {
        "aws_devops": "Investigate the AWS/infrastructure side (resources, alarms, metrics, recent events)",
        "research": "Research relevant documentation and best practices, with sources",
        "coding": "Analyze the code/repository side (code, tests, GitHub)",
        "general": "Coordinate remaining actions and produce the requested output",
    }.get(agent.id, f"Handle the part matching: {', '.join(agent.capabilities[:3])}")
    return f"{focus} for this request: {request}"


def heuristic_plan(request: str, agents: list[AgentDefinition], scores: list[RouteScore],
                   guidance: list[str]) -> Plan:
    by_id = {a.id: a for a in agents}
    specialists = [s for s in scores if s.agent_id != FALLBACK_AGENT_ID and s.score >= 1.0]
    wants_multi = bool(_MULTI.search(request)) or len(specialists) >= 2 and specialists[1].score >= 1.5
    needs_report = bool(_REPORT.search(request))
    routing = [{"agent_id": s.agent_id, "score": s.score, "matched": s.matched} for s in scores]
    if wants_multi and len(specialists) >= 2:
        chosen = sorted(specialists[:3], key=lambda s: ORDER.get(s.agent_id, 9))
        steps = [PlanStep(agent_id=s.agent_id, objective=_objective_for(by_id[s.agent_id], request, True),
                          depends_on=list(range(i))) for i, s in enumerate(chosen)]
        return Plan(complexity="complex", steps=steps, needs_report=True, guidance=guidance, routing=routing,
                    rationale="Multiple domains detected: " + ", ".join(f"{s.agent_id}({'/'.join(s.matched[:4])})"
                                                                        for s in chosen), planner="heuristic")
    # the top-scored agent wins; a specialist needs a real match (>= 1.0), otherwise the generalist handles it
    top = scores[0]
    if top.agent_id != FALLBACK_AGENT_ID and top.score >= 1.0:
        best = top.agent_id
    elif FALLBACK_AGENT_ID in by_id:
        best = FALLBACK_AGENT_ID
    else:
        best = top.agent_id
    matched = next((s.matched for s in scores if s.agent_id == best), [])
    return Plan(complexity="simple", steps=[PlanStep(agent_id=best, objective=request)], needs_report=needs_report,
                rationale=f"Best capability match: {best}" + (f" ({', '.join(matched[:5])})" if matched
                                                              else " (no keyword matched)"),
                planner="heuristic", guidance=guidance, routing=routing)


PLANNER_PROMPT = """You are the planner of Atlas, a multi-agent platform. Decide whether the request is
simple (one agent) or complex (several agents in sequence) and produce a plan.
Rules: use only the listed agent ids; at most {max_steps} steps; each step has one agent and a concrete
objective; depends_on lists indexes of earlier steps only; do not plan loops; prefer read-only
investigation before any change; take the learned procedures into account.
Reply with ONLY JSON: {{"complexity": "simple"|"complex", "needs_report": bool, "rationale": str,
"steps": [{{"agent_id": str, "objective": str, "depends_on": [int]}}]}}"""


class Planner:
    def __init__(self, registry: AgentRegistry, model: ModelClient):
        self.registry = registry
        self.model = model

    async def plan(self, request: str, memories: list[MemoryItem]) -> tuple[Plan, dict[str, Any]]:
        agents = await self.registry.list()
        scores = await self.registry.route(request, agents)
        guidance = [f"Learned procedure: {m.content[:400]}" for m in memories if m.type == "PROCEDURAL"][:5]
        agents_desc = [{"id": a.id, "name": a.name, "description": a.description, "capabilities": a.capabilities}
                       for a in agents]
        messages = [
            {"role": "system", "content": PLANNER_PROMPT.format(max_steps=MAX_STEPS)},
            {"role": "user", "content": json.dumps({
                "request": request, "agents": agents_desc,
                "routing_scores": [{"agent_id": s.agent_id, "score": s.score} for s in scores],
                "learned_procedures": guidance,
                "related_memory": [m.content[:300] for m in memories if m.type != "PROCEDURAL"][:4]})},
        ]
        data, resp = await self.model.chat_json(messages, tier="reasoning")
        meta = {"model": resp.model, "provider": resp.provider, "offline": resp.offline,
                "fallback_reason": resp.fallback_reason}
        if data is not None:
            plan = self._validate(data, agents, request, guidance, scores)
            if plan is not None:
                return plan, meta
            meta["llm_plan_rejected"] = True
        return heuristic_plan(request, agents, scores, guidance), meta

    @staticmethod
    def _validate(data: Any, agents: list[AgentDefinition], request: str, guidance: list[str],
                  scores: list[RouteScore]) -> Plan | None:
        ids = {a.id for a in agents}
        raw_steps = data.get("steps") if isinstance(data, dict) else None
        if not isinstance(raw_steps, list) or not raw_steps:
            return None
        steps: list[PlanStep] = []
        for i, st in enumerate(raw_steps[:MAX_STEPS]):
            if not isinstance(st, dict) or st.get("agent_id") not in ids or not str(st.get("objective", "")).strip():
                return None
            deps = [d for d in st.get("depends_on") or [] if isinstance(d, int) and 0 <= d < i]
            steps.append(PlanStep(agent_id=st["agent_id"], objective=str(st["objective"])[:2000], depends_on=deps))
        return Plan(complexity="complex" if len(steps) > 1 else "simple", steps=steps,
                    needs_report=bool(data.get("needs_report")) or len(steps) > 1,
                    rationale=str(data.get("rationale", ""))[:1000], planner="llm", guidance=guidance,
                    routing=[{"agent_id": s.agent_id, "score": s.score, "matched": s.matched} for s in scores])
