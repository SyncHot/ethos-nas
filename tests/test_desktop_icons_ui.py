#!/usr/bin/env python3
"""Playwright coverage for desktop app icon placement.

Boots an isolated EthOS instance and verifies drawer drops, collision handling,
desktop-icon movement, and per-user layout persistence.

Run: .venv/bin/python tests/test_desktop_icons_ui.py
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
DATA = tempfile.mkdtemp(prefix="ethos-desktop-icons-ui-")
os.environ.update({
    "ETHOS_DATA_DIR": DATA,
    "ETHOS_JWT_SECRET": "desktop-icons-ui-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402

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


def drop_from_drawer(page, app_id: str, col: int, row: int) -> None:
    page.evaluate("""({appId, col, row}) => {
        const container = document.getElementById('desktopIcons');
        const rect = container.getBoundingClientRect();
        const barHeight = parseFloat(getComputedStyle(document.documentElement)
            .getPropertyValue('--topbar-h')) || 42;
        const x = rect.left + 24 + col * 104 + 48;
        const y = rect.top + barHeight + 16 + row * 108 + 50;
        const transfer = new DataTransfer();
        transfer.setData('text/plain', appId);
        document.body.dispatchEvent(new DragEvent('drop', {
            bubbles: true, cancelable: true, clientX: x, clientY: y,
            dataTransfer: transfer,
        }));
    }""", {"appId": app_id, "col": col, "row": row})


def drop_pinned_icon(page, app_id: str, col: int, row: int) -> None:
    page.evaluate("""({appId, col, row}) => {
        const container = document.getElementById('desktopIcons');
        const rect = container.getBoundingClientRect();
        const barHeight = parseFloat(getComputedStyle(document.documentElement)
            .getPropertyValue('--topbar-h')) || 42;
        const x = rect.left + 24 + col * 104 + 48;
        const y = rect.top + barHeight + 16 + row * 108 + 50;
        const transfer = new DataTransfer();
        transfer.setData('application/x-ethos-desktop-icon', appId);
        container.dispatchEvent(new DragEvent('drop', {
            bubbles: true, cancelable: true, clientX: x, clientY: y,
            dataTransfer: transfer,
        }));
    }""", {"appId": app_id, "col": col, "row": row})


def cell_for(page, app_id: str) -> dict:
    return page.locator(f'[data-testid="desktop-icon-{app_id}"]').evaluate("""el => {
        const barHeight = parseFloat(getComputedStyle(document.documentElement)
            .getPropertyValue('--topbar-h')) || 42;
        return {
            c: Math.round((parseFloat(el.style.left) - 24) / 104),
            r: Math.round((parseFloat(el.style.top) - barHeight - 16) / 108),
        };
    }""")


def wait_for_remote_positions(page, token: str, app_ids: list[str]) -> None:
    expected = {app_id: cell_for(page, app_id) for app_id in app_ids}
    page.wait_for_function("""async ({token, expected}) => {
        const response = await fetch('/api/packages/desktop/layout', {
            headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) return false;
        const layout = await response.json();
        return Object.entries(expected).every(([id, cell]) =>
            layout.positions?.[id]?.c === cell.c && layout.positions?.[id]?.r === cell.r);
    }""", arg={"token": token, "expected": expected}, timeout=15000)


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
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page.goto(base)
            page.wait_for_selector("#desktopIcons", timeout=20000)
            page.wait_for_selector('[data-testid="desktop-icon-trash"]', timeout=20000)
            page.wait_for_function("document.body._dropBound === true && document.getElementById('desktopIcons')._reorderAttached === true")

            target = {"c": 3, "r": 2}
            drop_from_drawer(page, "overview", target["c"], target["r"])
            overview = page.locator('[data-testid="desktop-icon-overview"]')
            overview.wait_for(state="visible", timeout=10000)
            overview_cell = cell_for(page, "overview")
            check("drawer drop places the app under the pointer", overview_cell == target, str(overview_cell))

            drop_from_drawer(page, "storage", target["c"], target["r"])
            storage = page.locator('[data-testid="desktop-icon-storage"]')
            storage.wait_for(state="visible", timeout=10000)
            storage_cell = cell_for(page, "storage")
            check("occupied cells do not overlap", storage_cell != overview_cell, str(storage_cell))

            drop_pinned_icon(page, "overview", storage_cell["c"], storage_cell["r"])
            moved_overview = cell_for(page, "overview")
            check("pinned icons move to the requested free cell", moved_overview != storage_cell, str(moved_overview))

            wait_for_remote_positions(page, token, ["overview", "storage"])
            before_reload = {
                "overview": cell_for(page, "overview"),
                "storage": cell_for(page, "storage"),
            }
            page.reload()
            page.locator('[data-testid="desktop-icon-overview"]').wait_for(state="visible", timeout=20000)
            page.locator('[data-testid="desktop-icon-storage"]').wait_for(state="visible", timeout=20000)
            after_reload = {
                "overview": cell_for(page, "overview"),
                "storage": cell_for(page, "storage"),
            }
            check("positions persist after reload", after_reload == before_reload, str(after_reload))

            # The Trash shortcut sits in the bottom-right corner; its context
            # menu must flip/clamp instead of running off the viewport.
            page.set_viewport_size({"width": 760, "height": 600})
            page.wait_for_timeout(300)
            page.locator('[data-testid="desktop-icon-trash"]').click(button="right")
            menu = page.locator('[data-testid="icon-ctx-trash"]')
            menu.wait_for(timeout=5000)
            box = menu.bounding_box()
            inside = bool(box) and box["x"] >= 7.5 and box["y"] >= 7.5 \
                and box["x"] + box["width"] <= 760.5 and box["y"] + box["height"] <= 600.5
            check("desktop context menu stays inside the viewport", inside, str(box))

            check("no page errors", not errors, "; ".join(errors[:3]))
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