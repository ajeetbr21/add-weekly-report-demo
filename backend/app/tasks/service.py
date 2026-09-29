"""Central Task service: the single entry point every input source (web, API, Telegram, scheduler,
webhook) uses to create work. Also implements the Postgres-backed work queue (claim/lease/recover)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.enums import (
    IN_FLIGHT_STATUSES,
    RUNNABLE_STATUSES,
    ApprovalStatus,
    EventType,
    StepStatus,
    TaskStatus,
    ToolExecutionStatus,
)
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import correlation_id_var
from app.models import AgentRun, Approval, Project, Task, ToolExecution, User
from app.schemas.api import TaskCreate
from app.services import journal


def utcnow() -> datetime:
    return datetime.now(UTC)


async def next_task_id(session: AsyncSession) -> str:
    seq = (await session.execute(text("SELECT nextval('task_id_seq')"))).scalar_one()
    return f"TASK-{utcnow().year}-{int(seq):06d}"


async def get_or_create_user(session: AsyncSession, source: str, external_id: str | None,
                             display_name: str | None = None) -> User | None:
    if not external_id:
        return None
    user = (await session.execute(select(User).where(User.source == source, User.external_id == external_id))
            ).scalar_one_or_none()
    if user is None:
        user = User(source=source, external_id=external_id, display_name=display_name)
        session.add(user)
        await session.flush()
    return user


async def get_project(session: AsyncSession, slug: str) -> Project | None:
    return (await session.execute(select(Project).where(Project.slug == slug))).scalar_one_or_none()


async def ensure_default_project(session: AsyncSession) -> Project:
    slug = get_settings().default_project_slug
    proj = await get_project(session, slug)
    if proj is None:
        now = utcnow()
        # ON CONFLICT: API and worker may both create it at startup
        await session.execute(pg_insert(Project).values(
            id=uuid.uuid4(), slug=slug, name="Default", description="Default workspace", keywords=[],
            created_at=now, updated_at=now).on_conflict_do_nothing(index_elements=[Project.slug]))
        proj = await get_project(session, slug)
        assert proj is not None
    return proj


_WORD = re.compile(r"[a-z0-9]+")


async def detect_project(session: AsyncSession, request: str) -> Project:
    """Pick the project whose keywords best match the request; fall back to the default project."""
    words = set(_WORD.findall(request.lower()))
    best, best_score = None, 0
    for proj in (await session.execute(select(Project))).scalars():
        kws = {k.lower() for k in (proj.keywords or [])}
        score = len(words & kws)
        if score > best_score:
            best, best_score = proj, score
    return best or await ensure_default_project(session)


async def create_task(session: AsyncSession, data: TaskCreate) -> tuple[Task, bool]:
    """Create a task. Returns (task, created). Idempotent on `idempotency_key`."""
    if data.idempotency_key:
        existing = (await session.execute(select(Task).where(Task.idempotency_key == data.idempotency_key))
                    ).scalar_one_or_none()
        if existing:
            return existing, False

    if data.project:
        project = await get_project(session, data.project)
        if project is None:
            raise NotFoundError(f"Project '{data.project}' not found")
    else:
        project = await detect_project(session, data.request)

    if data.parent_task_id:
        if await session.get(Task, data.parent_task_id) is None:
            raise NotFoundError(f"Parent task {data.parent_task_id} not found")

    user = await get_or_create_user(session, str(data.source), data.user_external_id, data.user_display_name)
    task = Task(
        id=await next_task_id(session),
        parent_task_id=data.parent_task_id,
        project_id=project.id,
        source=str(data.source),
        user_id=user.id if user else None,
        session_key=data.session_key or (f"{data.source}:{data.user_external_id}" if data.user_external_id else None),
        original_request=data.request,
        status=TaskStatus.PENDING,
        priority=data.priority,
        idempotency_key=data.idempotency_key,
        correlation_id=correlation_id_var.get() or uuid.uuid4().hex[:16],
        max_retries=get_settings().task_max_retries,
        extra=data.metadata,
        session={},
    )
    try:
        async with session.begin_nested():
            session.add(task)
            await session.flush()
    except IntegrityError:
        # concurrent insert with the same idempotency key
        if data.idempotency_key:
            existing = (await session.execute(select(Task).where(Task.idempotency_key == data.idempotency_key))
                        ).scalar_one()
            return existing, False
        raise
    await journal.record(session, task.id, EventType.TASK_CREATED, f"Task created from {data.source}",
                         source=str(data.source), project=project.slug, priority=data.priority)
    return task, True


async def get_task(session: AsyncSession, task_id: str) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFoundError(f"Task {task_id} not found")
    return task


async def list_tasks(session: AsyncSession, *, status: str | None = None, source: str | None = None,
                     project_id: uuid.UUID | None = None, q: str | None = None, limit: int = 50,
                     offset: int = 0) -> tuple[list[Task], int]:
    conds = []
    if status:
        conds.append(Task.status.in_(status.split(",")))
    if source:
        conds.append(Task.source == source)
    if project_id:
        conds.append(Task.project_id == project_id)
    if q:
        conds.append(or_(Task.original_request.ilike(f"%{q}%"), Task.id.ilike(f"%{q}%")))
    where = and_(*conds) if conds else None
    stmt = select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset)
    cstmt = select(func.count()).select_from(Task)
    if where is not None:
        stmt, cstmt = stmt.where(where), cstmt.where(where)
    items = list((await session.execute(stmt)).scalars())
    total = (await session.execute(cstmt)).scalar_one()
    return items, total


async def lock_task(session: AsyncSession, task_id: str) -> Task:
    """Load a task with a row lock (SELECT ... FOR UPDATE) and fresh state. Every status transition that
    can race with another actor (worker vs. user cancel vs. approval) goes through this."""
    # FOR NO KEY UPDATE (not FOR UPDATE): it does not conflict with the FOR KEY SHARE locks that inserts into
    # task_events / tool_executions (foreign keys) hold while an agent loop is running, so a user can cancel
    # a running task immediately, while two status writers still serialize against each other.
    task = await session.get(Task, task_id, with_for_update={"key_share": True}, populate_existing=True)
    if task is None:
        raise NotFoundError(f"Task {task_id} not found")
    return task


CANCEL_ACTOR = "system:task-cancelled"


async def reject_pending_approvals(session: AsyncSession, task_id: str, reason: str) -> int:
    res = await session.execute(update(Approval).where(Approval.task_id == task_id,
                                                       Approval.status == ApprovalStatus.PENDING)
                                .values(status=ApprovalStatus.REJECTED, decided_at=utcnow(), decided_by=CANCEL_ACTOR,
                                        decision_channel="system", reason=reason))
    return res.rowcount or 0


async def mark_interrupted(session: AsyncSession, task_id: str, reason: str) -> dict[str, int]:
    """Agent runs / tool calls left RUNNING by a dead or failed attempt are marked INTERRUPTED."""
    tools = await session.execute(update(ToolExecution).where(
        ToolExecution.task_id == task_id, ToolExecution.status == ToolExecutionStatus.RUNNING)
        .values(status=ToolExecutionStatus.INTERRUPTED, error=reason))
    runs = await session.execute(update(AgentRun).where(AgentRun.task_id == task_id, AgentRun.status == "RUNNING")
                                 .values(status="INTERRUPTED", error=reason, completed_at=utcnow()))
    return {"tool_executions": tools.rowcount or 0, "agent_runs": runs.rowcount or 0}


async def cancel_task(session: AsyncSession, task_id: str, actor: str = "user") -> Task:
    task = await lock_task(session, task_id)
    if TaskStatus(task.status).is_terminal:
        raise ConflictError(f"Task {task_id} is already {task.status}")
    task.status = TaskStatus.CANCELLED
    task.completed_at = utcnow()
    task.locked_by = None
    task.lease_expires_at = None
    rejected = await reject_pending_approvals(session, task.id, f"task cancelled by {actor}")
    await journal.record(session, task.id, EventType.TASK_CANCELLED, f"Cancelled by {actor}",
                         pending_approvals_closed=rejected)
    return task


async def retry_task(session: AsyncSession, task_id: str) -> Task:
    """Manually re-queue a FAILED/CANCELLED task; completed steps are kept and skipped."""
    task = await lock_task(session, task_id)
    if task.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise ConflictError(f"Only FAILED or CANCELLED tasks can be retried (status={task.status})")
    task.status = TaskStatus.RETRYING
    task.error = None
    task.completed_at = None
    for step in task.steps:
        if step.status in (StepStatus.FAILED, StepStatus.RUNNING, StepStatus.WAITING_APPROVAL):
            step.status = StepStatus.PENDING
            step.attempts = 0
    # a manual retry is the human acknowledging interrupted (outcome-unknown) tool calls
    await session.execute(update(ToolExecution).where(
        ToolExecution.task_id == task.id, ToolExecution.status == ToolExecutionStatus.INTERRUPTED)
        .values(status=ToolExecutionStatus.ABANDONED))
    await journal.record(session, task.id, EventType.RETRY, "Manual retry requested")
    return task


# ------------------------------------------------------------------------------------------ queue
async def claim_next_task(session: AsyncSession, worker_id: str, lease_seconds: int) -> str | None:
    """Atomically claim the next runnable task using FOR UPDATE SKIP LOCKED."""
    sub = (select(Task.id).where(Task.status.in_([s.value for s in RUNNABLE_STATUSES]))
           .order_by(Task.priority.asc(), Task.created_at.asc()).limit(1)
           .with_for_update(skip_locked=True, key_share=True)
           .scalar_subquery())
    now = utcnow()
    stmt = (update(Task).where(Task.id == sub)
            .values(status=TaskStatus.PLANNING, locked_by=worker_id,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    started_at=func.coalesce(Task.started_at, now), updated_at=now)
            .returning(Task.id))
    task_id = (await session.execute(stmt)).scalar_one_or_none()
    if task_id:
        await journal.record(session, task_id, EventType.TASK_CLAIMED, f"Claimed by worker {worker_id}",
                             worker=worker_id)
    return task_id


async def heartbeat(session: AsyncSession, task_id: str, worker_id: str, lease_seconds: int) -> bool:
    res = await session.execute(update(Task).where(Task.id == task_id, Task.locked_by == worker_id)
                                .values(lease_expires_at=utcnow() + timedelta(seconds=lease_seconds)))
    return (res.rowcount or 0) > 0


async def recover_stale_tasks(session: AsyncSession, *, now: datetime | None = None,
                              recover_all_inflight: bool = False) -> list[str]:
    """Tasks whose worker died (lease expired) are re-queued, keeping completed steps + checkpoints.
    `recover_all_inflight` is used at worker startup when no other worker can own them (single worker)."""
    now = now or utcnow()
    cond = Task.status.in_([s.value for s in IN_FLIGHT_STATUSES])
    if not recover_all_inflight:
        cond = and_(cond, or_(Task.lease_expires_at.is_(None), Task.lease_expires_at < now))
    stale = list((await session.execute(select(Task).where(cond).with_for_update(skip_locked=True))).scalars())
    recovered: list[str] = []
    for task in stale:
        for step in task.steps:
            if step.status == StepStatus.RUNNING:
                step.status = StepStatus.PENDING  # last committed checkpoint is kept -> the step resumes from it
        interrupted = await mark_interrupted(session, task.id, "worker interrupted")
        task.locked_by = None
        task.lease_expires_at = None
        if task.retry_count >= task.max_retries:
            task.status = TaskStatus.FAILED
            task.error = "Worker interrupted and retry budget exhausted"
            task.completed_at = now
            await journal.record(session, task.id, EventType.TASK_FAILED, task.error)
        else:
            task.retry_count += 1
            task.status = TaskStatus.RETRYING
            await journal.record(session, task.id, EventType.RECOVERED,
                                 "Recovered unfinished task after worker interruption",
                                 retry_count=task.retry_count, interrupted=interrupted,
                                 completed_steps=[s.step_index for s in task.steps if s.status == "COMPLETED"])
        recovered.append(task.id)
    return recovered


async def release(session: AsyncSession, task: Task) -> None:
    task.locked_by = None
    task.lease_expires_at = None


def task_summary(task: Task) -> dict[str, Any]:
    return {"id": task.id, "status": task.status, "request": task.original_request[:200]}
