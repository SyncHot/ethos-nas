"""End-to-end test — Notification Center (system events).

Part A (API, always runs): PAM login → verify list/unread/read-all/per-event-read/
delete/clear semantics on /api/notifications → then trigger a REAL download through
the built-in HTTP engine (local http.server on 127.0.0.1 serving a small file) and
assert the completion produces exactly one "Download completed" event with source
"downloads" and the task name in the message. Self-cleaning: deletes its own events,
task, and temp file at the end.

Part B (UI via Playwright, auto-skips when chromium is unavailable): login → bell
visible on the taskbar → badge reflects unread count → clicking opens the drawer
showing the event row → "Mark all read" clears the badge.

Run:  .venv/bin/python tests/test_notifications.py   (server must be up)
      ETHOS_TEST_BASE=http://127.0.0.1:9001 to target a non-default port.
"""
import http.server
import json
import os

def _require_test_password() -> str:
    """Test credentials are never committed — export ETHOS_TEST_PASSWORD."""
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw
import shutil
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request

from fixture_dir import service_writable_tmpdir

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def api(method, path, token=None, body=None):
    """Minimal JSON API client. Returns (status_code, parsed_json_or_None)."""
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def login():
    code, data = api("POST", "/api/auth/login", body={"username": USER, "password": PASS})
    if code != 200 or not isinstance(data, dict):
        raise RuntimeError(f"login failed: HTTP {code}")
    return data["token"]


# ── Part A: API E2E ───────────────────────────────────────────────────────────

def part_a(token) -> int:
    failures = 0
    print("\nPart A — API")

    # 1. List shape (may contain pre-existing events from other subsystems).
    code, data = api("GET", "/api/notifications?limit=50", token)
    if not check("GET /api/notifications → 200 with {events, unread}",
                 code == 200 and isinstance(data, dict) and "events" in data and "unread" in data,
                 f"HTTP {code}"):
        return 1
    baseline_events = list(data["events"])
    baseline_unread = int(data["unread"] or 0)

    # 2. Unread-count endpoint shape.
    code, data = api("GET", "/api/notifications/unread-count", token)
    if not check("GET /api/notifications/unread-count → {unread}",
                 code == 200 and isinstance(data, dict) and "unread" in data, f"HTTP {code}"):
        failures += 1

    # 3. Trigger a real download through the built-in HTTP engine.
    payload = b"ethos-notification-center-e2e\n" * 64
    tmpdir = service_writable_tmpdir(prefix="ethos-notif-test-")
    served_file = os.path.join(tmpdir, "probe.bin")
    with open(served_file, "wb") as f:
        f.write(payload)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):  # serve from tmpdir only
            return os.path.join(tmpdir, os.path.basename(path))

        def log_message(self, *a):  # silence request logging
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    task_name = "probe.bin"
    code, data = api("POST", "/api/downloads/tasks", token,
                     body={"url": f"http://127.0.0.1:{port}/{task_name}", "folder": tmpdir})
    if not check("POST /api/downloads/tasks → 200 with task id",
                 code == 200 and isinstance(data, dict) and data.get("id"), f"HTTP {code}"):
        httpd.shutdown()
        return failures + 1
    task_id = data["id"]

    # 4. Poll until the download completes (small file → fast).
    status = None
    for _ in range(60):
        code, t = api("GET", f"/api/downloads/tasks/{task_id}", token)
        if code == 200 and isinstance(t, dict):
            status = t.get("status")
            if status in ("completed", "error", "canceled"):
                break
        time.sleep(0.5)
    if not check("download task reached terminal state", status is not None, f"last={status}"):
        httpd.shutdown()
        return failures + 1
    if not check("download task completed (not error)", status == "completed", f"status={status}"):
        print(f"         task detail: {json.dumps(t)[:300]}")
        httpd.shutdown()
        return failures + 1

    # 5. The completion must have emitted exactly one new success event for this file.
    time.sleep(0.5)  # allow the store write to settle
    code, data = api("GET", "/api/notifications?limit=200", token)
    events = data.get("events") if isinstance(data, dict) else []
    new_events = [e for e in events if e not in baseline_events]
    mine = [e for e in new_events
            if e.get("source") == "downloads" and task_name in (e.get("message") or "")]
    if not check("exactly one 'Download completed' event emitted", len(mine) == 1,
                 f"got {len(mine)} of {len(new_events)} new events: {[e.get('title') for e in new_events]}"):
        failures += 1
        httpd.shutdown()
        return failures + 1
    evt = mine[0]
    if not check("event kind=success, title='Download completed', unread",
                 evt.get("kind") == "success" and evt.get("title") == "Download completed"
                 and evt.get("read") is False, json.dumps(evt)[:200]):
        failures += 1

    # 6. Unread count grew by exactly one (our event).
    code, data = api("GET", "/api/notifications/unread-count", token)
    if not check("unread count increased by 1",
                 isinstance(data, dict) and int(data.get("unread", -1)) == baseline_unread + 1,
                 f"baseline={baseline_unread} now={data}"):
        failures += 1

    # 7. Per-event read → unread drops back to baseline; event still listed but read=True.
    code, data = api("POST", f"/api/notifications/{evt['id']}/read", token)
    if not check("POST /{id}/read → ok", code == 200 and isinstance(data, dict) and data.get("ok"), f"HTTP {code}"):
        failures += 1
    code, data = api("GET", "/api/notifications/unread-count", token)
    if not check("unread back to baseline after per-event read",
                 isinstance(data, dict) and int(data.get("unread", -1)) == baseline_unread, f"now={data}"):
        failures += 1

    # 8. Unknown id → 404 on both read and delete.
    code, _ = api("POST", "/api/notifications/evt_doesnotexist/read", token)
    if not check("unknown event id → 404 (read)", code == 404, f"HTTP {code}"):
        failures += 1
    code, _ = api("DELETE", "/api/notifications/evt_doesnotexist", token)
    if not check("unknown event id → 404 (delete)", code == 404, f"HTTP {code}"):
        failures += 1

    # 9. Delete our event → gone; second delete → 404.
    code, data = api("DELETE", f"/api/notifications/{evt['id']}", token)
    if not check("DELETE /{id} → ok", code == 200 and isinstance(data, dict) and data.get("ok"), f"HTTP {code}"):
        failures += 1
    code, _ = api("DELETE", f"/api/notifications/{evt['id']}", token)
    if not check("second DELETE → 404", code == 404, f"HTTP {code}"):
        failures += 1

    # 10. read-all + clear-all semantics (clear removes everything incl. pre-existing).
    code, data = api("POST", "/api/notifications/read-all", token)
    if not check("POST /read-all → ok", code == 200 and isinstance(data, dict) and data.get("ok"), f"HTTP {code}"):
        failures += 1
    code, data = api("GET", "/api/notifications/unread-count", token)
    if not check("unread=0 after read-all", isinstance(data, dict) and int(data.get("unread", -1)) == 0, f"now={data}"):
        failures += 1
    code, data = api("DELETE", "/api/notifications", token)
    if not check("DELETE / (clear all) → ok with cleared count",
                 code == 200 and isinstance(data, dict) and data.get("ok") and "cleared" in data, f"HTTP {code}"):
        failures += 1
    code, data = api("GET", "/api/notifications?limit=50", token)
    if not check("store empty after clear-all",
                 isinstance(data, dict) and data.get("events") == [] and int(data.get("unread", -1)) == 0, f"now={data}"):
        failures += 1

    # Cleanup: remove the download task + served file.
    api("DELETE", f"/api/downloads/tasks/{task_id}", token)
    httpd.shutdown()
    shutil.rmtree(tmpdir, ignore_errors=True)
    return failures


# ── Part B: UI (Playwright; auto-skips without chromium) ─────────────────────

def part_b(token) -> int:
    print("\nPart B — UI (Playwright)")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [SKIP] playwright not installed")
        return 0

    failures = 0
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            print(f"  [SKIP] chromium unavailable ({str(e).splitlines()[0][:120]}…)")
            return 0

        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        js_errors: list[str] = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))

        # Seed one unread event so the badge is deterministic.
        code, data = api("GET", "/api/notifications?limit=50", token)
        pre_existing = [e for e in (data.get("events") or [])] if isinstance(data, dict) else []
        api("DELETE", "/api/notifications", token)  # start from a clean slate

        payload = b"ethos-ui-probe\n" * 32
        tmpdir = service_writable_tmpdir(prefix="ethos-notif-ui-")
        served_file = os.path.join(tmpdir, "uiprobe.bin")
        with open(served_file, "wb") as f:
            f.write(payload)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def translate_path(self, path):
                return os.path.join(tmpdir, os.path.basename(path))

            def log_message(self, *a):
                pass

        httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        code, data = api("POST", "/api/downloads/tasks", token,
                         body={"url": f"http://127.0.0.1:{port}/uiprobe.bin", "folder": tmpdir})
        task_id = data.get("id") if isinstance(data, dict) else None
        for _ in range(60):
            code, t = api("GET", f"/api/downloads/tasks/{task_id}", token)
            if code == 200 and isinstance(t, dict) and t.get("status") in ("completed", "error"):
                break
            time.sleep(0.5)

        # Fresh page → login overlay (token cleared).
        page.goto(BASE, wait_until="networkidle")
        page.evaluate("localStorage.removeItem('token')")
        page.reload(wait_until="networkidle")
        page.fill('[data-testid="login-username"]', USER)
        page.fill('[data-testid="login-password"]', PASS)
        page.click('[data-testid="login-submit"]')
        page.wait_for_function(
            "() => { const o = document.getElementById('loginOverlay'); return !o || o.style.display === 'none'; }",
            timeout=20_000,
        )

        # Bell present on the taskbar.
        bell = page.locator('[data-testid="notification-trigger"]')
        if not check("taskbar bell visible", bell.is_visible()):
            browser.close()
            return failures + 1

        # Badge reflects unread (force a poll so it's fresh, then read DOM).
        page.wait_for_timeout(500)
        badge = page.locator("#notificationCount")
        try:
            page.evaluate("window.pollUnreadCount && window.pollUnreadCount()")
            page.wait_for_timeout(300)
        except Exception:
            pass
        badge_text = (badge.inner_text().strip()) if badge.count() else ""
        if not check(f"badge shows unread count (expected ≥1, got '{badge_text}')", int(badge_text or 0) >= 1):
            failures += 1

        # Click bell → drawer opens with our event row.
        bell.click()
        page.wait_for_selector("#notificationDrawer.open", timeout=5_000)
        if not check("drawer opened", page.locator("#notificationDrawer.open").count() == 1):
            failures += 1
        try:
            page.wait_for_function(
                "() => { const l = document.getElementById('notificationDrawerList');"
                " return l && !/Loading/.test(l.textContent); }", timeout=8_000)
        except Exception:
            pass
        row = page.locator('[data-testid^="notification-event-"]')
        if not check("event row rendered in drawer", row.count() >= 1, f"rows={row.count()}"):
            failures += 1
        elif "uiprobe.bin" not in (row.first.inner_text()):
            if not check("event row mentions the downloaded file", False, row.first.inner_text()[:120]):
                failures += 1

        # Mark all read → badge clears.
        page.click('[data-testid="notification-mark-all-read"]')
        try:
            page.wait_for_function(
                "() => { const b = document.getElementById('notificationCount');"
                " return !b || b.style.display === 'none' || b.textContent.trim() === ''; }", timeout=5_000)
        except Exception:
            pass
        badge_text = (page.locator("#notificationCount").inner_text().strip()) if page.locator("#notificationCount").count() else ""
        if not check("badge cleared after mark-all-read", badge_text == "", f"got '{badge_text}'"):
            failures += 1

        # Close button works.
        page.click('[data-testid="notification-drawer-close"]')
        try:
            page.wait_for_function(
                "() => !document.getElementById('notificationDrawer').classList.contains('open')", timeout=5_000)
            check("drawer closes via × button", True)
        except Exception:
            if not check("drawer closes via × button", False):
                failures += 1

        real_js_errors = [e for e in js_errors if "files/events" not in e]
        if not check("no page JS errors", not real_js_errors, "; ".join(real_js_errors)[:300]):
            failures += 1

        # Cleanup.
        api("DELETE", f"/api/downloads/tasks/{task_id}", token)
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)
        browser.close()
    return failures


def main() -> int:
    print(f"Notification Center E2E — {BASE}")
    token = login()

    total = part_a(token)
    total += part_b(token)

    print(f"\n{'=' * 50}\nRESULT: {'PASS' if total == 0 else f'{total} FAILURE(S)'}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
