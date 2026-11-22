#!/usr/bin/env python3
"""Sticky Notes package UI + ribbon E2E (Playwright).

Seeds the *real* package frontend (`frontend/app.js` + `frontend/ribbon.js`)
against a mock loopback service, then verifies the window, quick capture,
checklist toggle, settings, and the core-mounted right-edge ribbon.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT.parent / "ethos-packages" / "ethos-sticky-notes"
DATA = Path(tempfile.mkdtemp(prefix="ethos-sticky-ui-"))
os.environ.update({
    "ETHOS_DATA_DIR": str(DATA),
    "ETHOS_JWT_SECRET": "sticky-ui-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402
from backend.services import packages_store  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    if not (PACKAGE / "frontend" / "app.js").is_file() or not (PACKAGE / "frontend" / "ribbon.js").is_file():
        print(f"ethos-packages checkout not found at {PACKAGE} — skipping")
        shutil.rmtree(DATA, ignore_errors=True)
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed — skipping")
        shutil.rmtree(DATA, ignore_errors=True)
        return 0

    lock = threading.RLock()
    notes: list[dict] = [{
        "id": "sn_111111111111", "kind": "note", "text": "Seed note", "items": [],
        "color": "yellow", "pinned": True, "archived": False,
        "due_at": None, "reminder_at": None, "reminder_fired_at": None,
        "created_at": "2026-11-22T09:00:00+00:00", "updated_at": "2026-11-22T09:00:00+00:00",
    }]
    settings = {"ribbon_enabled": True, "ribbon_show_completed": True}
    posts: list[dict] = []
    item_updates: list[dict] = []

    def find(note_id):
        return next((note for note in notes if note["id"] == note_id), None)

    class MockService(http.server.BaseHTTPRequestHandler):
        def _reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return {}

        def _path(self):
            return urllib.parse.urlsplit(self.path).path

        def do_GET(self):  # noqa: N802
            path = self._path()
            with lock:
                if path == "/health":
                    return self._reply(200, {"ok": True, "version": "2.0.0", "push": False})
                if path == "/api/sticky-notes":
                    return self._reply(200, {"notes": notes})
                if path == "/api/sticky-notes/settings":
                    return self._reply(200, {"settings": settings})
                if path == "/api/sticky-notes/reminders/due":
                    return self._reply(200, {"reminders": []})
                if path == "/api/sticky-notes/push/key":
                    return self._reply(503, {"detail": "push is not available"})
            self._reply(404, {"detail": "not found"})

        def do_POST(self):  # noqa: N802
            path = self._path()
            body = self._body()
            with lock:
                if path == "/api/sticky-notes":
                    posts.append(body)
                    now = "2026-11-22T10:00:00+00:00"
                    if body.get("kind") == "checklist":
                        note = {
                            "id": body.get("client_id") or "sn_222222222222", "kind": "checklist",
                            "text": body.get("text", ""),
                            "items": [{"id": (body.get("items") or [{}])[0].get("client_id") or "ci_222222222222",
                                       "text": (body.get("items") or [{}])[0].get("text", ""), "done": False}]
                            if body.get("items") else [],
                            "color": "pink", "pinned": False, "archived": False,
                            "due_at": body.get("due_at"), "reminder_at": body.get("reminder_at"),
                            "reminder_fired_at": None, "created_at": now, "updated_at": now,
                        }
                    else:
                        note = {
                            "id": body.get("client_id") or "sn_333333333333", "kind": "note",
                            "text": body.get("text", ""), "items": [], "color": "blue",
                            "pinned": False, "archived": False,
                            "due_at": body.get("due_at"), "reminder_at": body.get("reminder_at"),
                            "reminder_fired_at": None, "created_at": now, "updated_at": now,
                        }
                    notes.append(note)
                    return self._reply(201, {"notes": notes, "note": note})
                parts = [part for part in path.strip("/").split("/") if part]
                if len(parts) == 4 and parts[:3] == ["api", "sticky-notes", "push"] and parts[3] == "test":
                    return self._reply(200, {"sent": 0, "failed": 0})
                if len(parts) == 5 and parts[3] == "items" and parts[4] == "":
                    return self._reply(404, {"detail": "not found"})
                if len(parts) == 4 and parts[1] == "sticky-notes" and parts[3] == "items":
                    note = find(parts[2])
                    item = {"id": body.get("client_id") or "ci_444444444444",
                            "text": body.get("text", ""), "done": False}
                    if note is not None:
                        note.setdefault("items", []).append(item)
                    return self._reply(201, {"notes": notes, "note": note, "item": item})
            self._reply(404, {"detail": "not found"})

        def do_PUT(self):  # noqa: N802
            path = self._path()
            body = self._body()
            with lock:
                if path == "/api/sticky-notes/settings":
                    settings.update({key: body[key] for key in ("ribbon_enabled", "ribbon_show_completed") if key in body})
                    return self._reply(200, {"settings": settings})
                parts = [part for part in path.strip("/").split("/") if part]
                if len(parts) == 3 and parts[:2] == ["api", "sticky-notes"]:
                    note = find(parts[2])
                    if note is not None:
                        note.update(body)
                    return self._reply(200, {"notes": notes, "note": note})
                if len(parts) == 5 and parts[3] == "items":
                    note = find(parts[2])
                    item = next((entry for entry in (note or {}).get("items", []) if entry["id"] == parts[4]), None)
                    if item is not None:
                        item.update(body)
                    item_updates.append(body)
                    return self._reply(200, {"notes": notes, "note": note, "item": item})
            self._reply(404, {"detail": "not found"})

        def do_DELETE(self):  # noqa: N802
            path = self._path()
            with lock:
                parts = [part for part in path.strip("/").split("/") if part]
                if len(parts) == 3 and parts[:2] == ["api", "sticky-notes"]:
                    notes[:] = [note for note in notes if note["id"] != parts[2]]
                    return self._reply(200, {"notes": notes})
            self._reply(404, {"detail": "not found"})

        def log_message(self, *args):
            pass

    proxy_port = free_port()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", proxy_port), MockService)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    installed = DATA / "packages" / "apps" / "ethos-sticky-notes"
    (installed / "frontend").mkdir(parents=True)
    for name in ("app.js", "ribbon.js"):
        shutil.copy2(PACKAGE / "frontend" / name, installed / "frontend" / name)
    shutil.copy2(PACKAGE / "icon.svg", installed / "icon.svg")
    packages_store.add_package({
        "id": "ethos-sticky-notes", "name": "Sticky Notes", "version": "2.0.0",
        "icon": "🗒️", "description": "Sticky UI test", "trust": "pinned",
        "user_access": True,
        "ui": {"mode": "native", "entry": "frontend/app.js", "port": proxy_port,
               "ribbon": {"entry": "frontend/ribbon.js", "title": "Sticky Notes"}},
        "files": [
            {"path": "frontend/app.js", "url": "local", "sha256": "0" * 64, "mode": 420},
            {"path": "frontend/ribbon.js", "url": "local", "sha256": "0" * 64, "mode": 420},
            {"path": "icon.svg", "url": "local", "sha256": "0" * 64, "mode": 420},
        ],
    })

    app_port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(app_port)],
        cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    base = f"http://127.0.0.1:{app_port}"
    token = _create_token("sticky-ui", "sticky-ui", "admin")

    try:
        end = time.time() + 60
        while time.time() < end:
            try:
                urllib.request.urlopen(base + "/api/system/status", timeout=2)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    break
            except OSError:
                time.sleep(0.3)
        if proc.poll() is not None:
            print(proc.stderr.read()[-2000:], file=sys.stderr)
            check("isolated instance started", False)
            return 1

        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            context.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base)

            tab = page.locator('#ethos-ribbon-host [data-testid="sn-ribbon-tab"]')
            tab.wait_for(state="visible", timeout=15000)
            check("core mounted the Sticky Notes ribbon", tab.is_visible())
            tab.click()
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.sn-ribbon-note-text')).some(el => el.textContent.includes('Seed note'))",
                timeout=5000)
            check("ribbon renders the notes list", True)

            page.fill("[data-composer]", "Ribbon quick note")
            page.press("[data-composer]", "Enter")
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.sn-ribbon-note-text')).some(el => el.textContent.includes('Ribbon quick note'))",
                timeout=5000)
            check("ribbon quick add reaches the service", any(post.get("text") == "Ribbon quick note" for post in posts),
                  json.dumps([post.get("text") for post in posts]))
            page.click('#ethos-ribbon-host [data-action="close"]')
            page.wait_for_timeout(250)

            page.evaluate("window.openApp('pkg-ethos-sticky-notes')")
            page.locator('[data-testid="sn-root"]').wait_for(state="visible", timeout=15000)
            check("package window renders", page.locator('[data-testid="sn-nav-settings"]').is_visible())
            check("package window shows the seeded note",
                  page.evaluate("Array.from(document.querySelectorAll('textarea.sn-note-text')).some(t => t.value === 'Seed note')"))

            page.click('[data-action="add-note"]')
            page.wait_for_timeout(300)
            check("app creates a note", any(post.get("kind") == "note" and post.get("text") == "" for post in posts))
            page.click('[data-action="add-task"]')
            page.wait_for_timeout(300)
            check("app creates a task", any(post.get("kind") == "checklist" for post in posts))

            add_form = page.locator(".sn-item-add").last
            add_form.locator("input").fill("Checklist item")
            add_form.locator("button").click()
            page.wait_for_timeout(400)
            check("app adds a checklist item",
                  any(item.get("text") == "Checklist item" for note in notes for item in note.get("items", [])))

            checkbox = page.locator('.sn-note input[type="checkbox"]').last
            checkbox.check()
            page.wait_for_timeout(400)
            check("app toggles a checklist item", any(item.get("done") is True for item in item_updates),
                  json.dumps(item_updates))

            page.click('[data-nav="settings"]')
            page.wait_for_timeout(300)
            toggle = page.locator('input[data-setting="ribbon_enabled"]')
            toggle.wait_for(state="attached", timeout=5000)
            page.evaluate("document.querySelector(\"input[data-setting='ribbon_enabled']\").click()")
            page.wait_for_timeout(500)
            check("settings toggle reaches the service", settings.get("ribbon_enabled") is False)
            check("ribbon hides when disabled", page.locator('#ethos-ribbon-host [data-testid="sn-ribbon-tab"]').is_hidden())

            check("no page errors", not errors, "; ".join(errors[:2]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()

    failed = [item for item in results if not item[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
