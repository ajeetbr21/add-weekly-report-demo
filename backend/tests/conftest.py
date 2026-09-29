"""Test fixtures.

Tests run against a REAL PostgreSQL + pgvector database (DATABASE_URL, see scripts/with-test-db.sh) and a
REAL local MCP server started in-process on a free port. External services (OmniRoute, Letta, Composio,
Telegram) are replaced with scripted HTTP transports where a test needs them.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

# ---- environment must be set before app modules read settings ------------------------------------
_WORKSPACE = Path(tempfile.mkdtemp(prefix="atlas-ws-"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


MCP_PORT = _free_port()
os.environ["ATLAS_ENV_FILE"] = ""  # hermetic: never read a developer's atlas/.env
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://atlas:atlas@127.0.0.1:55432/atlas_test")
os.environ.update({
    "OMNIROUTE_ENABLED": "false", "OMNIROUTE_BASE_URL": "", "LLM_OFFLINE_FALLBACK": "true",
    "LOCAL_MCP_TOKEN": "", "COMPOSIO_MCP_HEADERS": "", "LETTA_ENABLED": "false",
    "COMPOSIO_ENABLED": "false", "LOCAL_MCP_ENABLED": "true", "LOCAL_MCP_URL": f"http://127.0.0.1:{MCP_PORT}/mcp",
    "LOCAL_MCP_WORKSPACE": str(_WORKSPACE), "LOCAL_MCP_HOST": "127.0.0.1", "LOCAL_MCP_PORT": str(MCP_PORT),
    "ATLAS_API_TOKEN": "", "LOG_JSON": "false", "LOG_LEVEL": "WARNING", "TASK_MAX_RETRIES": "2",
    "TELEGRAM_BOT_TOKEN": "", "GITHUB_WEBHOOK_SECRET": "", "CUSTOM_WEBHOOK_SECRET": "",
})

from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from alembic import command  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.database.session import dispose_engine, session_scope  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


def _start_mcp_server() -> None:
    import uvicorn

    from app.integrations.mcp import local_server

    local_server.WORKSPACE = _WORKSPACE
    app = local_server.mcp.streamable_http_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=MCP_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", MCP_PORT), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("local MCP server did not start")


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")
    _start_mcp_server()
    yield


@pytest.fixture(scope="session")
def workspace() -> Path:
    return _WORKSPACE


TABLES = ["audit_logs", "system_events", "webhook_events", "scheduled_tasks", "reports", "skills", "lessons",
          "learning_candidates", "feedback", "sessions", "memory_embeddings", "memories", "tool_executions",
          "approvals", "agent_runs", "task_steps", "task_events", "tasks", "tools", "agents", "users", "projects"]


@pytest.fixture(autouse=True)
async def clean_db(migrated_db):  # type: ignore[no-untyped-def]
    from app.agents.registry import sync_builtin_agents
    from app.integrations.omniroute.client import set_model_client
    from app.tasks.service import ensure_default_project
    from app.tools.registry import get_tool_registry, set_tool_registry

    async with session_scope() as s:
        await s.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    async with session_scope() as s:
        await sync_builtin_agents(s)
        await ensure_default_project(s)
    set_model_client(None)
    set_tool_registry(None)
    async with session_scope() as s:
        await get_tool_registry().refresh(s)
    yield
    set_model_client(None)
    set_tool_registry(None)
    await dispose_engine()


@pytest.fixture
async def client():  # type: ignore[no-untyped-def]
    import httpx

    from app.main import create_app

    app = create_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def settings():  # type: ignore[no-untyped-def]
    return get_settings()
