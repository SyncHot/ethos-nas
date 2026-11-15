#!/usr/bin/env python3
"""Backup Manager package UI E2E (Playwright).

Seeds the local `ethos-packages/backup-manager` module as an installed native
package in an isolated EthOS instance, then drives the real window against the
real `/api/backup` API: create a schedule, run it, and open the restore view.

Requires the sibling repo `~/apps/compose/ethos-packages`; skips otherwise.

Run: .venv/bin/python tests/test_backup_ui.py
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

DATA = tempfile.mkdtemp(prefix="ethos-backup-ui-data-")
os.environ.update({
    "ETHOS_DATA_DIR": DATA,
    "ETHOS_JWT_SECRET": "backup-ui-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path.home() / "apps/compose/ethos-packages/backup-manager"
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402
from backend.services import packages_store  # noqa: E402

FAILURES = []


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
    if not (PACKAGE / "frontend/app.js").is_file():
        print(f"package repo not found at {PACKAGE} — skipping")
        return 0

    source = tempfile.mkdtemp(prefix="bm-ui-src-")
    destination = tempfile.mkdtemp(prefix="bm-ui-dst-")
    with open(os.path.join(source, "hello.txt"), "w", encoding="utf-8") as handle:
        handle.write("hello from the ui test\n")

    installed = Path(DATA) / "packages" / "apps" / "backup-manager"
    (installed / "frontend").mkdir(parents=True)
    (installed / "frontend" / "app.js").write_text(
        (PACKAGE / "frontend" / "app.js").read_text(encoding="utf-8"), encoding="utf-8")
    (installed / "icon.svg").write_text((PACKAGE / "icon.svg").read_text(encoding="utf-8"), encoding="utf-8")
    packages_store.add_package({
        "id": "backup-manager",
        "name": "EthOS Backup Manager",
        "version": "1.0.2",
        "icon": "🔄",
        "description": "Backup UI test",
        "trust": "pinned",
        "ui": {"mode": "native", "entry": "frontend/app.js"},
        "files": [
            {"path": "frontend/app.js", "url": "local", "sha256": "0" * 64, "mode": 420},
            {"path": "icon.svg", "url": "local", "sha256": "0" * 64, "mode": 420},
        ],
    })

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

        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            context.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base)
            page.wait_for_timeout(2500)
            page.evaluate("window.openApp('pkg-backup-manager')")
            page.wait_for_selector(".bm-root", timeout=15000)
            check("package window renders", page.locator(".bm-root").count() == 1)
            check("overview is the default view", "Overview" in page.locator("#bm-title").inner_text(),
                  page.locator("#bm-title").inner_text())
            check("rail has all sections", page.locator(".bm-nav-item").count() == 5)

            page.click('[data-action="new-schedule"]')
            page.wait_for_selector("#bm-name", timeout=5000)
            page.fill("#bm-name", "UI schedule")
            page.fill("#bm-source", source)
            page.fill("#bm-dest", destination)
            page.click('.bm-form-actions [data-action="save-schedule"]')
            page.wait_for_function(
                "() => [...document.querySelectorAll('.bm-table td')].some(td => td.textContent.includes('UI schedule'))",
                timeout=15000)
            check("schedule created through the wizard", True)

            page.click('.bm-table [data-action="run"]')
            page.wait_for_function(
                "() => (document.querySelector('#bm-pill')?.textContent || '').match(/backup|queued|Starting/i)",
                timeout=15000)
            page.wait_for_function(
                "() => document.querySelector('#bm-pill')?.textContent.includes('Ready')",
                timeout=90000)
            check("run completed", True)

            page.click('.bm-nav-item[data-view="restore"]')
            page.wait_for_selector("tr[data-gen-row]", timeout=15000)
            check("restore lists the generation", page.locator("tr[data-gen-row]").count() >= 1)

            page.evaluate("document.documentElement.classList.remove('light'); document.documentElement.classList.add('dark')")
            background = page.evaluate("getComputedStyle(document.querySelector('.bm-root')).getPropertyValue('--bm-bg').trim()")
            check("dark palette applies", background.startswith("#081"), background)

            check("no page errors", not errors, "; ".join(errors[:3]))
            check("native UI is not an iframe", page.locator("iframe").count() == 0)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(source, ignore_errors=True)
        shutil.rmtree(destination, ignore_errors=True)

    print(f"\n{len(FAILURES)} failure(s)")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
