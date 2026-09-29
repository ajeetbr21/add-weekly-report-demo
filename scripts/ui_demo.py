"""Browser walkthrough of the Section-49 demo through the real Web UI (Playwright).

Runs against a running stack and saves screenshots. Easiest: from a Playwright container attached to
the compose network:

    docker run --rm --network atlas_default -v "$PWD/scripts:/scripts" -v "$PWD/screenshots:/shots" \
      -e UI_URL=http://frontend mcr.microsoft.com/playwright/python:v1.63.0-noble python /scripts/ui_demo.py

(the UI origin, here http://frontend, must be listed in CORS_ORIGINS). Or on the host with
`pip install playwright && playwright install chromium` and UI_URL=http://localhost:3000.
Exit code is non-zero when a step fails.
"""

from __future__ import annotations

import os
import re
import sys
import time

from playwright.sync_api import Page, expect, sync_playwright

UI = os.environ.get("UI_URL", "http://localhost:3000").rstrip("/")
SHOTS = os.environ.get("SHOTS_DIR", "/shots")
APPROVAL_FILE = os.environ.get("DEMO_APPROVAL_FILE", "notes/ui-demo.txt")
DEMO = "Analyze today's AWS alarms and summarize anything that needs attention."
FEEDBACK = "Before restarting anything, check CloudWatch status and recent events."
FOLLOW_UP = "The EC2 app server is down after the AWS alarm fired. Should we restart it?"
LONG = 180_000


def shot(page: Page, name: str) -> None:
    os.makedirs(SHOTS, exist_ok=True)
    page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)
    print(f"  screenshot {name}.png")


def run_task(page: Page, request: str, until: str = "COMPLETED") -> str:
    page.goto(f"{UI}/")
    page.get_by_placeholder(re.compile("What should Atlas do")).fill(request)
    page.get_by_role("button", name="Run task").click()
    page.wait_for_url(re.compile(r"/tasks/TASK-\d{4}-\d+"), timeout=30_000)
    task_id = page.url.rsplit("/", 1)[-1]
    expect(page.locator("h1 .badge")).to_have_text(until, timeout=LONG)
    page.wait_for_timeout(1500)  # let the journal/tool panels refresh
    print(f"  {task_id} -> {until}")
    return task_id


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        print("1. dashboard")
        page.goto(f"{UI}/")
        expect(page.get_by_text("backend ready")).to_be_visible(timeout=30_000)
        shot(page, "01-dashboard")

        print("2. demo task from the web UI")
        t1 = run_task(page, DEMO)
        expect(page.get_by_role("heading", name=re.compile("Result"))).to_be_visible()
        expect(page.get_by_text("TOOL_RESULT").first).to_be_visible()
        shot(page, "02-task-result-and-journal")

        print("3. feedback -> learning candidate")
        page.get_by_placeholder(re.compile("What should Atlas do differently")).fill(FEEDBACK)
        page.get_by_role("button", name="Send feedback").click()
        expect(page.get_by_text(re.compile("Feedback stored"))).to_be_visible(timeout=30_000)
        shot(page, "03-feedback")

        print("4. approve the lesson")
        page.goto(f"{UI}/learning")
        approve = page.get_by_role("button", name="Approve lesson").first
        approve.wait_for(state="visible", timeout=20_000)  # the candidate list loads asynchronously
        shot(page, "04a-learning-candidate")
        approve.click()
        expect(page.get_by_role("heading", name=re.compile(r"Lessons \(1\)"))).to_be_visible(timeout=20_000)
        expect(page.get_by_role("cell", name=re.compile("CloudWatch")).first).to_be_visible()
        shot(page, "04b-learning-lesson-approved")

        print("5. similar task retrieves the learned procedure")
        t2 = run_task(page, FOLLOW_UP)
        expect(page.get_by_role("heading", name="Lessons applied from memory")).to_be_visible()
        shot(page, "05-follow-up-applies-lesson")

        print("6. destructive action waits for human approval")
        run_task(page, f"Delete the file {APPROVAL_FILE} from the workspace", until="WAITING_APPROVAL")
        expect(page.get_by_role("button", name="APPROVE")).to_be_visible()
        shot(page, "06-approval-required")
        page.get_by_role("button", name="APPROVE").click()
        expect(page.locator("h1 .badge")).to_have_text("COMPLETED", timeout=LONG)
        page.wait_for_timeout(1500)
        shot(page, "07-approved-and-completed")

        print("7. memory search, reports, agents, integrations, approvals, schedules")
        page.goto(f"{UI}/memory")
        page.get_by_placeholder(re.compile("how to troubleshoot")).fill("restart EC2 after alarm")
        page.get_by_role("button", name="Search").click()
        expect(page.get_by_role("cell", name=re.compile("CloudWatch")).first).to_be_visible(timeout=15_000)
        shot(page, "08-memory-search")
        for path, name in (("/reports", "09-reports"), ("/agents", "10-agents"), ("/integrations", "11-integrations"),
                           ("/approvals?", "12-approvals"), ("/schedules", "13-schedules"), ("/", "14-dashboard-after")):
            page.goto(f"{UI}{path}")
            page.wait_for_timeout(2000)
            shot(page, name)
        browser.close()

    print(f"tasks: {t1}, {t2}")
    real_errors = [e for e in errors if "favicon" not in e]
    print(f"browser console errors: {real_errors or 'none'}")
    return 1 if real_errors else 0


if __name__ == "__main__":
    started = time.time()
    try:
        rc = main()
    except Exception as exc:  # noqa: BLE001 - print a short, readable failure instead of a page dump
        print(f"UI DEMO FAILED: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}")
        rc = 1
    print(f"UI demo finished in {time.time() - started:.0f}s, rc={rc}")
    sys.exit(rc)
