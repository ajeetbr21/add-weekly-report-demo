"""Long-term memory store and retrieval (PostgreSQL + pgvector).

Retrieval pipeline:  query -> embedding -> pgvector ANN candidates (filtered by status, type, project
scope, agent scope) -> re-ranking (similarity, importance, confidence, recency, usage) -> top-k.
"""

from __future__ import annotations

import hashlib
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.enums import MemoryStatus, MemoryType
from app.memory.embeddings import HASH_MODEL, Embedder, hash_embed
from app.models import Memory, MemoryEmbedding


def content_hash(text: str) -> str:
    norm = " ".join(text.lower().split())
    return hashlib.sha256(norm.encode()).hexdigest()


@dataclass
class ScoredMemory:
    memory: Memory
    score: float
    similarity: float


# Relative weights of the ranking signals
W_SIM, W_IMPORTANCE, W_CONFIDENCE, W_RECENCY, W_USAGE = 0.62, 0.14, 0.10, 0.09, 0.05
# Memory types considered more actionable for planning get a small boost
TYPE_BOOST = {MemoryType.PROCEDURAL: 0.06, MemoryType.CORE: 0.05, MemoryType.SEMANTIC: 0.02}


class MemoryService:
    def __init__(self, session: AsyncSession, embedder: Embedder | None = None):
        self.session = session
        self.embedder = embedder or Embedder()
        self.dim = get_settings().embedding_dimensions

    # ------------------------------------------------------------------------------ write
    async def find_duplicate(self, text: str, type_: str, project_id: uuid.UUID | None) -> Memory | None:
        stmt = select(Memory).where(Memory.content_hash == content_hash(text), Memory.type == type_,
                                    Memory.status == MemoryStatus.ACTIVE)
        stmt = stmt.where(Memory.project_id == project_id) if project_id else stmt.where(Memory.project_id.is_(None))
        return (await self.session.execute(stmt.limit(1))).scalar_one_or_none()

    async def store(self, *, type_: MemoryType | str, content: str, source: str, title: str | None = None,
                    task_id: str | None = None, project_id: uuid.UUID | None = None, agent_id: str | None = None,
                    tags: list[str] | None = None, metadata: dict[str, Any] | None = None,
                    confidence: float = 0.6, importance: float = 0.5) -> tuple[Memory, bool]:
        """Store a memory (deduplicated by normalized content hash within scope). Returns (memory, created)."""
        dup = await self.find_duplicate(content, str(type_), project_id)
        if dup is not None:
            dup.confidence = min(1.0, max(dup.confidence, confidence) + 0.02)
            dup.importance = max(dup.importance, importance)
            dup.tags = sorted(set(dup.tags or []) | set(tags or []))
            return dup, False
        mem = Memory(type=str(type_), content=content, content_hash=content_hash(content), source=source,
                     title=title, task_id=task_id, project_id=project_id, agent_id=agent_id,
                     tags=sorted(set(tags or [])), extra=metadata or {}, confidence=confidence,
                     importance=importance, status=MemoryStatus.ACTIVE)
        self.session.add(mem)
        await self.session.flush()
        await self.embed_memory(mem)
        return mem, True

    def _embedding_text(self, mem: Memory) -> str:
        return " ".join(filter(None, [mem.title, mem.content, " ".join(mem.tags or [])]))

    async def embed_memory(self, mem: Memory) -> None:
        text = self._embedding_text(mem)
        # Always store the local lexical embedding so retrieval works even if the gateway is down,
        # plus the gateway embedding when configured.
        self.session.add(MemoryEmbedding(memory_id=mem.id, model=HASH_MODEL, embedding=hash_embed(text, self.dim)))
        if self.embedder.model_name != HASH_MODEL:
            model, vec = await self.embedder.embed_one(text)
            if model != HASH_MODEL:
                self.session.add(MemoryEmbedding(memory_id=mem.id, model=model, embedding=vec))
        await self.session.flush()

    # ------------------------------------------------------------------------------ read
    async def search(self, query: str, *, project_id: uuid.UUID | None = None, types: list[str] | None = None,
                     agent_id: str | None = None, tags: list[str] | None = None, limit: int = 6,
                     min_score: float | None = None, include_global: bool = True,
                     touch: bool = True) -> list[ScoredMemory]:
        model, qvec = await self.embedder.embed_one(query)
        s = get_settings()
        results = await self._search_with(model, qvec, project_id=project_id, types=types, agent_id=agent_id,
                                          tags=tags, limit=limit, include_global=include_global)
        threshold = (s.memory_min_score_hash if model == HASH_MODEL else s.memory_min_score) \
            if min_score is None else min_score
        results = [r for r in results if r.similarity >= threshold]
        if model != HASH_MODEL and not results:
            # the gateway embedding space may not cover older memories -> lexical fallback
            results = await self._search_with(HASH_MODEL, hash_embed(query, self.dim), project_id=project_id,
                                              types=types, agent_id=agent_id, tags=tags, limit=limit,
                                              include_global=include_global)
            threshold = s.memory_min_score_hash if min_score is None else min_score
            results = [r for r in results if r.similarity >= threshold]
        if touch and results:
            ids = [r.memory.id for r in results]
            await self.session.execute(update(Memory).where(Memory.id.in_(ids)).values(
                usage_count=Memory.usage_count + 1, last_accessed_at=datetime.now(UTC)))
        return results

    async def _search_with(self, model: str, qvec: list[float], *, project_id, types, agent_id, tags, limit,
                           include_global) -> list[ScoredMemory]:
        distance = MemoryEmbedding.embedding.cosine_distance(qvec).label("distance")
        conds = [MemoryEmbedding.model == model, Memory.status == MemoryStatus.ACTIVE]
        if types:
            conds.append(Memory.type.in_(types))
        if project_id is not None:
            # project boundary: this project's memories (+ global memories with no project)
            conds.append(or_(Memory.project_id == project_id, Memory.project_id.is_(None)) if include_global
                         else Memory.project_id == project_id)
        if agent_id:
            conds.append(or_(Memory.agent_id == agent_id, Memory.agent_id.is_(None)))
        if tags:
            conds.append(Memory.tags.op("?|")(tags))
        stmt = (select(Memory, distance).join(MemoryEmbedding, MemoryEmbedding.memory_id == Memory.id)
                .where(and_(*conds)).order_by(distance).limit(max(limit * 4, 20)))
        rows = (await self.session.execute(stmt)).all()
        now = datetime.now(UTC)
        scored: list[ScoredMemory] = []
        for mem, dist in rows:
            sim = max(0.0, 1.0 - float(dist))
            age_days = max(0.0, (now - (mem.updated_at or mem.created_at)).total_seconds() / 86400)
            recency = math.exp(-age_days / 30.0)
            usage = min(1.0, math.log1p(mem.usage_count) / 3.0)
            score = (W_SIM * sim + W_IMPORTANCE * mem.importance + W_CONFIDENCE * mem.confidence +
                     W_RECENCY * recency + W_USAGE * usage + TYPE_BOOST.get(MemoryType(mem.type), 0.0))
            if project_id is not None and mem.project_id == project_id:
                score += 0.03
            scored.append(ScoredMemory(memory=mem, score=round(score, 4), similarity=round(sim, 4)))
        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:limit]

    async def list(self, *, type_: str | None = None, project_id: uuid.UUID | None = None,
                   status: str | None = None, q: str | None = None, limit: int = 50,
                   offset: int = 0) -> tuple[list[Memory], int]:
        conds = []
        if type_:
            conds.append(Memory.type == type_)
        if project_id:
            conds.append(Memory.project_id == project_id)
        if status:
            conds.append(Memory.status == status)
        if q:
            conds.append(Memory.content.ilike(f"%{q}%"))
        stmt = select(Memory).order_by(Memory.created_at.desc()).limit(limit).offset(offset)
        cstmt = select(func.count()).select_from(Memory)
        if conds:
            stmt, cstmt = stmt.where(and_(*conds)), cstmt.where(and_(*conds))
        return list((await self.session.execute(stmt)).scalars()), (await self.session.execute(cstmt)).scalar_one()

    async def stats(self) -> dict[str, int]:
        rows = (await self.session.execute(select(Memory.type, func.count()).where(
            Memory.status == MemoryStatus.ACTIVE).group_by(Memory.type))).all()
        return {t: c for t, c in rows}
