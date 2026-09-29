"""Persistent local scheduler. Schedules live in `scheduled_tasks`; the worker calls `tick()` periodically.
A firing schedule creates a *normal Task* (source=scheduler) through the task service - there is no
separate execution path. Duplicate firing is prevented by the idempotency key schedule:<id>:<fire time>
(unique on tasks.idempotency_key) plus row locking (FOR UPDATE SKIP LOCKED)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ScheduleType, TaskSource
from app.core.errors import AppError
from app.models import Project, ScheduledTask
from app.schemas.api import ScheduleCreate, TaskCreate
from app.services import journal
from app.tasks import service as task_service

logger = logging.getLogger("atlas.scheduler")


def cron_for(schedule_type: str, anchor: datetime | None, cron_expression: str | None) -> str | None:
    if schedule_type == ScheduleType.CRON:
        return cron_expression
    if schedule_type == ScheduleType.ONCE:
        return None
    a = anchor or datetime.now(UTC)
    if schedule_type == ScheduleType.DAILY:
        return f"{a.minute} {a.hour} * * *"
    if schedule_type == ScheduleType.WEEKLY:
        return f"{a.minute} {a.hour} * * {(a.weekday() + 1) % 7}"  # cron: 0=Sunday
    if schedule_type == ScheduleType.MONTHLY:
        return f"{a.minute} {a.hour} {a.day} * *"
    raise AppError(f"Unknown schedule type {schedule_type}")


def compute_next_run(sched: ScheduledTask, after: datetime) -> datetime | None:
    if sched.schedule_type == ScheduleType.ONCE:
        return sched.run_at if sched.run_count == 0 else None
    expr = sched.cron_expression
    if not expr:
        return None
    tz = ZoneInfo(sched.timezone or "UTC")
    nxt = croniter(expr, after.astimezone(tz)).get_next(datetime)
    return nxt.astimezone(UTC)


async def create_schedule(session: AsyncSession, data: ScheduleCreate) -> ScheduledTask:
    try:
        ZoneInfo(data.timezone)
    except Exception as exc:
        raise AppError(f"Unknown timezone {data.timezone}") from exc
    if data.schedule_type == ScheduleType.ONCE and not data.run_at:
        raise AppError("ONCE schedules require run_at")
    if data.schedule_type == ScheduleType.CRON and (not data.cron_expression or not croniter.is_valid(
            data.cron_expression)):
        raise AppError("CRON schedules require a valid cron_expression (5 fields)")
    anchor = data.run_at.astimezone(ZoneInfo(data.timezone)) if data.run_at else None
    project_id = None
    if data.project:
        proj = (await session.execute(select(Project).where(Project.slug == data.project))).scalar_one_or_none()
        if proj is None:
            raise AppError(f"Unknown project {data.project}")
        project_id = proj.id
    sched = ScheduledTask(name=data.name, request=data.request, schedule_type=str(data.schedule_type),
                          cron_expression=cron_for(data.schedule_type, anchor, data.cron_expression),
                          run_at=data.run_at.astimezone(UTC) if data.run_at else None, timezone=data.timezone,
                          enabled=data.enabled, project_id=project_id, run_count=0)
    sched.next_run_at = compute_next_run(sched, datetime.now(UTC)) if sched.schedule_type != ScheduleType.ONCE \
        else sched.run_at
    session.add(sched)
    await session.flush()
    return sched


async def tick(session: AsyncSession, now: datetime | None = None) -> list[str]:
    """Fire all due schedules. Returns created task ids."""
    now = now or datetime.now(UTC)
    due = list((await session.execute(
        select(ScheduledTask).where(ScheduledTask.enabled.is_(True), ScheduledTask.next_run_at.is_not(None),
                                    ScheduledTask.next_run_at <= now)
        .order_by(ScheduledTask.next_run_at).with_for_update(skip_locked=True).limit(20))).scalars())
    created: list[str] = []
    for sched in due:
        fire_at = sched.next_run_at
        assert fire_at is not None
        project_slug = None
        if sched.project_id:
            proj = await session.get(Project, sched.project_id)
            project_slug = proj.slug if proj else None
        task, was_created = await task_service.create_task(session, TaskCreate(
            request=sched.request, source=TaskSource.SCHEDULER, project=project_slug, priority=5,
            idempotency_key=f"schedule:{sched.id}:{fire_at.isoformat()}",
            metadata={"schedule_id": str(sched.id), "schedule_name": sched.name, "fire_at": fire_at.isoformat()}))
        sched.last_run_at = now
        sched.last_task_id = task.id
        sched.run_count += 1
        sched.next_run_at = compute_next_run(sched, max(now, fire_at))
        if sched.next_run_at is None:
            sched.enabled = sched.schedule_type != ScheduleType.ONCE and sched.enabled
        if was_created:
            created.append(task.id)
            await journal.system_event(session, "scheduler", "schedule_fired", f"{sched.name} -> {task.id}",
                                       schedule_id=str(sched.id))
    return created
