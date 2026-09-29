"""Thin HTTP client the Telegram bot uses to talk to the local Atlas API. The bot has no business logic
and no database access: everything goes through the same /api endpoints as the Web UI."""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import Settings, get_settings


class AtlasApiClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or get_settings()
        headers = {}
        token = self.settings.atlas_api_token.get_secret_value()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(base_url=self.settings.atlas_api_url.rstrip("/"), headers=headers,
                                         timeout=30, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        r = await self._client.get(path, params={k: v for k, v in params.items() if v is not None})
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        r = await self._client.post(path, json=body)
        r.raise_for_status()
        return r.json()

    async def create_task(self, request: str, chat_id: int, user_name: str | None) -> dict[str, Any]:
        return (await self._post("/api/tasks", {"request": request, "source": "telegram",
                                                "user_external_id": str(chat_id), "user_display_name": user_name,
                                                "session_key": f"telegram:{chat_id}",
                                                "metadata": {"telegram_chat_id": chat_id}}))["task"]

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._get(f"/api/tasks/{task_id}")

    async def list_tasks(self, limit: int = 5) -> list[dict[str, Any]]:
        return (await self._get("/api/tasks", limit=limit))["items"]

    async def list_agents(self) -> list[dict[str, Any]]:
        return await self._get("/api/agents")

    async def dashboard(self) -> dict[str, Any]:
        return await self._get("/api/system/dashboard")

    async def health(self) -> dict[str, Any]:
        return await self._get("/ready")

    async def approvals(self, task_id: str | None = None, status: str = "PENDING") -> list[dict[str, Any]]:
        return (await self._get("/api/approvals", task_id=task_id, status=status))["items"]

    async def decide(self, approval_id: str, approve: bool, who: str) -> dict[str, Any]:
        return await self._post(f"/api/approvals/{approval_id}/decision",
                                {"approve": approve, "decided_by": who, "channel": "telegram"})

    async def feedback(self, task_id: str, content: str, chat_id: int, rating: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"task_id": task_id, "content": content, "channel": "telegram",
                                "user_external_id": str(chat_id)}
        if rating:
            body["rating"] = rating
        return await self._post("/api/feedback", body)
