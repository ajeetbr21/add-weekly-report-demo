"""Model client. Every LLM call in the platform goes through here, and from here to OmniRoute's
OpenAI-compatible API (/v1/chat/completions, /v1/embeddings). Agents never talk to a provider directly.

Model routing is by *tier* (fast / reasoning / coding / default); tier -> model name comes from config.

If OmniRoute is not configured (or fails) and LLM_OFFLINE_FALLBACK=true, the client returns an
`offline=True` response instead of raising. Callers then use their deterministic heuristic path and the
journal/result clearly states that no LLM was used. This is a degraded mode, not a fake model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import ConfigurationRequiredError

logger = logging.getLogger("atlas.model")

TIERS = ("default", "fast", "reasoning", "coding")


class ModelError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": json.dumps(self.arguments)}}


@dataclass
class ModelResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    provider: str = "omniroute"
    usage: dict[str, int] = field(default_factory=dict)
    offline: bool = False
    fallback_reason: str | None = None
    duration_ms: int = 0

    def assistant_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
        return msg


def _parse_json_loose(text: str) -> Any:
    """Parse JSON from a model reply that may be wrapped in ```json fences or prose."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


parse_json_loose = _parse_json_loose


class ModelClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or get_settings()
        self._transport = transport
        # circuit breaker: after a failed call, skip the gateway until this monotonic time
        self._open_until = 0.0
        self._last_error: str | None = None

    # ---------------------------------------------------------------- config
    @property
    def configured(self) -> bool:
        return self.settings.omniroute_configured

    def _circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    def _trip(self, error: str) -> None:
        self._last_error = error
        self._open_until = time.monotonic() + self.settings.omniroute_failure_cooldown_seconds

    @property
    def base_url(self) -> str:
        base = self.settings.omniroute_base_url.rstrip("/")
        return base if base.endswith("/v1") else base + "/v1"

    def model_for(self, tier: str) -> str:
        s = self.settings
        mapping = {"fast": s.model_fast, "reasoning": s.model_reasoning, "coding": s.model_coding}
        return mapping.get(tier) or s.model_default

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        key = self.settings.omniroute_api_key.get_secret_value()
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers=self._headers(),
                                 timeout=timeout or self.settings.omniroute_timeout_seconds,
                                 transport=self._transport)

    def _offline(self, tier: str, reason: str) -> ModelResponse:
        if not self.settings.llm_offline_fallback:
            raise ConfigurationRequiredError(f"Model gateway unavailable: {reason}",
                                             details={"integration": "omniroute"})
        return ModelResponse(content=None, model="offline-heuristic", provider="offline", offline=True,
                             fallback_reason=reason)

    # ---------------------------------------------------------------- chat
    async def chat(self, messages: list[dict[str, Any]], *, tier: str = "default",
                   tools: list[dict[str, Any]] | None = None, json_mode: bool = False,
                   temperature: float = 0.2, max_tokens: int | None = None) -> ModelResponse:
        if not self.configured:
            return self._offline(tier, "OmniRoute not enabled (set OMNIROUTE_ENABLED=true and OMNIROUTE_BASE_URL)")
        if self._circuit_open() and self.settings.llm_offline_fallback:
            return self._offline(tier, f"OmniRoute recently failed, retrying later: {self._last_error}")
        model = self.model_for(tier)
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens

        last_err = "unknown error"
        started = time.monotonic()
        # OmniRoute already does provider fallback/retries internally: one retry here is enough
        for attempt in range(2):
            try:
                async with self._client() as client:
                    resp = await client.post("/chat/completions", json=body)
                if resp.status_code >= 500 or resp.status_code == 429:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                if resp.status_code >= 400:
                    last_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                    break
                parsed = self._parse_chat(resp.json(), model, int((time.monotonic() - started) * 1000))
                self._open_until, self._last_error = 0.0, None
                return parsed
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_err = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.5 * (2 ** attempt))
            except (ValueError, KeyError, IndexError) as exc:
                last_err = f"Malformed gateway response: {exc}"
                break
        logger.warning("model_call_failed", extra={"extra_fields": {"model": model, "error": last_err}})
        self._trip(last_err[:200])
        if self.settings.llm_offline_fallback:
            return self._offline(tier, f"OmniRoute call failed: {last_err}")
        raise ModelError(last_err)

    @staticmethod
    def _parse_chat(data: dict[str, Any], model: str, duration_ms: int) -> ModelResponse:
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {"_raw": raw}
            calls.append(ToolCall(id=tc.get("id") or f"call_{len(calls)}", name=fn.get("name", ""), arguments=args))
        usage = data.get("usage") or {}
        return ModelResponse(content=msg.get("content"), tool_calls=calls, model=data.get("model") or model,
                             provider="omniroute",
                             usage={"prompt_tokens": int(usage.get("prompt_tokens") or 0),
                                    "completion_tokens": int(usage.get("completion_tokens") or 0)},
                             duration_ms=duration_ms)

    async def chat_json(self, messages: list[dict[str, Any]], *, tier: str = "default") -> tuple[Any, ModelResponse]:
        """Chat expecting a JSON object reply. Returns (parsed | None, response)."""
        resp = await self.chat(messages, tier=tier, json_mode=True)
        if resp.offline or not resp.content:
            return None, resp
        try:
            return _parse_json_loose(resp.content), resp
        except (json.JSONDecodeError, ValueError):
            return None, resp

    # ---------------------------------------------------------------- embeddings
    @property
    def embeddings_configured(self) -> bool:
        return self.configured and bool(self.settings.embedding_model)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.embeddings_configured:
            raise ConfigurationRequiredError("Embedding model not configured")
        body = {"model": self.settings.embedding_model, "input": texts,
                "dimensions": self.settings.embedding_dimensions}
        async with self._client(timeout=60) as client:
            resp = await client.post("/embeddings", json=body)
        if resp.status_code >= 400:
            raise ModelError(f"Embedding call failed: HTTP {resp.status_code}: {resp.text[:200]}")
        data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]

    # ---------------------------------------------------------------- health
    async def health(self) -> dict[str, Any]:
        if not self.configured:
            return {"status": "CONFIGURATION_REQUIRED",
                    "detail": "Set OMNIROUTE_ENABLED=true, OMNIROUTE_BASE_URL and OMNIROUTE_API_KEY",
                    "offline_fallback": self.settings.llm_offline_fallback}
        try:
            async with self._client(timeout=5) as client:
                resp = await client.get("/models")
            if resp.status_code == 200:
                models = [m.get("id") for m in (resp.json().get("data") or [])][:50]
                return {"status": "OK", "models_available": len(models), "models": models[:20],
                        "routing": {t: self.model_for(t) for t in TIERS}}
            if resp.status_code in (401, 403):
                return {"status": "CONFIGURATION_REQUIRED", "detail": f"Gateway rejected API key (HTTP {resp.status_code})"}
            return {"status": "DEGRADED", "detail": f"HTTP {resp.status_code}"}
        except httpx.HTTPError as exc:
            return {"status": "UNREACHABLE", "detail": f"{type(exc).__name__}: {exc}"[:200]}


_client: ModelClient | None = None


def get_model_client() -> ModelClient:
    global _client
    if _client is None:
        _client = ModelClient()
    return _client


def set_model_client(client: ModelClient | None) -> None:
    """Test hook / dependency override."""
    global _client
    _client = client
