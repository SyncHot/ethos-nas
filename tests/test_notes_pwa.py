#!/usr/bin/env python3
"""Sticky Notes PWA E2E (Playwright).

Boots an isolated EthOS instance, mocks the package proxy in the browser, and
verifies the mobile page at /notes: login gate, quick capture (note + task),
search, checklist toggle, offline queue + replay, and the installable manifest.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(tempfile.mkdtemp(prefix="ethos-notes-pwa-"))
os.environ.update({
    "ETHOS_DATA_DIR": str(DATA),
    "ETHOS_JWT_SECRET": "notes-pwa-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
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


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed — skipping")
        return 0

    port = free_port()
    env = {**os.environ, "ETHOS_PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    token = _create_token("notes-pwa", "notes-pwa", "admin")

    notes: list[dict] = []
    posts: list[dict] = []
    puts: list[dict] = []
    mode = {"offline": False}

    def find(note_id):
        return next((note for note in notes if note["id"] == note_id), None)

    def route_handler(route):
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path
        suffix = path.split("/proxy/api/sticky-notes", 1)[-1]
        method = request.method
        if mode["offline"] and suffix != "/settings":
            route.abort("internetdisconnected")
            return
        if suffix == "/settings" and method == "GET":
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"settings": {"ribbon_enabled": False, "ribbon_show_completed": True}}))
            return
        if suffix == "/reminders/due":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"reminders": []}))
            return
        if suffix == "/push/key":
            route.fulfill(status=503, content_type="application/json",
                          body=json.dumps({"detail": "push is not available"}))
            return
        if suffix == "" and method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"notes": notes}))
            return
        if suffix == "" and method == "POST":
            body = request.post_data_json or {}
            posts.append(body)
            now = "2026-11-22T10:00:00+00:00"
            if body.get("kind") == "checklist":
                note = {
                    "id": body.get("client_id") or "sn_aaaaaaaaaaaa", "kind": "checklist",
                    "text": body.get("text", ""),
                    "items": [{"id": (body.get("items") or [{}])[0].get("client_id") or "ci_aaaaaaaaaaaa",
                               "text": (body.get("items") or [{}])[0].get("text", ""), "done": False}],
                    "color": "yellow", "pinned": False, "archived": False,
                    "due_at": None, "reminder_at": None, "reminder_fired_at": None,
                    "created_at": now, "updated_at": now,
                }
            else:
                note = {
                    "id": body.get("client_id") or "sn_bbbbbbbbbbbb", "kind": "note",
                    "text": body.get("text", ""), "items": [], "color": "blue",
                    "pinned": False, "archived": False,
                    "due_at": None, "reminder_at": None, "reminder_fired_at": None,
                    "created_at": now, "updated_at": now,
                }
            notes.append(note)
            route.fulfill(status=201, content_type="application/json",
                          body=json.dumps({"notes": notes, "note": note}))
            return
        parts = [part for part in suffix.strip("/").split("/") if part]
        if len(parts) == 1 and method == "PUT":
            note = find(parts[0])
            if note:
                note.update(request.post_data_json or {})
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"notes": notes, "note": note}))
            return
        if len(parts) == 1 and method == "DELETE":
            notes[:] = [note for note in notes if note["id"] != parts[0]]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"notes": notes}))
            return
        if len(parts) == 3 and parts[1] == "items" and method == "PUT":
            note = find(parts[0])
            item = next((entry for entry in (note or {}).get("items", []) if entry["id"] == parts[2]), None)
            if item:
                item.update(request.post_data_json or {})
            puts.append(request.post_data_json or {})
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"notes": notes, "note": note, "item": item}))
            return
        if len(parts) == 2 and parts[1] == "items" and method == "POST":
            note = find(parts[0])
            body = request.post_data_json or {}
            if note:
                note.setdefault("items", []).append({
                    "id": body.get("client_id") or "ci_cccccccccccc",
                    "text": body.get("text", ""), "done": False,
                })
            route.fulfill(status=201, content_type="application/json",
                          body=json.dumps({"notes": notes, "note": note}))
            return
        if len(parts) == 3 and parts[1] == "items" and method == "DELETE":
            note = find(parts[0])
            if note:
                note["items"] = [entry for entry in note.get("items", []) if entry["id"] != parts[2]]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"notes": notes, "note": note}))
            return
        route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "not found"}))

    try:
        end = time.time() + 60
        while time.time() < end:
            try:
                urllib.request.urlopen(base + "/notes", timeout=2)
                break
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(0.3)
        if proc.poll() is not None:
            print(proc.stderr.read()[-2000:], file=sys.stderr)
            check("isolated instance started", False)
            return 1

        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
            context.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.route("**/api/packages/ethos-sticky-notes/proxy/api/sticky-notes**", route_handler)
            page.route("**/api/packages/ethos-sticky-notes/proxy/api/sticky-notes/settings**", route_handler)

            page.goto(base + "/notes")
            page.locator("#notes-main").wait_for(state="visible", timeout=15000)
            check("authenticated page hides the login card", page.locator("#notes-login").is_hidden())
            check("manifest link points at the notes app",
                  page.evaluate("document.querySelector('link[rel=manifest]').getAttribute('href')")
                  == "/manifest-notes.webmanifest")

            page.fill("#notes-quick-input", "Buy milk")
            page.click("#notes-add")
            page.wait_for_function(
                "Array.from(document.querySelectorAll('textarea.notes-note-text')).some(t => t.value === 'Buy milk')",
                timeout=5000)
            check("quick add sends a note", posts and posts[0].get("kind") == "note"
                  and posts[0].get("text") == "Buy milk", json.dumps(posts[:1]))
            check("quick add uses an idempotent client id",
                  str(posts[0].get("client_id", "")).startswith("sn_"), str(posts[0].get("client_id")))

            page.click('[data-mode="checklist"]')
            page.fill("#notes-quick-input", "Call mom")
            page.press("#notes-quick-input", "Enter")
            page.wait_for_function(
                "Array.from(document.querySelectorAll('input')).some(i => i.value === 'Call mom')",
                timeout=5000)
            check("quick add task creates a checklist item",
                  len(posts) >= 2 and posts[1].get("kind") == "checklist"
                  and posts[1].get("items", [{}])[0].get("text") == "Call mom",
                  json.dumps(posts[1] if len(posts) > 1 else {}))

            page.fill("#notes-search", "milk")
            page.wait_for_function(
                """() => {
                    const values = Array.from(document.querySelectorAll(
                        'textarea.notes-note-text, input.notes-note-title, .notes-item input[type=text]'
                    )).map((el) => el.value);
                    return values.includes('Buy milk') && !values.includes('Call mom');
                }""",
                timeout=3000)
            check("search hides non-matching tasks", True)
            page.fill("#notes-search", "")
            page.wait_for_timeout(150)

            checkbox = page.locator('input[type="checkbox"]').first
            checkbox.check()
            page.wait_for_timeout(250)
            check("checking an item sends a done update", any(item.get("done") is True for item in puts),
                  json.dumps(puts))

            mode["offline"] = True
            page.evaluate("window.dispatchEvent(new Event('offline'))")
            page.locator("#notes-offline").wait_for(state="visible", timeout=3000)
            page.fill("#notes-quick-input", "Offline idea")
            page.click("#notes-add")
            page.wait_for_function(
                "Array.from(document.querySelectorAll('input')).some(i => i.value === 'Offline idea')",
                timeout=5000)
            pending = page.evaluate("JSON.parse(localStorage.getItem('ethos-notes-outbox:notes-pwa') || '[]').length")
            check("offline add is queued", pending >= 1, str(pending))

            mode["offline"] = False
            page.evaluate("window.dispatchEvent(new Event('online'))")
            page.wait_for_function(
                "JSON.parse(localStorage.getItem('ethos-notes-outbox:notes-pwa') || '[]').length === 0",
                timeout=5000)
            check("queued note replays when online",
                  any(post.get("text") == "Offline idea"
                      or (post.get("items") or [{}])[0].get("text") == "Offline idea"
                      for post in posts),
                  json.dumps([post.get("text") or (post.get("items") or [{}])[0].get("text") for post in posts]))
            check("outbox drains after replay",
                  page.evaluate("JSON.parse(localStorage.getItem('ethos-notes-outbox:notes-pwa') || '[]').length") == 0)

            login_context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
            login_page = login_context.new_page()
            login_page.route("**/api/packages/ethos-sticky-notes/proxy/api/sticky-notes**", route_handler)
            login_page.goto(base + "/notes")
            login_page.locator("#notes-login").wait_for(state="visible", timeout=10000)
            check("without a token the login card is shown", login_page.locator("#notes-main").is_hidden())
            login_context.close()

            check("no page errors", not errors, "; ".join(errors[:2]))
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    failed = [item for item in results if not item[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
