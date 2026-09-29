from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import FeedbackRating
from app.core.errors import NotFoundError
from app.database.session import get_db
from app.integrations.omniroute.client import get_model_client
from app.learning.engine import LearningEngine
from app.memory.service import MemoryService
from app.models import Feedback, LearningCandidate, Lesson, Memory, Skill
from app.schemas.api import (
    FeedbackCreate,
    FeedbackOut,
    FeedbackResult,
    LearningCandidateOut,
    LearningDecision,
    LessonOut,
    MemoryCreate,
    MemoryOut,
    MemorySearchHit,
    MemorySearchRequest,
    MemoryUpdate,
    SkillCreate,
    SkillOut,
)
from app.schemas.common import Page
from app.services import journal
from app.tasks import service as task_service

memory_router = APIRouter(prefix="/api/memory", tags=["memory"])
feedback_router = APIRouter(prefix="/api/feedback", tags=["feedback"])
learning_router = APIRouter(prefix="/api/learning", tags=["learning"])


async def _project_id(db: AsyncSession, slug: str | None) -> uuid.UUID | None:
    if not slug:
        return None
    proj = await task_service.get_project(db, slug)
    if proj is None:
        raise NotFoundError(f"Project '{slug}' not found")
    return proj.id


# ------------------------------------------------------------------------------------------ memory
@memory_router.get("", response_model=Page[MemoryOut])
async def list_memories(type: str | None = None, project: str | None = None, status_: str | None = Query(
        default=None, alias="status"), q: str | None = None, limit: int = Query(default=50, le=200), offset: int = 0,
        db: AsyncSession = Depends(get_db)) -> Page[MemoryOut]:
    items, total = await MemoryService(db).list(type_=type, project_id=await _project_id(db, project),
                                                status=status_, q=q, limit=limit, offset=offset)
    return Page(items=[MemoryOut.model_validate(m) for m in items], total=total, limit=limit, offset=offset)


@memory_router.post("", response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
async def create_memory(body: MemoryCreate, db: AsyncSession = Depends(get_db)) -> MemoryOut:
    """Explicitly add knowledge (e.g. a CORE fact or a known configuration)."""
    mem, _ = await MemoryService(db).store(type_=body.type, content=body.content, title=body.title, source="manual",
                                           project_id=await _project_id(db, body.project), agent_id=body.agent_id,
                                           tags=body.tags, metadata=body.metadata, confidence=body.confidence,
                                           importance=body.importance)
    await journal.audit(db, "operator", "memory.create", str(mem.id), type=body.type)
    return MemoryOut.model_validate(mem)


@memory_router.get("/stats")
async def memory_stats(db: AsyncSession = Depends(get_db)) -> dict:
    return await MemoryService(db).stats()


@memory_router.post("/search", response_model=list[MemorySearchHit])
async def search_memory(body: MemorySearchRequest, db: AsyncSession = Depends(get_db)) -> list[MemorySearchHit]:
    hits = await MemoryService(db).search(body.query, project_id=await _project_id(db, body.project),
                                          types=body.types, agent_id=body.agent_id, limit=body.limit, min_score=0.0,
                                          touch=False)
    return [MemorySearchHit(memory=MemoryOut.model_validate(h.memory), score=h.score, similarity=h.similarity)
            for h in hits]


@memory_router.get("/{memory_id}", response_model=MemoryOut)
async def get_memory(memory_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> MemoryOut:
    mem = await db.get(Memory, memory_id)
    if mem is None:
        raise NotFoundError("Memory not found")
    return MemoryOut.model_validate(mem)


@memory_router.patch("/{memory_id}", response_model=MemoryOut)
async def update_memory(memory_id: uuid.UUID, body: MemoryUpdate, db: AsyncSession = Depends(get_db)) -> MemoryOut:
    mem = await db.get(Memory, memory_id)
    if mem is None:
        raise NotFoundError("Memory not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(mem, k, v)
    await journal.audit(db, "operator", "memory.update", str(mem.id), **body.model_dump(exclude_none=True))
    await db.flush()
    return MemoryOut.model_validate(mem)


# ------------------------------------------------------------------------------------------ feedback
@feedback_router.post("", response_model=FeedbackResult, status_code=status.HTTP_201_CREATED)
async def submit_feedback(body: FeedbackCreate, db: AsyncSession = Depends(get_db)) -> FeedbackResult:
    """Store feedback for a task and run it through the learning pipeline."""
    await task_service.get_task(db, body.task_id)
    user = await task_service.get_or_create_user(db, body.channel, body.user_external_id)
    fb = Feedback(task_id=body.task_id, user_id=user.id if user else None, channel=body.channel,
                  rating=str(body.rating) if body.rating else "UNSET", content=body.content)
    db.add(fb)
    await db.flush()
    cands = await LearningEngine(db, MemoryService(db), get_model_client()).process_feedback(fb)
    return FeedbackResult(feedback=FeedbackOut.model_validate(fb),
                          candidates=[LearningCandidateOut.model_validate(c) for c in cands])


@feedback_router.get("", response_model=list[FeedbackOut])
async def list_feedback(task_id: str | None = None, limit: int = Query(default=50, le=200),
                        db: AsyncSession = Depends(get_db)) -> list[FeedbackOut]:
    stmt = select(Feedback).order_by(Feedback.created_at.desc()).limit(limit)
    if task_id:
        stmt = stmt.where(Feedback.task_id == task_id)
    return [FeedbackOut.model_validate(f) for f in (await db.execute(stmt)).scalars()]


# ------------------------------------------------------------------------------------------ learning
@learning_router.get("/candidates", response_model=Page[LearningCandidateOut])
async def list_candidates(status_: str | None = Query(default=None, alias="status"),
                          limit: int = Query(default=50, le=200), offset: int = 0,
                          db: AsyncSession = Depends(get_db)) -> Page[LearningCandidateOut]:
    stmt = select(LearningCandidate).order_by(LearningCandidate.created_at.desc())
    cstmt = select(func.count()).select_from(LearningCandidate)
    if status_:
        stmt, cstmt = stmt.where(LearningCandidate.status == status_), cstmt.where(LearningCandidate.status == status_)
    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars()
    return Page(items=[LearningCandidateOut.model_validate(r) for r in rows],
                total=(await db.execute(cstmt)).scalar_one(), limit=limit, offset=offset)


@learning_router.post("/candidates/{candidate_id}/decision", response_model=LearningCandidateOut)
async def decide_candidate(candidate_id: uuid.UUID, body: LearningDecision,
                           db: AsyncSession = Depends(get_db)) -> LearningCandidateOut:
    engine = LearningEngine(db, MemoryService(db), get_model_client())
    if body.approve:
        await engine.approve(candidate_id, decided_by=body.decided_by, edited_lesson=body.edited_lesson)
    else:
        await engine.reject(candidate_id, decided_by=body.decided_by, reason=body.reason)
    cand = await db.get(LearningCandidate, candidate_id)
    return LearningCandidateOut.model_validate(cand)


@learning_router.get("/lessons", response_model=list[LessonOut])
async def list_lessons(status_: str | None = Query(default=None, alias="status"),
                       db: AsyncSession = Depends(get_db)) -> list[LessonOut]:
    stmt = select(Lesson).order_by(Lesson.created_at.desc()).limit(200)
    if status_:
        stmt = stmt.where(Lesson.status == status_)
    return [LessonOut.model_validate(r) for r in (await db.execute(stmt)).scalars()]


@learning_router.post("/lessons/{lesson_id}/deprecate", response_model=LessonOut)
async def deprecate_lesson(lesson_id: uuid.UUID, reason: str = "operator decision",
                           db: AsyncSession = Depends(get_db)) -> LessonOut:
    lesson = await LearningEngine(db, MemoryService(db)).deprecate_lesson(lesson_id, reason=reason,
                                                                        decided_by="operator")
    return LessonOut.model_validate(lesson)


@learning_router.get("/skills", response_model=list[SkillOut])
async def list_skills(db: AsyncSession = Depends(get_db)) -> list[SkillOut]:
    return [SkillOut.model_validate(s) for s in (await db.execute(select(Skill).order_by(Skill.name))).scalars()]


@learning_router.post("/skills", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
async def create_skill(body: SkillCreate, db: AsyncSession = Depends(get_db)) -> SkillOut:
    """Define a reusable procedure explicitly. It is also stored as PROCEDURAL memory for retrieval."""
    mem, _ = await MemoryService(db).store(
        type_="PROCEDURAL", title=f"Skill: {body.name}", source="skill",
        content=f"{body.description}\nSteps:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(body.steps)),
        tags=sorted(set(body.domain_tags) | {"skill"}), confidence=0.9, importance=0.8)
    skill = Skill(name=body.name, description=body.description, domain_tags=body.domain_tags, steps=body.steps,
                  source_lesson_ids=[str(x) for x in body.source_lesson_ids], memory_id=mem.id, status="APPROVED")
    db.add(skill)
    await db.flush()
    await journal.audit(db, "operator", "skill.create", str(skill.id), name=body.name)
    return SkillOut.model_validate(skill)


@learning_router.get("/summary")
async def learning_summary(db: AsyncSession = Depends(get_db)) -> dict:
    by_status = dict((await db.execute(select(LearningCandidate.status, func.count())
                                       .group_by(LearningCandidate.status))).all())
    lessons = (await db.execute(select(func.count()).select_from(Lesson).where(Lesson.status == "APPROVED"))
               ).scalar_one()
    skills = (await db.execute(select(func.count()).select_from(Skill))).scalar_one()
    return {"candidates": by_status, "approved_lessons": lessons, "skills": skills,
            "ratings": dict((await db.execute(select(Feedback.rating, func.count()).group_by(Feedback.rating))).all()),
            "rating_values": [r.value for r in FeedbackRating]}
