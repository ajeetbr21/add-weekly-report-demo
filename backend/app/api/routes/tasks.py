from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.models import AgentRun, Report, TaskEvent, ToolExecution
from app.schemas.api import (
    AgentRunOut,
    ReportOut,
    TaskCreate,
    TaskCreated,
    TaskDetail,
    TaskEventOut,
    TaskOut,
    ToolExecutionOut,
)
from app.schemas.common import Page
from app.tasks import service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.post("", response_model=TaskCreated, status_code=status.HTTP_201_CREATED)
async def create_task(body: TaskCreate, db: AsyncSession = Depends(get_db)) -> TaskCreated:
    """Create a task. This is the single entry point used by Web UI, Telegram, scheduler and webhooks."""
    task, created = await service.create_task(db, body)
    return TaskCreated(task=TaskOut.model_validate(task), created=created)


@router.get("", response_model=Page[TaskOut])
async def list_tasks(status_: str | None = Query(default=None, alias="status"), source: str | None = None,
                     q: str | None = None, limit: int = Query(default=50, ge=1, le=200),
                     offset: int = Query(default=0, ge=0), db: AsyncSession = Depends(get_db)) -> Page[TaskOut]:
    items, total = await service.list_tasks(db, status=status_, source=source, q=q, limit=limit, offset=offset)
    return Page(items=[TaskOut.model_validate(t) for t in items], total=total, limit=limit, offset=offset)


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(task_id: str, db: AsyncSession = Depends(get_db)) -> TaskDetail:
    return TaskDetail.model_validate(await service.get_task(db, task_id))


@router.get("/{task_id}/events", response_model=list[TaskEventOut])
async def task_events(task_id: str, after_id: int = 0, limit: int = Query(default=500, le=2000),
                      db: AsyncSession = Depends(get_db)) -> list[TaskEventOut]:
    """The task journal: every event needed to reconstruct the task's history."""
    await service.get_task(db, task_id)
    rows = (await db.execute(select(TaskEvent).where(TaskEvent.task_id == task_id, TaskEvent.id > after_id)
                             .order_by(TaskEvent.id).limit(limit))).scalars()
    return [TaskEventOut.model_validate(r) for r in rows]


@router.get("/{task_id}/tool-executions", response_model=list[ToolExecutionOut])
async def task_tool_executions(task_id: str, db: AsyncSession = Depends(get_db)) -> list[ToolExecutionOut]:
    rows = (await db.execute(select(ToolExecution).where(ToolExecution.task_id == task_id)
                             .order_by(ToolExecution.started_at))).scalars()
    return [ToolExecutionOut.model_validate(r) for r in rows]


@router.get("/{task_id}/agent-runs", response_model=list[AgentRunOut])
async def task_agent_runs(task_id: str, db: AsyncSession = Depends(get_db)) -> list[AgentRunOut]:
    rows = (await db.execute(select(AgentRun).where(AgentRun.task_id == task_id).order_by(AgentRun.started_at))
            ).scalars()
    return [AgentRunOut.model_validate(r) for r in rows]


@router.get("/{task_id}/report", response_model=ReportOut | None)
async def task_report(task_id: str, db: AsyncSession = Depends(get_db)) -> ReportOut | None:
    row = (await db.execute(select(Report).where(Report.task_id == task_id))).scalar_one_or_none()
    return ReportOut.model_validate(row) if row else None


@router.post("/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: str, db: AsyncSession = Depends(get_db)) -> TaskOut:
    return TaskOut.model_validate(await service.cancel_task(db, task_id))


@router.post("/{task_id}/retry", response_model=TaskOut)
async def retry_task(task_id: str, db: AsyncSession = Depends(get_db)) -> TaskOut:
    return TaskOut.model_validate(await service.retry_task(db, task_id))
