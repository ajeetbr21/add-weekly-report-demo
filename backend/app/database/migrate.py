"""Wait until PostgreSQL accepts connections, then apply Alembic migrations.

Used by the `migrate` compose service:  python -m app.database.migrate
Waiting here (instead of relying only on container healthchecks) keeps startup reliable on runtimes where
healthchecks are unavailable (e.g. Podman without systemd).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import asyncpg
from alembic.config import Config

from alembic import command
from app.core.config import get_settings

BACKEND = Path(__file__).resolve().parents[2]


async def wait_for_db(url: str, timeout: float = 180.0) -> None:
    dsn = url.replace("+asyncpg", "")
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(dsn, timeout=5)
            await conn.execute("SELECT 1")
            await conn.close()
            return
        except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
            last = exc
            await asyncio.sleep(2)
    raise RuntimeError(f"database not reachable after {timeout:.0f}s: {last}")


def main() -> int:
    url = get_settings().database_url
    print("waiting for database ...", flush=True)
    asyncio.run(wait_for_db(url))
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    command.upgrade(cfg, "head")
    print("migrations applied", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
