"""Approvals, reports, schedules and projects."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import TaskSource
from app.core.errors import ConflictError, NotFoundError
from app.database.session import get_db
from app.models import Approval, Project, Report, ScheduledTask
from app.scheduler import service as scheduler
from app.schemas.api import (
    ApprovalDecision,
    ApprovalOut,
    ProjectCreate,
    ProjectOut,
    ReportOut,
    ScheduleCreate,
    ScheduleOut,
    ScheduleUpdate,
    TaskCreate,
    TaskCreated,
    TaskOut,
)
from app.schemas.common import Page
from app.services import approvals as approval_service
from app.services import journal
from app.tasks import service as task_service

approvals_router = APIRouter(prefix="/api/approvals", tags=["approvals"])
reports_router = APIRouter(prefix="/api/reports", tags=["reports"])
schedules_router = APIRouter(prefix="/api/schedules", tags=["schedules"])
projects_router = APIRouter(prefix="/api/projects", tags=["projects"])


# ------------------------------------------------------------------------------------------ approvals
@approvals_router.get("", response_model=Page[ApprovalOut])
async def list_approvals(status_: str | None = Query(default=None, alias="status"), task_id: str | None = None,
                         limit: int = Query(default=50, le=200), offset: int = 0,
                         db: AsyncSession = Depends(get_db)) -> Page[ApprovalOut]:
    items, total = await approval_service.list_approvals(db, status_, task_id, limit, offset)
    return Page(items=[ApprovalOut.model_validate(a) for a in items], total=total, limit=limit, offset=offset)


@approvals_router.get("/{approval_id}", response_model=ApprovalOut)
async def get_approval(approval_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> ApprovalOut:
    appr = await db.get(Approval, approval_id)
    if appr is None:
        raise NotFoundError("Approval not found")
    return ApprovalOut.model_validate(appr)


@approvals_router.post("/{approval_id}/decision", response_model=ApprovalOut)
async def decide_approval(approval_id: uuid.UUID, body: ApprovalDecision,
                          db: AsyncSession = Depends(get_db)) -> ApprovalOut:
    """APPROVE or REJECT a pending high-risk action. Approved tasks are re-queued and resume."""
    return ApprovalOut.model_validate(await approval_service.decide(db, approval_id, body))


# ------------------------------------------------------------------------------------------ reports
@reports_router.get("", response_model=Page[ReportOut])
async def list_reports(limit: int = Query(default=50, le=200), offset: int = 0,
                       db: AsyncSession = Depends(get_db)) -> Page[ReportOut]:
    rows = (await db.execute(select(Report).order_by(Report.created_at.desc()).limit(limit).offset(offset))).scalars()
    total = (await db.execute(select(func.count()).select_from(Report))).scalar_one()
    return Page(items=[ReportOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@reports_router.get("/{report_id}", response_model=ReportOut)
async def get_report(report_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> ReportOut:
    r = await db.get(Report, report_id)
    if r is None:
        raise NotFoundError("Report not found")
    return ReportOut.model_validate(r)


@reports_router.get("/{report_id}/markdown", response_class=PlainTextResponse)
async def get_report_markdown(report_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> str:
    r = await db.get(Report, report_id)
    if r is None:
        raise NotFoundError("Report not found")
    return r.markdown


# ------------------------------------------------------------------------------------------ schedules
@schedules_router.get("", response_model=list[ScheduleOut])
async def list_schedules(db: AsyncSession = Depends(get_db)) -> list[ScheduleOut]:
    rows = (await db.execute(select(ScheduledTask).order_by(ScheduledTask.created_at.desc()))).scalars()
    return [ScheduleOut.model_validate(r) for r in rows]


@schedules_router.post("", response_model=ScheduleOut, status_code=status.HTTP_201_CREATED)
async def create_schedule(body: ScheduleCreate, db: AsyncSession = Depends(get_db)) -> ScheduleOut:
    sched = await scheduler.create_schedule(db, body)
    await journal.audit(db, "operator", "schedule.create", str(sched.id), type=sched.schedule_type)
    return ScheduleOut.model_validate(sched)


async def _sched(db: AsyncSession, sid: uuid.UUID) -> ScheduledTask:
    s = await db.get(ScheduledTask, sid)
    if s is None:
        raise NotFoundError("Schedule not found")
    return s


@schedules_router.patch("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(schedule_id: uuid.UUID, body: ScheduleUpdate,
                          db: AsyncSession = Depends(get_db)) -> ScheduleOut:
    s = await _sched(db, schedule_id)
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(s, k, v)
    if body.enabled and s.next_run_at is None and s.schedule_type != "ONCE":
        s.next_run_at = scheduler.compute_next_run(s, datetime.now(UTC))
    await db.flush()
    return ScheduleOut.model_validate(s)


@schedules_router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(schedule_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    await db.delete(await _sched(db, schedule_id))
    await journal.audit(db, "operator", "schedule.delete", str(schedule_id))


@schedules_router.post("/{schedule_id}/run-now", response_model=TaskCreated)
async def run_schedule_now(schedule_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> TaskCreated:
    s = await _sched(db, schedule_id)
    proj = await db.get(Project, s.project_id) if s.project_id else None
    task, created = await task_service.create_task(db, TaskCreate(
        request=s.request, source=TaskSource.SCHEDULER, project=proj.slug if proj else None,
        metadata={"schedule_id": str(s.id), "manual_run": True}))
    s.last_task_id = task.id
    return TaskCreated(task=TaskOut.model_validate(task), created=created)


# ------------------------------------------------------------------------------------------ projects
@projects_router.get("", response_model=list[ProjectOut])
async def list_projects(db: AsyncSession = Depends(get_db)) -> list[ProjectOut]:
    return [ProjectOut.model_validate(p) for p in (await db.execute(select(Project).order_by(Project.name))).scalars()]


@projects_router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(get_db)) -> ProjectOut:
    if await task_service.get_project(db, body.slug):
        raise ConflictError(f"Project {body.slug} already exists")
    p = Project(slug=body.slug, name=body.name, description=body.description,
                keywords=[k.lower() for k in body.keywords])
    db.add(p)
    await db.flush()
    return ProjectOut.model_validate(p)
