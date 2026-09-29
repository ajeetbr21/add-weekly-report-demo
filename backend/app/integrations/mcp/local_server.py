"""Atlas local MCP server (Streamable HTTP, port 8765).

A real MCP server exposing safe local tools. It lets the whole Agent -> Tool Registry -> MCP ->
Result Normalizer path run with no external credentials, alongside Composio MCP for GitHub/AWS/Google.

All file tools are confined to LOCAL_MCP_WORKSPACE (default /workspace, a Docker volume).
Risk is declared with MCP ToolAnnotations (readOnlyHint / destructiveHint); Atlas maps
destructive tools to human approval.

Run:  python -m app.integrations.mcp.local_server
"""

from __future__ import annotations

import hmac
import html
import ipaddress
import os
import re
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse

WORKSPACE = Path(os.environ.get("LOCAL_MCP_WORKSPACE", "/workspace")).resolve()
HOST = os.environ.get("LOCAL_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("LOCAL_MCP_PORT", "8765"))
MAX_FETCH_BYTES = 400_000
ALLOW_PRIVATE_FETCH = os.environ.get("LOCAL_MCP_ALLOW_PRIVATE_FETCH", "false").lower() == "true"

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
READ_WEB = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

mcp = FastMCP("atlas-local-tools", host=HOST, port=PORT, stateless_http=True, json_response=True,
              instructions="Local utility tools for Atlas agents: time, web fetch, workspace files, git, tests.")


def _safe_path(rel: str) -> Path:
    p = (WORKSPACE / rel.lstrip("/")).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise ValueError(f"Path escapes workspace: {rel}")
    return p


def _html_to_text(raw: str) -> tuple[str, str]:
    title_m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    title = html.unescape(title_m.group(1).strip()) if title_m else ""
    raw = re.sub(r"<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", "\n", raw, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return title, text.strip()


def _is_private_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


# ------------------------------------------------------------------------------------------- tools
@mcp.tool(annotations=READ, meta={"toolkit": "system", "capabilities": ["time", "utility"]})
def get_current_time(timezone: str = "UTC") -> dict:
    """Return the current date/time in the given IANA timezone (e.g. 'UTC', 'Europe/Berlin')."""
    now = datetime.now(ZoneInfo(timezone))
    return {"iso": now.isoformat(), "timezone": timezone, "weekday": now.strftime("%A"),
            "utc": datetime.now(UTC).isoformat()}


@mcp.tool(annotations=READ_WEB, meta={"toolkit": "web", "capabilities": ["web", "research", "documentation"]})
async def http_fetch(url: str, max_chars: int = 8000) -> dict:
    """Fetch a public web page or document over HTTP(S) and return its title and readable text.
    Use for documentation research and evidence collection. Private/internal addresses are blocked."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only absolute http(s) URLs are allowed")
    if not ALLOW_PRIVATE_FETCH and _is_private_host(parsed.hostname):
        raise ValueError("Fetching private/internal network addresses is not allowed")
    async with httpx.AsyncClient(follow_redirects=True, timeout=20,
                                 headers={"User-Agent": "Atlas-LocalAgent/0.1 (+local research tool)"}) as c:
        resp = await c.get(url)
    body = resp.content[:MAX_FETCH_BYTES].decode(resp.encoding or "utf-8", errors="replace")
    ctype = resp.headers.get("content-type", "")
    title, text = _html_to_text(body) if "html" in ctype or body.lstrip().startswith("<") else ("", body)
    max_chars = max(500, min(int(max_chars), 40000))
    return {"url": str(resp.url), "status_code": resp.status_code, "content_type": ctype, "title": title,
            "text": text[:max_chars], "truncated": len(text) > max_chars,
            "retrieved_at": datetime.now(UTC).isoformat()}


@mcp.tool(annotations=READ, meta={"toolkit": "workspace", "capabilities": ["files", "workspace", "code"]})
def list_workspace_files(path: str = ".", max_entries: int = 200) -> dict:
    """List files and directories under a path inside the local workspace."""
    root = _safe_path(path)
    if not root.exists():
        return {"path": path, "entries": [], "exists": False}
    entries = []
    for p in sorted(root.rglob("*")) if root.is_dir() else [root]:
        if any(part in (".git", "node_modules", "__pycache__", ".venv") for part in p.parts):
            continue
        entries.append({"path": str(p.relative_to(WORKSPACE)), "type": "dir" if p.is_dir() else "file",
                        "size": p.stat().st_size if p.is_file() else None})
        if len(entries) >= max_entries:
            break
    return {"path": path, "entries": entries, "exists": True}


@mcp.tool(annotations=READ, meta={"toolkit": "workspace", "capabilities": ["files", "workspace", "code"]})
def read_workspace_file(path: str, max_chars: int = 20000) -> dict:
    """Read a UTF-8 text file from the local workspace."""
    p = _safe_path(path)
    if not p.is_file():
        raise FileNotFoundError(f"No such file in workspace: {path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    return {"path": path, "content": text[:max_chars], "truncated": len(text) > max_chars, "size": len(text)}


@mcp.tool(annotations=READ, meta={"toolkit": "workspace", "capabilities": ["files", "workspace", "code", "search"]})
def search_workspace(query: str, path: str = ".", max_results: int = 50) -> dict:
    """Case-insensitive text search across workspace files. Returns matching lines."""
    root = _safe_path(path)
    rx = re.compile(re.escape(query), re.I)
    hits = []
    for p in root.rglob("*"):
        if not p.is_file() or p.stat().st_size > 1_000_000 or any(x in p.parts for x in (".git", "node_modules")):
            continue
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if rx.search(line):
                    hits.append({"path": str(p.relative_to(WORKSPACE)), "line": i, "text": line.strip()[:300]})
                    if len(hits) >= max_results:
                        return {"query": query, "matches": hits, "truncated": True}
        except OSError:
            continue
    return {"query": query, "matches": hits, "truncated": False}


@mcp.tool(annotations=READ, meta={"toolkit": "workspace", "capabilities": ["git", "repository", "code"]})
def git_repo_summary(path: str = ".") -> dict:
    """Summarize a git repository in the workspace: current branch, recent commits, and working-tree status."""
    repo = _safe_path(path)

    def git(*args: str) -> str:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:300] or "git command failed")
        return out.stdout.strip()

    return {"path": path, "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "recent_commits": git("log", "--oneline", "-n", "10").splitlines(),
            "status": git("status", "--short").splitlines()[:100]}


@mcp.tool(annotations=WRITE, meta={"toolkit": "workspace", "capabilities": ["files", "notes", "reports"]})
def write_workspace_note(filename: str, content: str) -> dict:
    """Create or overwrite a text note under notes/ in the workspace (e.g. to save a report)."""
    if "/" in filename or filename.startswith("."):
        raise ValueError("filename must be a plain file name")
    p = _safe_path(f"notes/{filename}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": str(p.relative_to(WORKSPACE)), "bytes": len(content.encode())}


@mcp.tool(annotations=DESTRUCTIVE, meta={"toolkit": "workspace", "capabilities": ["files", "cleanup"]})
def delete_workspace_file(path: str) -> dict:
    """Permanently delete a file from the workspace. DESTRUCTIVE: requires human approval in Atlas."""
    p = _safe_path(path)
    if not p.is_file():
        raise FileNotFoundError(f"No such file in workspace: {path}")
    p.unlink()
    return {"deleted": path}


@mcp.tool(annotations=DESTRUCTIVE, meta={"toolkit": "workspace", "capabilities": ["tests", "code", "execution"]})
def run_pytest(path: str = ".", timeout_seconds: int = 120) -> dict:
    """Run `python -m pytest -q` for a project in the workspace. Executes code, so it is treated as
    DESTRUCTIVE and requires human approval."""
    target = _safe_path(path)
    out = subprocess.run(["python", "-m", "pytest", "-q", "--no-header"], cwd=target, capture_output=True, text=True,
                         timeout=max(10, min(timeout_seconds, 600)))
    return {"path": path, "exit_code": out.returncode, "passed": out.returncode == 0,
            "output_tail": (out.stdout + out.stderr)[-6000:]}


class BearerAuth:
    """Optional shared-secret protection (LOCAL_MCP_TOKEN) for the MCP endpoint."""

    def __init__(self, app, token: str):  # type: ignore[no-untyped-def]
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] == "http":
            provided = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(provided, self.expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app():  # type: ignore[no-untyped-def]
    app = mcp.streamable_http_app()
    token = os.environ.get("LOCAL_MCP_TOKEN", "")
    return BearerAuth(app, token) if token else app


def main() -> None:
    import uvicorn

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    uvicorn.run(build_app(), host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
