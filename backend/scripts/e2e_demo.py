"""End-to-end demo against a RUNNING Atlas stack (section 49 of the spec).

    docker compose exec api python scripts/e2e_demo.py
    # or from the host:  ATLAS_URL=http://localhost:8000 python backend/scripts/e2e_demo.py

Env: ATLAS_URL (default http://localhost:8000), ATLAS_UI_URL (optional: create the task through the web UI
proxy), ATLAS_API_TOKEN, DEMO_APPROVAL_FILE (optional path inside the workspace to exercise approvals).
Exits non-zero if any check fails. Prints what is CONFIGURATION REQUIRED instead of hiding it.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import httpx

API = os.environ.get("ATLAS_URL", "http://localhost:8000").rstrip("/")
UI = os.environ.get("ATLAS_UI_URL", "").rstrip("/")
TOKEN = os.environ.get("ATLAS_API_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
DEMO = "Analyze today's AWS alarms and summarize anything that needs attention."
FAILED: list[str] = []


def check(cond: bool, label: str) -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILED.append(label)
    return cond


def call(method: str, path: str, base: str = API, **kw: Any) -> Any:
    r = httpx.request(method, base + path, headers=HEADERS if base == API else {}, timeout=60, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else None


def wait_task(task_id: str, until: set[str], timeout: float = 180) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = call("GET", f"/api/tasks/{task_id}")
        if t["status"] in until:
            return t
        time.sleep(1)
    raise TimeoutError(f"{task_id} did not reach {until}")


def create_task(request: str) -> dict[str, Any]:
    body = {"request": request, "source": "web", "user_external_id": "demo-user", "session_key": "web:demo-user"}
    if UI:  # exactly what the browser does: POST through the nginx proxy (token injected server-side)
        return call("POST", "/api/tasks", base=UI, json=body)["task"]
    return call("POST", "/api/tasks", json=body)["task"]


def main() -> int:
    print(f"== Atlas E2E demo against {API}" + (f" (UI proxy {UI})" if UI else ""))
    ready = call("GET", "/ready")
    check(ready["status"] == "ready", f"/ready -> {ready['database']}")
    integ = call("GET", "/api/integrations")
    print("== Integrations")
    for name, info in integ.items():
        print(f"  {name:10s} {info.get('status', '')} {str(info.get('detail', ''))[:110]}")
    if UI:
        html = httpx.get(UI + "/", timeout=10).text
        check("<div id=\"root\">" in html, "web UI served by frontend container")

    print("== 1-13: task through the single pipeline")
    task = create_task(DEMO)
    print(f"  created {task['id']} (source={task['source']})")
    t = wait_task(task["id"], {"COMPLETED", "FAILED"})
    res = t["result"] or {}
    check(t["status"] == "COMPLETED", f"task completed ({t['status']})")
    check(t["selected_agent"] == "aws_devops", f"AWS agent selected ({t['selected_agent']})")
    events = [e["event_type"] for e in call("GET", f"/api/tasks/{t['id']}/events")]
    for e in ("MEMORY_RETRIEVED", "PLAN_CREATED", "AGENT_SELECTED", "MODEL_CALL", "TOOL_CALLED", "TOOL_RESULT",
              "VALIDATION", "MEMORY_CREATED", "TASK_COMPLETED"):
        check(e in events, f"journal has {e}")
    model_ev = [e for e in call("GET", f"/api/tasks/{t['id']}/events") if e["event_type"] == "MODEL_CALL"]
    for ev in model_ev[:2]:
        md = ev["metadata"]
        print(f"  model call: provider={md.get('provider')} model={md.get('model')} offline={md.get('offline')} "
              f"reason={str(md.get('fallback_reason'))[:160]}")
    execs = call("GET", f"/api/tasks/{t['id']}/tool-executions")
    check(bool(execs), "tool execution recorded: " + ", ".join(f"{x['tool_id']}={x['status']}" for x in execs))
    print(f"  summary: {res.get('summary', '')[:300]}")
    for c in res.get("configuration_required") or []:
        print(f"  CONFIGURATION REQUIRED -> {c}")
    print(f"  session runtime: {res.get('session_runtime')}  memory: {res.get('memory')}")

    print("== 14-15: feedback -> learning candidate -> approval")
    fb = call("POST", "/api/feedback", json={"task_id": t["id"], "channel": "web",
                                              "content": "Before restarting anything, check CloudWatch status and recent events."})
    cands = fb["candidates"]
    check(bool(cands), f"learning candidate created: {[c['normalized_lesson'] for c in cands]}")
    for c in cands:
        if c["status"] == "CANDIDATE":
            call("POST", f"/api/learning/candidates/{c['id']}/decision", json={"approve": True, "decided_by": "demo"})
    lessons = call("GET", "/api/learning/lessons")
    check(any("CloudWatch" in lesson["content"] for lesson in lessons), "lesson approved and stored")

    print("== 16: similar future task retrieves the learned procedure")
    t2 = wait_task(create_task("The EC2 app server is down after the AWS alarm fired. Should we restart it?")["id"],
                   {"COMPLETED", "FAILED"})
    applied = (t2["result"] or {}).get("applied_lessons") or []
    check(bool(applied) and "CloudWatch" in applied[0]["content"], f"{t2['id']} applied lesson: "
          f"{applied[0]['content'] if applied else None}")
    hits = call("POST", "/api/memory/search", json={"query": "restart EC2 after alarm", "limit": 3})
    check(bool(hits), "memory search: " + "; ".join(f"{h['memory']['type']}:{h['score']}" for h in hits))

    approval_file = os.environ.get("DEMO_APPROVAL_FILE")
    if approval_file:
        print("== approvals: destructive tool pauses until a human approves")
        t3 = create_task(f"Delete the file {approval_file} from the workspace")
        w = wait_task(t3["id"], {"WAITING_APPROVAL", "COMPLETED", "FAILED"})
        check(w["status"] == "WAITING_APPROVAL", f"{t3['id']} is {w['status']}")
        pend = call("GET", "/api/approvals", params={"status": "PENDING", "task_id": t3["id"]})["items"]
        if check(bool(pend), f"approval requested: {pend[0]['action_summary'] if pend else None}"):
            if os.environ.get("DEMO_STOP_AFTER_APPROVAL_REQUEST"):
                print(f"  leaving {t3['id']} WAITING_APPROVAL (approval id {pend[0]['id']})")
                print(f"APPROVAL_ID={pend[0]['id']} TASK_ID={t3['id']}")
                return 1 if FAILED else 0
            call("POST", f"/api/approvals/{pend[0]['id']}/decision", json={"approve": True, "decided_by": "demo"})
            done = wait_task(t3["id"], {"COMPLETED", "FAILED"})
            execs = call("GET", f"/api/tasks/{t3['id']}/tool-executions")
            check(done["status"] == "COMPLETED" and any(x["tool_id"].endswith("delete_workspace_file") and
                                                        x["status"] == "SUCCESS" for x in execs),
                  "destructive action executed only after approval")

    print(f"== RESULT: {'ALL CHECKS PASSED' if not FAILED else f'{len(FAILED)} CHECK(S) FAILED: {FAILED}'}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
