"""Tool Registry: discovers tools from providers (Composio MCP, local MCP), persists them in the
`tools` table (the shared source of truth for API + worker), and scopes them per agent."""

from __future__ import annotations

import fnmatch
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.enums import RiskLevel
from app.integrations.mcp.client import MCPToolProvider
from app.models import ToolDefinition
from app.tools.base import ToolProvider, ToolProviderUnavailable, ToolSpec

logger = logging.getLogger("atlas.tools")


def build_providers(settings: Settings | None = None) -> list[ToolProvider]:
    s = settings or get_settings()
    local_headers = {}
    if s.local_mcp_token.get_secret_value():
        local_headers["Authorization"] = f"Bearer {s.local_mcp_token.get_secret_value()}"
    providers: list[ToolProvider] = [
        MCPToolProvider("local", s.local_mcp_url, headers=local_headers, enabled=s.local_mcp_enabled,
                        timeout=s.tool_timeout_seconds),
    ]
    headers = s.composio_headers()
    url = s.composio_mcp_url
    if url and s.composio_user_id and "user_id=" not in url:
        url += ("&" if "?" in url else "?") + f"user_id={s.composio_user_id}"
    providers.append(MCPToolProvider("composio", url, headers=headers, enabled=s.composio_configured,
                                     timeout=s.tool_timeout_seconds))
    return providers


def _spec_from_row(row: ToolDefinition) -> ToolSpec:
    return ToolSpec(id=row.id, name=row.name, provider=row.provider, description=row.description,
                    input_schema=row.input_schema or {}, output_schema=row.output_schema, toolkit=row.toolkit,
                    capabilities=list(row.capabilities or []), risk_level=RiskLevel(row.risk_level),
                    requires_approval=row.requires_approval)


class ToolRegistry:
    def __init__(self, providers: list[ToolProvider] | None = None):
        self.providers: dict[str, ToolProvider] = {p.name: p for p in (providers or build_providers())}
        self.provider_status: dict[str, dict[str, Any]] = {}
        self._last_refresh = 0.0

    def provider(self, name: str) -> ToolProvider | None:
        return self.providers.get(name)

    async def refresh(self, session: AsyncSession) -> dict[str, dict[str, Any]]:
        """Discover tools from every configured provider and upsert them into the tools table."""
        now = datetime.now(UTC)
        for name, prov in self.providers.items():
            if not prov.configured:
                self.provider_status[name] = {"status": "CONFIGURATION_REQUIRED",
                                              "detail": f"{name} provider not configured"}
                continue
            try:
                specs = await prov.list_tools()
            except ToolProviderUnavailable as exc:
                self.provider_status[name] = {"status": "UNREACHABLE", "detail": str(exc)[:300]}
                logger.warning(f"tool_provider_unavailable {name}: {exc}")
                continue
            for spec in specs:
                # atomic upsert: API and worker refresh concurrently at startup
                values = dict(id=spec.id, name=spec.name, provider=spec.provider, toolkit=spec.toolkit,
                              description=spec.description, capabilities=spec.capabilities,
                              input_schema=spec.input_schema, output_schema=spec.output_schema,
                              risk_level=str(spec.risk_level), last_seen_at=now, enabled=True,
                              requires_approval=spec.requires_approval or spec.risk_level == RiskLevel.DESTRUCTIVE,
                              created_at=now, updated_at=now)
                stmt = pg_insert(ToolDefinition).values(**values)
                excl = stmt.excluded
                await session.execute(stmt.on_conflict_do_update(
                    index_elements=[ToolDefinition.id],
                    set_={"name": excl.name, "provider": excl.provider, "toolkit": excl.toolkit,
                          "description": excl.description, "capabilities": excl.capabilities,
                          "input_schema": excl.input_schema, "output_schema": excl.output_schema,
                          "risk_level": excl.risk_level, "last_seen_at": excl.last_seen_at, "updated_at": now,
                          # an operator may force approval on any tool; never silently relax that
                          "requires_approval": or_(ToolDefinition.requires_approval, excl.requires_approval)}))
            self.provider_status[name] = {"status": "OK", "tools": len(specs), "refreshed_at": now.isoformat()}
        await session.flush()
        self._last_refresh = time.monotonic()
        return self.provider_status

    async def refresh_if_stale(self, session: AsyncSession, max_age: float | None = None) -> None:
        max_age = get_settings().tool_refresh_seconds if max_age is None else max_age
        if time.monotonic() - self._last_refresh > max_age:
            await self.refresh(session)

    async def all_tools(self, session: AsyncSession, *, include_disabled: bool = False) -> list[ToolSpec]:
        stmt = select(ToolDefinition).order_by(ToolDefinition.id)
        if not include_disabled:
            stmt = stmt.where(ToolDefinition.enabled.is_(True))
        return [_spec_from_row(r) for r in (await session.execute(stmt)).scalars()]

    async def get(self, session: AsyncSession, tool_id: str) -> ToolSpec | None:
        row = await session.get(ToolDefinition, tool_id)
        return _spec_from_row(row) if row and row.enabled else None

    @staticmethod
    def for_agent(patterns: list[str], specs: list[ToolSpec]) -> list[ToolSpec]:
        return [s for s in specs if any(fnmatch.fnmatchcase(s.id, p) for p in patterns)]


_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


def set_tool_registry(reg: ToolRegistry | None) -> None:
    global _registry
    _registry = reg
