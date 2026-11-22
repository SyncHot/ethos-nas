#!/usr/bin/env python3
"""Settings OS-update task dedupe (Playwright).

Regression for the Tasks panel stacking identical "Updating Ubuntu" rows:
re-rendering Settings while an apt job runs re-attached to the job by starting
a *new* watcher (and task row) each time. There must be exactly one row no
matter how often the System section is opened, and it must finish cleanly.

Run: .venv/bin/python tests/test_settings_task_dedupe.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ethos-task-dedupe-"))
DATA = TMP / "data"
os.environ.update({
    "ETHOS_DATA_DIR": str(DATA),
    "ETHOS_JWT_SECRET": "task-dedupe-test-secret",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
    "ETHOS_UPDATE_CHECK": "0",
})
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_ready(base: str, proc: subprocess.Popen, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/api/system/status", timeout=2)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 401:      # up, unauthenticated
                return
        except OSError:
            pass
        if proc.poll() is not None:
            raise SystemExit("instance died on start")
        time.sleep(0.3)
    raise SystemExit("instance did not become ready")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_job(status: str, percent: int, detail: str) -> None:
    """Fake the helper's job.json/result.json (the API only reads these files)."""
    folder = DATA / "os-update"
    folder.mkdir(parents=True, exist_ok=True)
    at = stamp()
    job = {"status": status, "step": "upgrade" if status == "running" else "done",
           "detail": detail, "upgraded": 0 if status == "running" else 84,
           "percent": percent, "at": at, "pid": 1}
    (folder / "job.json").write_text(json.dumps(job), encoding="utf-8")
    if status != "running":
        result = {"status": status, "step": "done", "detail": detail,
                  "upgraded": 84, "at": at}
        (folder / "result.json").write_text(json.dumps(result), encoding="utf-8")


def main() -> int:
    write_job("running", 3, "upgrading 0/84")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    token = _create_token("task-dedupe", "task-dedupe", "admin")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )

    try:
        wait_ready(base, proc)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            ctx.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base)
            page.wait_for_function("typeof window.openApp === 'function'", timeout=20000)
            page.evaluate("window.openApp('settings')")
            page.wait_for_selector('[data-testid="settings-nav-system"]', timeout=15000)

            # Three visits = three navToken bumps. The old code added a row each time.
            for _ in range(3):
                page.locator('[data-testid="settings-nav-system"]').click()
                page.wait_for_timeout(900)
            page.wait_for_function(
                "document.querySelectorAll('[data-testid=\"os-update-item\"]').length >= 1",
                timeout=10000,
            )
            page.wait_for_timeout(1500)
            rows = page.locator('[data-testid="os-update-item"]').count()
            check("repeated re-renders keep one task row", rows == 1, str(rows))
            step = page.locator('[data-testid="os-update-step"]').inner_text()
            check("running job shows apt progress", "upgrading 0/84" in step, step)

            write_job("ok", 100, "84 package(s) upgraded")
            page.wait_for_function(
                "document.querySelector('[data-testid=\"os-update-percent\"]')?.textContent === '100%'",
                timeout=15000,
            )
            page.wait_for_timeout(500)
            check("the single row finishes", page.locator('[data-testid="os-update-item"]').count() == 1)
            text = page.locator('[data-testid="os-update-item"]').inner_text()
            check("finished row reports the result", "84 package(s) upgraded" in text, text)
            check("no page errors", not errors, "; ".join(errors[:2]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
