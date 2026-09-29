from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import AgentRegistry
from app.core.errors import NotFoundError
from app.database.session import get_db
from app.models import AgentRun, ToolDefinition, ToolExecution
from app.schemas.api import AgentOut, AgentRunOut, AgentUpdate, ToolExecutionOut, ToolOut
from app.schemas.common import Page
from app.services import journal
from app.tools.registry import get_tool_registry

agents_router = APIRouter(prefix="/api/agents", tags=["agents"])
tools_router = APIRouter(prefix="/api/tools", tags=["tools"])


@agents_router.get("", response_model=list[AgentOut])
async def list_agents(include_disabled: bool = True, db: AsyncSession = Depends(get_db)) -> list[AgentOut]:
    return [AgentOut.model_validate(a) for a in await AgentRegistry(db).list(include_disabled=include_disabled)]


@agents_router.get("/runs", response_model=Page[AgentRunOut])
async def list_agent_runs(limit: int = Query(default=50, le=200), offset: int = 0,
                          db: AsyncSession = Depends(get_db)) -> Page[AgentRunOut]:
    rows = (await db.execute(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit).offset(offset))
            ).scalars()
    total = (await db.execute(select(func.count()).select_from(AgentRun))).scalar_one()
    return Page(items=[AgentRunOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@agents_router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: str, db: AsyncSession = Depends(get_db)) -> AgentOut:
    return AgentOut.model_validate(await AgentRegistry(db).get(agent_id))


@agents_router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(agent_id: str, body: AgentUpdate, db: AsyncSession = Depends(get_db)) -> AgentOut:
    """Explicit operator action (audited). Learning never calls this."""
    agent = await AgentRegistry(db).get(agent_id)
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(agent, k, v)
    await journal.audit(db, "operator", "agent.update", agent_id, fields=sorted(changes))
    await db.flush()
    return AgentOut.model_validate(agent)


@tools_router.get("", response_model=list[ToolOut])
async def list_tools(db: AsyncSession = Depends(get_db)) -> list[ToolOut]:
    rows = (await db.execute(select(ToolDefinition).order_by(ToolDefinition.provider, ToolDefinition.id))).scalars()
    return [ToolOut.model_validate(r) for r in rows]


@tools_router.post("/refresh")
async def refresh_tools(db: AsyncSession = Depends(get_db)) -> dict:
    """Re-discover tools from all MCP providers (local + Composio)."""
    return {"providers": await get_tool_registry().refresh(db)}


class ToolPatch(BaseModel):
    enabled: bool | None = None
    requires_approval: bool | None = None


@tools_router.patch("/{tool_id}", response_model=ToolOut)
async def patch_tool(tool_id: str, body: ToolPatch, db: AsyncSession = Depends(get_db)) -> ToolOut:
    row = await db.get(ToolDefinition, tool_id)
    if row is None:
        raise NotFoundError(f"Tool {tool_id} not found")
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.requires_approval is not None:
        # destructive tools can never be switched to "no approval"
        row.requires_approval = body.requires_approval or row.risk_level == "DESTRUCTIVE"
    await journal.audit(db, "operator", "tool.update", tool_id, **body.model_dump(exclude_none=True))
    await db.flush()
    return ToolOut.model_validate(row)


@tools_router.get("/executions", response_model=Page[ToolExecutionOut])
async def list_executions(limit: int = Query(default=50, le=200), offset: int = 0,
                          db: AsyncSession = Depends(get_db)) -> Page[ToolExecutionOut]:
    rows = (await db.execute(select(ToolExecution).order_by(ToolExecution.started_at.desc()).limit(limit)
                             .offset(offset))).scalars()
    total = (await db.execute(select(func.count()).select_from(ToolExecution))).scalar_one()
    return Page(items=[ToolExecutionOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)
