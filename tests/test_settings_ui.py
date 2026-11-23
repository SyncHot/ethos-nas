#!/usr/bin/env python3
"""Settings window E2E — rail shell, every section, both themes.

Boots an isolated EthOS instance (temp ETHOS_DATA_DIR), then drives the real
Settings window: 218 px rail, eight sections, admin gating, dark theme and the
≤ 780 px horizontal strip.

Requires Playwright (`pip install -r requirements-dev.txt`). Skips when the
browser runtime is missing.

Run:  .venv/bin/python tests/test_settings_ui.py
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
DATA = tempfile.mkdtemp(prefix="ethos-settings-ui-")
os.environ.update({
    "ETHOS_DATA_DIR": DATA,
    "ETHOS_JWT_SECRET": "settings-ui-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402

FAILURES = []

SECTIONS = [
    ("overview", '[data-testid="settings-health-panel"]'),
    ("updates", '[data-testid="core-update-card"]'),
    ("system", '[data-testid="settings-system-card"]'),
    ("maintenance", '[data-testid="settings-caches-card"]'),
    ("users", ".ui-table"),
    ("sharing", '[data-testid="settings-sharing-card"]'),
    ("security", '[data-testid="api-key-card"]'),
    ("appearance", '[data-testid="wp-custom-preview"]'),
    ("about", ".ui-table"),
]


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


def open_settings(page, base: str, token: str) -> None:
    page.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
    page.goto(base)
    page.wait_for_function("typeof window.openApp === 'function'", timeout=20000)
    page.wait_for_timeout(1200)
    page.evaluate("window.openApp('settings')")
    page.wait_for_selector('[data-testid="settings-shell"]', timeout=15000)


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    token = _create_token("admin", "admin", "admin")
    user_token = _create_token("viewer", "viewer", "user")

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
            except Exception as exc:  # browser binary missing
                print(f"chromium unavailable ({exc}) — skipping")
                return 0

            context = browser.new_context(viewport={"width": 1400, "height": 900})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            open_settings(page, base, token)

            check("Settings window renders the shell", page.locator('[data-testid="settings-shell"]').count() == 1)
            rail_width = page.eval_on_selector('[data-testid="settings-rail"]', "el => getComputedStyle(el).width")
            check("rail is 218 px", rail_width == "218px", rail_width)
            check(f"rail has {len(SECTIONS)} sections",
                  page.locator('[data-testid^="settings-nav-"]').count() == len(SECTIONS),
                  str(page.locator('[data-testid^="settings-nav-"]').count()))

            for section, selector in SECTIONS:
                page.click(f'[data-testid="settings-nav-{section}"]')
                page.wait_for_selector(selector, timeout=20000)
                check(f"{section} section renders", page.locator(selector).count() > 0)
                check(f"{section} sets aria-current",
                      page.locator(f'[data-testid="settings-nav-{section}"][aria-current="page"]').count() == 1)
                if section == "overview":
                    check("health section has the Health label",
                          page.locator('[data-testid="settings-nav-overview"]').inner_text().strip() == "Health")
                    check("health monitor keeps its manual check action",
                          page.locator('[data-action="health-run"]').count() == 1)
                    check("health section has no resource dashboard",
                          page.locator('[data-testid="settings-shell"] .ui-summary, '
                                       '[data-testid="settings-shell"] .ui-grid').count() == 0)

            # The update controls are admin-only.
            page.click('[data-testid="settings-nav-updates"]')
            page.wait_for_selector('[data-testid="core-update-card"]')
            check("admin sees the update check button",
                  page.locator('[data-testid="core-update-check"]').count() == 1)
            page.wait_for_selector('[data-testid="core-update-download"]:not([hidden])')
            download_href = page.get_attribute('[data-testid="core-update-download"]', "href")
            check("core card links the release archive",
                  bool(download_href) and "/archive/refs/tags/v" in download_href
                  and download_href.endswith(".zip"), str(download_href))
            page.wait_for_selector('[data-testid="extension-download"]:not([hidden])')
            extension_href = page.get_attribute('[data-testid="extension-download"]', "href")
            check("extension card links the release zip",
                  bool(extension_href) and extension_href.endswith(
                      "/releases/latest/download/ethos-nas-downloader.zip"), str(extension_href))
            page.click('[data-testid="settings-nav-system"]')
            page.wait_for_selector('[data-testid="settings-os-image-card"]')
            check("admin sees the OS image check button",
                  page.locator('[data-testid="settings-os-image-check"]').count() == 1)

            # Both themes are part of the shell.
            page.evaluate("document.documentElement.classList.remove('light'); document.documentElement.classList.add('dark')")
            page.wait_for_timeout(150)
            scheme = page.eval_on_selector('[data-testid="settings-shell"]', "el => getComputedStyle(el).colorScheme")
            check("dark theme is applied", "dark" in scheme, scheme)
            page.evaluate("document.documentElement.classList.remove('dark'); document.documentElement.classList.add('light')")
            page.wait_for_timeout(150)
            scheme = page.eval_on_selector('[data-testid="settings-shell"]', "el => getComputedStyle(el).colorScheme")
            check("light theme is applied", "light" in scheme, scheme)

            # ≤ 780 px: the rail becomes a horizontal strip.
            page.set_viewport_size({"width": 720, "height": 900})
            page.wait_for_timeout(250)
            direction = page.eval_on_selector('[data-testid="settings-rail"]', "el => getComputedStyle(el).flexDirection")
            check("rail collapses to a horizontal strip", direction == "row", direction)
            page.set_viewport_size({"width": 1400, "height": 900})

            check("no page errors", not errors, "; ".join(errors[:3]))

            # Non-admin: destructive/update actions stay hidden or disabled.
            user_context = browser.new_context(viewport={"width": 1280, "height": 900})
            user_page = user_context.new_page()
            user_errors = []
            user_page.on("pageerror", lambda exc: user_errors.append(str(exc)))
            open_settings(user_page, base, user_token)
            user_page.click('[data-testid="settings-nav-updates"]')
            user_page.wait_for_selector('[data-testid="core-update-card"]')
            check("non-admin has no update button",
                  user_page.locator('[data-testid="core-update-check"]').count() == 0)
            user_page.wait_for_selector('[data-testid="core-update-download"]:not([hidden])')
            check("non-admin can download the release",
                  user_page.locator('[data-testid="core-update-download"]:not([hidden])').count() == 1)
            user_page.wait_for_selector('[data-testid="extension-download"]:not([hidden])')
            check("non-admin can download the browser extension",
                  user_page.locator('[data-testid="extension-download"]:not([hidden])').count() == 1)
            user_page.click('[data-testid="settings-nav-maintenance"]')
            user_page.wait_for_selector('[data-testid="settings-caches-card"]')
            restart_disabled = user_page.eval_on_selector('[data-action="restart"]', "el => el.disabled")
            check("non-admin cannot restart", restart_disabled is True)
            user_page.click('[data-testid="settings-nav-system"]')
            user_page.wait_for_selector('[data-testid="settings-system-card"]')
            check("non-admin has no OS update check button",
                  user_page.locator('[data-testid="settings-os-check"]').count() == 0)
            check("non-admin has no OS image check button",
                  user_page.locator('[data-testid="settings-os-image-check"]').count() == 0)
            check("no page errors for non-admin", not user_errors, "; ".join(user_errors[:3]))

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
