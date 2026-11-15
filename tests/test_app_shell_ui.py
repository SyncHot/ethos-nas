#!/usr/bin/env python3
"""Core app shell smoke — every desktop window renders on the shared shell.

Boots an isolated EthOS instance, opens each registered app and checks:
  • the window exists and no page error was raised,
  • migrated apps render `.ui-shell`,
  • multi-section apps render a 218 px rail,
  • single-pane apps render no rail.

Requires Playwright. Skips when the browser runtime is missing.

Run:  .venv/bin/python tests/test_app_shell_ui.py
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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = tempfile.mkdtemp(prefix="ethos-shell-ui-")
os.environ.update({
    "ETHOS_DATA_DIR": DATA,
    "ETHOS_JWT_SECRET": "shell-ui-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402

FAILURES = []

# app id -> 'rail' | 'single' | 'exception'
APPS = {
    "overview": "single",
    "storage": "rail",
    "network": "rail",
    "security": "rail",
    "settings": "rail",
    "packages": "rail",
    "logs": "rail",
    "processes": "single",
    "services": "single",
    "smart": "single",
    "systemd": "single",
    "cron": "single",
    "search": "single",
    "shared-links": "single",
    "files": "exception",
    "terminal": "exception",
}


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_ready(base: str) -> bool:
    end = time.time() + 45
    while time.time() < end:
        try:
            urllib.request.urlopen(base + "/api/system/status", timeout=2)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    token = _create_token("admin", "admin", "admin")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        if not wait_ready(base):
            print((proc.stderr.read() if proc.stderr else "")[-2000:])
            return 1

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("playwright is not installed — skipping")
            return 0

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as exc:
                print(f"chromium unavailable ({exc}) — skipping")
                return 0

            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base)
            page.wait_for_function("typeof window.openApp === 'function'", timeout=20000)
            page.wait_for_timeout(1200)

            for app_id, shape in APPS.items():
                page.evaluate(f"window.openApp({json.dumps(app_id)})")
                window = page.locator(f'[data-testid="window-{app_id}"]')
                try:
                    window.wait_for(timeout=15000)
                except Exception:
                    check(f"{app_id} opens", False, "window did not appear")
                    continue
                page.wait_for_timeout(700)
                check(f"{app_id} opens", window.count() == 1)

                if shape == "exception":
                    continue

                shell = window.locator(".ui-shell")
                check(f"{app_id} uses the shared shell", shell.count() == 1)
                if shape == "rail":
                    rail = window.locator('[data-testid$="-rail"]')
                    width = rail.evaluate("el => getComputedStyle(el).width") if rail.count() else ""
                    check(f"{app_id} rail is 218 px", width == "218px", width)
                else:
                    check(f"{app_id} has no rail", window.locator('[data-testid$="-rail"]').count() == 0)

            check("no page errors", not errors, "; ".join(errors[:5]))
            browser.close()

        return 1 if FAILURES else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(DATA, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
