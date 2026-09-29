"""MCP tool provider over Streamable HTTP. Used for both:

* `composio` - Composio MCP (GitHub / AWS / Google toolkits), URL + x-api-key from config
* `local`    - Atlas' own local MCP server (app.integrations.mcp.local_server)

Each operation opens a short-lived MCP session (initialize -> list/call -> close). That keeps the
provider stateless and restart-safe; tool lists are cached by the ToolRegistry.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app.tools.base import (
    TOOLKIT_CAPABILITIES,
    RawToolResult,
    ToolProviderUnavailable,
    ToolSpec,
    classify_risk,
    infer_toolkit,
)

logger = logging.getLogger("atlas.mcp")


def _exc_text(exc: BaseException) -> str:
    """anyio wraps transport errors in ExceptionGroups; surface the root cause."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"[:300]


class MCPToolProvider:
    def __init__(self, name: str, url: str, headers: dict[str, str] | None = None, *, enabled: bool = True,
                 timeout: float = 60.0, default_toolkit: str | None = None,
                 risk_overrides: dict[str, str] | None = None):
        self.name = name
        self.url = url
        self.headers = headers or {}
        self.enabled = enabled
        self.timeout = timeout
        self.default_toolkit = default_toolkit
        self.risk_overrides = risk_overrides or {}

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.url)

    async def _with_session(self, fn):  # type: ignore[no-untyped-def]
        if not self.configured:
            raise ToolProviderUnavailable(f"MCP provider '{self.name}' is not configured")
        try:
            async with streamablehttp_client(self.url, headers=self.headers, timeout=15,
                                             sse_read_timeout=self.timeout) as (read, write, _):
                async with ClientSession(read, write,
                                         read_timeout_seconds=timedelta(seconds=self.timeout)) as session:
                    await session.initialize()
                    return await asyncio.wait_for(fn(session), timeout=self.timeout)
        except ToolProviderUnavailable:
            raise
        except BaseException as exc:  # noqa: BLE001 - anyio ExceptionGroup etc.
            if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt):
                raise
            raise ToolProviderUnavailable(f"MCP provider '{self.name}' failed: {_exc_text(exc)}") from None

    async def list_tools(self) -> list[ToolSpec]:
        async def _list(session: ClientSession) -> list[Any]:
            tools, cursor = [], None
            while True:
                res = await session.list_tools(cursor=cursor) if cursor else await session.list_tools()
                tools.extend(res.tools)
                cursor = getattr(res, "nextCursor", None)
                if not cursor:
                    return tools

        specs: list[ToolSpec] = []
        for t in await self._with_session(_list):
            ann = t.annotations.model_dump(exclude_none=True) if getattr(t, "annotations", None) else {}
            meta = (getattr(t, "meta", None) or {}) if isinstance(getattr(t, "meta", None), dict) else {}
            toolkit = meta.get("toolkit") or infer_toolkit(t.name) or self.default_toolkit
            risk = self.risk_overrides.get(t.name) or meta.get("risk") or classify_risk(t.name, ann)
            caps = list(meta.get("capabilities") or TOOLKIT_CAPABILITIES.get(toolkit or "", []))
            specs.append(ToolSpec(
                id=f"{self.name}:{t.name}", name=t.name, provider=self.name, description=t.description or "",
                input_schema=t.inputSchema or {"type": "object", "properties": {}},
                output_schema=getattr(t, "outputSchema", None), toolkit=toolkit, capabilities=caps,
                risk_level=risk, requires_approval=str(risk) == "DESTRUCTIVE",
            ))
        return specs

    async def call_tool(self, spec: ToolSpec, arguments: dict[str, Any]) -> RawToolResult:
        async def _call(session: ClientSession) -> Any:
            return await session.call_tool(spec.name, arguments)

        res = await self._with_session(_call)
        content = [c.model_dump(exclude_none=True, mode="json") for c in (res.content or [])]
        return RawToolResult(content=content, structured=getattr(res, "structuredContent", None),
                             is_error=bool(res.isError), metadata={"provider": self.name, "transport": "mcp"})

    async def health(self) -> dict[str, Any]:
        if not self.configured:
            return {"status": "CONFIGURATION_REQUIRED", "detail": f"{self.name} MCP URL/credentials not configured"}
        try:
            count = len(await self.list_tools())
            return {"status": "OK", "tools": count}
        except ToolProviderUnavailable as exc:
            return {"status": "UNREACHABLE", "detail": str(exc)[:300]}
