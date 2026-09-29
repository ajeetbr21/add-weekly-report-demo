"""Agent Registry: dynamic discovery + capability-based routing of agents stored in the `agents` table."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.definitions import BUILTIN_AGENTS, FALLBACK_AGENT_ID, AgentSpec
from app.core.errors import NotFoundError
from app.models import AgentDefinition

_TOKEN = re.compile(r"[a-z0-9]+")
_URL = re.compile(r"https?://\S+")
# paths and file names ("notes/restart-demo.txt", "main.py") are objects of the request, not intent words
_PATHLIKE = re.compile(r"[`'\"]?[\w.\-]*(?:/[\w.\-]+)+/?[`'\"]?|[`'\"]?[\w\-]+\.[A-Za-z0-9]{1,5}\b[`'\"]?")


def strip_literals(text: str) -> str:
    """Remove URLs, paths and file names before keyword routing."""
    return _PATHLIKE.sub(" ", _URL.sub(" ", text))


@dataclass
class RouteScore:
    agent_id: str
    score: float
    matched: list[str]


async def sync_builtin_agents(session: AsyncSession) -> list[str]:
    """Insert built-in agents that are missing. Existing rows (possibly operator-edited) are left alone.
    Uses INSERT ... ON CONFLICT DO NOTHING so the API and the worker can seed concurrently."""
    created = []
    now = datetime.now(UTC)
    for spec in BUILTIN_AGENTS:
        stmt = pg_insert(AgentDefinition).values(
            id=spec.id, name=spec.name, description=spec.description, capabilities=spec.capabilities,
            keywords=spec.keywords, tools=spec.tools, instructions=spec.instructions,
            model_preference=spec.model_preference, memory_policy=spec.memory_policy, status="ACTIVE",
            created_at=now, updated_at=now,
        ).on_conflict_do_nothing(index_elements=[AgentDefinition.id]).returning(AgentDefinition.id)
        if (await session.execute(stmt)).scalar_one_or_none():
            created.append(spec.id)
    return created


def to_row(spec: AgentSpec) -> AgentDefinition:
    return AgentDefinition(id=spec.id, name=spec.name, description=spec.description,
                           capabilities=spec.capabilities, keywords=spec.keywords, tools=spec.tools,
                           instructions=spec.instructions, model_preference=spec.model_preference,
                           memory_policy=spec.memory_policy, status="ACTIVE")


class AgentRegistry:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list(self, include_disabled: bool = False) -> list[AgentDefinition]:
        stmt = select(AgentDefinition).order_by(AgentDefinition.id)
        if not include_disabled:
            stmt = stmt.where(AgentDefinition.status == "ACTIVE")
        return list((await self.session.execute(stmt)).scalars())

    async def get(self, agent_id: str) -> AgentDefinition:
        row = await self.session.get(AgentDefinition, agent_id)
        if row is None:
            raise NotFoundError(f"Agent '{agent_id}' not found")
        return row

    async def route(self, request: str, agents: list[AgentDefinition] | None = None) -> list[RouteScore]:
        """Score every active agent against the request (keyword + capability overlap)."""
        agents = agents if agents is not None else await self.list()
        toks = _TOKEN.findall(strip_literals(request).lower())
        tokset = set(toks)
        scores = []
        for a in agents:
            kw = {k.lower() for k in (a.keywords or [])}
            cap_words = {w for c in (a.capabilities or []) for w in _TOKEN.findall(c.lower()) if len(w) > 3}
            matched = sorted((tokset & kw) | (tokset & cap_words))
            # strong domain words count more than generic ones
            score = sum(1.0 if len(m) > 3 else 0.6 for m in (tokset & kw)) + 0.5 * len(tokset & cap_words - kw)
            if a.id == FALLBACK_AGENT_ID:
                score = score * 0.6 + 0.3  # generalist: small base score, weaker keywords
            scores.append(RouteScore(agent_id=a.id, score=round(score, 3), matched=matched))
        # highest score wins; on a tie a specialist beats the generalist
        scores.sort(key=lambda s: (s.score, s.agent_id != FALLBACK_AGENT_ID), reverse=True)
        return scores
