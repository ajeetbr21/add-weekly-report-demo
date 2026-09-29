"""Scripted OpenAI-compatible transport standing in for OmniRoute in tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.config import get_settings
from app.integrations.omniroute.client import ModelClient


class ScriptedGateway:
    """Each chat request is answered by `responder(body) -> dict(message)`; records every request."""

    def __init__(self, responder: Callable[[dict[str, Any]], dict[str, Any]]):
        self.responder = responder
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-fast"}, {"id": "test-reasoning"}]})
        body = json.loads(request.content or b"{}")
        self.requests.append({"path": request.url.path, "body": body,
                              "auth": request.headers.get("authorization")})
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(404, json={"error": "no embeddings in test gateway"})
        msg = self.responder(body)
        return httpx.Response(200, json={"id": "x", "model": body.get("model"), "choices": [
            {"index": 0, "message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7}})

    def client(self, **overrides: Any) -> ModelClient:
        if "omniroute_api_key" in overrides:
            overrides["omniroute_api_key"] = SecretStr(overrides["omniroute_api_key"])
        s = get_settings().model_copy(update={"omniroute_enabled": True, "omniroute_base_url": "http://omniroute.test", "model_default": "test-default",
                                              "model_fast": "test-fast", "model_reasoning": "test-reasoning",
                                              "model_coding": "test-coding", **overrides})
        return ModelClient(settings=s, transport=httpx.MockTransport(self.handler))


def tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def final(summary: str, **extra: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": json.dumps({"summary": summary, "findings": extra.get("findings", []),
                                                        "recommendations": extra.get("recommendations", []),
                                                        "facts": extra.get("facts", []),
                                                        "confidence": extra.get("confidence", 0.8)})}
