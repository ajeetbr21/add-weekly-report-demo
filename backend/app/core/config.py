"""Application configuration loaded from environment variables / .env.

All secrets come from the environment. Nothing in here has a real credential default.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# dotenv files, later ones override earlier ones; real environment variables always win.
# Default: atlas/.env when running from atlas/backend (local development). Inside containers neither
# file exists and compose provides the environment. ATLAS_ENV_FILE="" disables dotenv loading (tests).
_ENV_FILES = tuple(p for p in os.environ.get("ATLAS_ENV_FILE", "../.env,.env").split(",") if p.strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILES or None, env_file_encoding="utf-8", extra="ignore")

    # ---- application -------------------------------------------------------------------------
    app_name: str = "Atlas"
    app_env: str = "local"
    log_level: str = "INFO"
    log_json: bool = True
    application_secret: SecretStr = SecretStr("")
    # Token protecting /api/*. Empty = auth disabled (only acceptable when bound to localhost).
    atlas_api_token: SecretStr = SecretStr("")
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:5173"])
    default_project_slug: str = "default"

    # ---- database ----------------------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://atlas:atlas@localhost:5432/atlas"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_echo: bool = False

    # ---- model gateway (OmniRoute, OpenAI-compatible) ----------------------------------------
    # Explicit opt-in: prompts are only sent to the gateway when enabled AND a URL is set. OmniRoute's
    # default "auto" combo can route to third-party (free) providers - configure providers first.
    omniroute_enabled: bool = False
    omniroute_base_url: str = ""
    omniroute_api_key: SecretStr = SecretStr("")
    omniroute_timeout_seconds: float = 120.0
    # after a failed gateway call, skip the gateway for this many seconds (circuit breaker)
    omniroute_failure_cooldown_seconds: float = 60.0
    model_default: str = "auto"
    model_fast: str = ""
    model_reasoning: str = ""
    model_coding: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = 1536
    # When OmniRoute is not configured/reachable, use the deterministic offline engine
    # (clearly labelled in journals/results) instead of failing every task.
    llm_offline_fallback: bool = True

    # ---- Letta (session state runtime) ---------------------------------------------------------
    letta_enabled: bool = False
    letta_base_url: str = "http://letta:8283"
    letta_api_key: SecretStr = SecretStr("")

    # ---- tools / MCP -------------------------------------------------------------------------
    local_mcp_enabled: bool = True
    local_mcp_url: str = "http://mcp-local:8765/mcp"
    local_mcp_workspace: str = "/workspace"
    # optional shared secret between the agents and the local MCP server (Authorization: Bearer ...)
    local_mcp_token: SecretStr = SecretStr("")
    composio_enabled: bool = False
    composio_mcp_url: str = ""
    composio_api_key: SecretStr = SecretStr("")
    # JSON object of extra headers exactly as exported by the Composio SDK (session.mcp.headers),
    # e.g. {"x-api-key": "...", "x-project-id": "..."}. Takes precedence over COMPOSIO_API_KEY.
    composio_mcp_headers: SecretStr = SecretStr("")
    composio_user_id: str = ""
    tool_timeout_seconds: float = 60.0
    tool_refresh_seconds: int = 300

    # ---- Telegram ----------------------------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_allowed_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    atlas_api_url: str = "http://api:8000"
    telegram_result_poll_seconds: float = 3.0

    # ---- webhooks ----------------------------------------------------------------------------
    github_webhook_secret: SecretStr = SecretStr("")
    custom_webhook_secret: SecretStr = SecretStr("")

    # ---- worker / scheduler ------------------------------------------------------------------
    worker_poll_interval_seconds: float = 2.0
    worker_concurrency: int = 2
    task_lease_seconds: int = 120
    task_max_retries: int = 2
    agent_max_iterations: int = 6
    scheduler_interval_seconds: float = 15.0
    context_max_chars: int = 24000

    # ---- memory ------------------------------------------------------------------------------
    memory_top_k: int = 6
    # minimum cosine similarity for retrieval; the lexical hash embedding has a lower natural range
    memory_min_score: float = 0.3
    memory_min_score_hash: float = 0.1
    learning_auto_approve_threshold: float = 0.85

    @field_validator("telegram_allowed_chat_ids", "cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v):  # type: ignore[no-untyped-def]
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    # ---- derived -----------------------------------------------------------------------------
    @property
    def omniroute_configured(self) -> bool:
        return bool(self.omniroute_enabled and self.omniroute_base_url)

    def composio_headers(self) -> dict[str, str]:
        raw = self.composio_mcp_headers.get_secret_value().strip()
        headers: dict[str, str] = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    headers = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                headers = {}
        key = self.composio_api_key.get_secret_value()
        if key and not any(h.lower() in ("x-api-key", "x-user-api-key") for h in headers):
            headers["x-api-key"] = key
        return headers

    @property
    def composio_configured(self) -> bool:
        return bool(self.composio_enabled and self.composio_mcp_url and self.composio_headers())

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
