"""Minimal async REST client for a self-hosted Letta server (http://letta:8283/v1).

We use Letta for *session* state: each conversation/session key owns a set of Letta memory blocks
(`conversation`, `active_task`, `scratchpad`). Blocks are first-class Letta objects and do not require an
LLM, so session state works even before model credentials are configured. When a Letta agent is
created for a session (optional, requires LLM config inside Letta) the same blocks are attached to it.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings, get_settings


class LettaError(Exception):
    pass


class LettaClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or get_settings()
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.settings.letta_enabled and self.settings.letta_base_url)

    def _client(self) -> httpx.AsyncClient:
        headers = {"Content-Type": "application/json"}
        key = self.settings.letta_api_key.get_secret_value()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        base = self.settings.letta_base_url.rstrip("/")
        return httpx.AsyncClient(base_url=base if base.endswith("/v1") else base + "/v1", headers=headers,
                                 timeout=15.0, transport=self._transport)

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            async with self._client() as c:
                resp = await c.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise LettaError(f"Letta unreachable: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise LettaError(f"Letta {method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else None

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "DISABLED", "detail": "LETTA_ENABLED=false (using local session store)"}
        try:
            data = await self._req("GET", "/health/")
            return {"status": "OK", "version": (data or {}).get("version")}
        except LettaError as exc:
            return {"status": "UNREACHABLE", "detail": str(exc)[:200]}

    async def create_block(self, label: str, value: str, limit: int = 8000, description: str | None = None) -> str:
        body: dict[str, Any] = {"label": label, "value": value, "limit": limit}
        if description:
            body["description"] = description
        data = await self._req("POST", "/blocks/", json=body)
        return data["id"]

    async def get_block(self, block_id: str) -> dict[str, Any]:
        return await self._req("GET", f"/blocks/{block_id}")

    async def update_block(self, block_id: str, value: str) -> dict[str, Any]:
        return await self._req("PATCH", f"/blocks/{block_id}", json={"value": value})

    async def delete_block(self, block_id: str) -> None:
        await self._req("DELETE", f"/blocks/{block_id}")
