"""Learning Engine - controlled feedback learning:

Feedback -> parser -> LearningCandidate -> validation -> (auto-)approval -> Lesson / Skill ->
PROCEDURAL or SEMANTIC memory -> retrieved for future similar tasks.

Safety: learning only ever writes memory, lessons and skills (retrieval/planning guidance). It never
changes source code, security configuration, agent core instructions or the database schema.
Candidates touching those areas are flagged and always require human review.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.enums import (
    EventType,
    FeedbackRating,
    LearningCategory,
    LearningStatus,
    MemoryStatus,
    MemoryType,
)
from app.core.errors import ConflictError, NotFoundError
from app.integrations.omniroute.client import ModelClient
from app.learning.parser import parse_feedback
from app.memory.service import MemoryService
from app.models import AgentDefinition, Feedback, LearningCandidate, Lesson, Memory, Skill, Task
from app.services import journal

DEPRECATE_MIN_USES = 3
DEPRECATE_MAX_SUCCESS = 0.34


class LearningEngine:
    def __init__(self, session: AsyncSession, memory: MemoryService, model: ModelClient | None = None):
        self.session = session
        self.memory = memory
        self.model = model
        self.settings = get_settings()

    # ------------------------------------------------------------------------------ feedback intake
    async def process_feedback(self, feedback: Feedback) -> list[LearningCandidate]:
        task = await self.session.get(Task, feedback.task_id)
        if task is None:
            raise NotFoundError(f"Task {feedback.task_id} not found")
        parsed = await parse_feedback(feedback.content, FeedbackRating(feedback.rating)
                                      if feedback.rating != "UNSET" else None, task.original_request, self.model)
        feedback.rating = str(parsed.rating)
        await journal.record(self.session, task.id, EventType.FEEDBACK_RECEIVED,
                             f"Feedback ({parsed.rating}): {feedback.content[:200]}", feedback_id=str(feedback.id),
                             rating=str(parsed.rating), parser=parsed.parser)
        await self._update_outcome_stats(task, parsed.rating)

        agent_ids = [s.agent_id for s in task.steps] or ([task.selected_agent] if task.selected_agent else [])
        base_tags: set[str] = set(agent_ids)
        for aid in agent_ids:
            a = await self.session.get(AgentDefinition, aid)
            if a:
                base_tags.update((a.memory_policy or {}).get("tags", []))

        created: list[LearningCandidate] = []
        for lesson in parsed.lessons:
            cand = LearningCandidate(
                task_id=task.id, feedback_id=feedback.id, project_id=task.project_id,
                agent_id=agent_ids[0] if agent_ids else None, original_feedback=feedback.content,
                normalized_lesson=lesson.text, category=str(lesson.category),
                domain_tags=sorted(base_tags | set(lesson.domain_tags)), confidence=lesson.confidence,
                status=LearningStatus.CANDIDATE, requires_approval=True)
            self.session.add(cand)
            await self.session.flush()
            await self._validate(cand, sensitive=lesson.sensitive)
            await journal.record(self.session, task.id, EventType.LEARNING_CANDIDATE,
                                 f"Learning candidate ({cand.category}, {cand.status}): {cand.normalized_lesson[:200]}",
                                 candidate_id=str(cand.id), confidence=cand.confidence, status=cand.status)
            created.append(cand)
        feedback.processed = True
        await self.session.flush()
        return created

    async def _validate(self, cand: LearningCandidate, *, sensitive: bool) -> None:
        notes = []
        # duplicate of an existing lesson? -> reinforce it instead of creating a second copy
        dup = None
        for mtype in (MemoryType.PROCEDURAL, MemoryType.SEMANTIC):
            dup = dup or await self.memory.find_duplicate(cand.normalized_lesson, mtype, cand.project_id)
        if dup is None:
            hits = await self.memory.search(cand.normalized_lesson, project_id=cand.project_id,
                                            types=[MemoryType.PROCEDURAL, MemoryType.SEMANTIC], limit=1,
                                            min_score=0.0, touch=False)
            dup = hits[0].memory if hits and hits[0].similarity >= 0.9 else None
        if dup is not None:
            dup.confidence = min(1.0, dup.confidence + 0.05)
            cand.status = LearningStatus.REJECTED
            cand.validation_notes = f"Duplicate of existing memory {dup.id}; reinforced it instead."
            cand.validated_at = datetime.now(UTC)
            return
        if sensitive:
            cand.requires_approval = True
            notes.append("Touches a protected area (code/security/instructions/schema). Learning can only store "
                         "it as guidance; human review required.")
        if len(cand.normalized_lesson) > 1000:
            notes.append("Very long lesson; consider editing before approval.")
        auto = (not sensitive and cand.confidence >= self.settings.learning_auto_approve_threshold
                and cand.category in (LearningCategory.PROCEDURE, LearningCategory.PREFERENCE,
                                      LearningCategory.ANTI_PATTERN))
        cand.validation_notes = " ".join(notes) or ("Auto-approved (high confidence)." if auto
                                                    else "Awaiting human approval.")
        if auto:
            cand.requires_approval = False
            await self.approve(cand.id, decided_by="learning-engine:auto")

    async def _update_outcome_stats(self, task: Task, rating: FeedbackRating) -> None:
        """Outcome feedback updates success/failure statistics of the lessons that were applied."""
        if rating == FeedbackRating.NEUTRAL:
            return
        used = [uuid.UUID(m) for m in (task.session or {}).get("applied_lesson_memory_ids", [])]
        if not used:
            return
        lessons = (await self.session.execute(select(Lesson).where(Lesson.memory_id.in_(used)))).scalars().all()
        for lesson in lessons:
            if rating == FeedbackRating.POSITIVE:
                lesson.success_count += 1
            else:
                lesson.failure_count += 1
            if lesson.candidate_id:
                cand = await self.session.get(LearningCandidate, lesson.candidate_id)
                if cand:
                    cand.success_count, cand.failure_count = lesson.success_count, lesson.failure_count
            total = lesson.success_count + lesson.failure_count
            if total >= DEPRECATE_MIN_USES and lesson.success_count / total < DEPRECATE_MAX_SUCCESS:
                await self.deprecate_lesson(lesson.id, reason="low success rate")

    # ------------------------------------------------------------------------------ decisions
    async def approve(self, candidate_id: uuid.UUID, *, decided_by: str, edited_lesson: str | None = None) -> Lesson:
        cand = await self.session.get(LearningCandidate, candidate_id)
        if cand is None:
            raise NotFoundError("Learning candidate not found")
        if cand.status != LearningStatus.CANDIDATE:
            raise ConflictError(f"Candidate is already {cand.status}")
        if edited_lesson:
            cand.normalized_lesson = edited_lesson.strip()
        mtype = MemoryType.PROCEDURAL if cand.category in (LearningCategory.PROCEDURE, LearningCategory.ANTI_PATTERN) \
            else MemoryType.SEMANTIC
        title = ("Avoid: " if cand.category == LearningCategory.ANTI_PATTERN else "Lesson: ") + \
            cand.normalized_lesson[:120]
        mem, _ = await self.memory.store(type_=mtype, content=cand.normalized_lesson, title=title, source="learning",
                                         task_id=cand.task_id, project_id=cand.project_id, agent_id=None,
                                         tags=sorted(set(cand.domain_tags) | {"lesson"}),
                                         metadata={"candidate_id": str(cand.id), "category": cand.category,
                                                   "approved_by": decided_by},
                                         confidence=max(cand.confidence, 0.8), importance=0.85)
        lesson = Lesson(candidate_id=cand.id, memory_id=mem.id, project_id=cand.project_id, title=title,
                        content=cand.normalized_lesson, category=cand.category, domain_tags=cand.domain_tags,
                        status=LearningStatus.APPROVED)
        self.session.add(lesson)
        cand.status = LearningStatus.APPROVED
        cand.validated_at = datetime.now(UTC)
        await self.session.flush()
        if cand.category == LearningCategory.PROCEDURE:
            await self._merge_into_skill(lesson)
        await journal.audit(self.session, decided_by, "learning.approve", str(cand.id), lesson_id=str(lesson.id))
        return lesson

    async def reject(self, candidate_id: uuid.UUID, *, decided_by: str, reason: str | None = None) -> LearningCandidate:
        cand = await self.session.get(LearningCandidate, candidate_id)
        if cand is None:
            raise NotFoundError("Learning candidate not found")
        if cand.status != LearningStatus.CANDIDATE:
            raise ConflictError(f"Candidate is already {cand.status}")
        cand.status = LearningStatus.REJECTED
        cand.validated_at = datetime.now(UTC)
        cand.validation_notes = (cand.validation_notes or "") + f" Rejected by {decided_by}: {reason or '-'}"
        await journal.audit(self.session, decided_by, "learning.reject", str(cand.id), reason=reason)
        return cand

    async def deprecate_lesson(self, lesson_id: uuid.UUID, *, reason: str, decided_by: str = "learning-engine") -> Lesson:
        lesson = await self.session.get(Lesson, lesson_id)
        if lesson is None:
            raise NotFoundError("Lesson not found")
        lesson.status = LearningStatus.DEPRECATED
        if lesson.memory_id:
            mem = await self.session.get(Memory, lesson.memory_id)
            if mem:
                mem.status = MemoryStatus.DEPRECATED
        if lesson.candidate_id:
            cand = await self.session.get(LearningCandidate, lesson.candidate_id)
            if cand:
                cand.status = LearningStatus.DEPRECATED
        await journal.audit(self.session, decided_by, "learning.deprecate", str(lesson.id), reason=reason)
        return lesson

    async def _merge_into_skill(self, lesson: Lesson) -> Skill:
        """Approved procedures are grouped into a per-domain playbook skill."""
        domain = next((t for t in lesson.domain_tags if t not in ("lesson",)), "general")
        name = f"{domain}-playbook"
        skill = (await self.session.execute(select(Skill).where(Skill.name == name))).scalar_one_or_none()
        if skill is None:
            skill = Skill(name=name, description=f"Learned procedures for {domain} tasks",
                          domain_tags=lesson.domain_tags, steps=[], source_lesson_ids=[], status="APPROVED", version=0)
            self.session.add(skill)
        if lesson.content not in (skill.steps or []):
            skill.steps = [*(skill.steps or []), lesson.content]
            skill.source_lesson_ids = [*(skill.source_lesson_ids or []), str(lesson.id)]
            skill.version = (skill.version or 0) + 1
            skill.domain_tags = sorted(set(skill.domain_tags or []) | set(lesson.domain_tags))
        await self.session.flush()
        return skill
