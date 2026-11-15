"""Playwright smoke test — Files login.

Flow: fresh browser (no token) → login overlay visible → wrong password shows
error → correct credentials (marcin / $ETHOS_TEST_PASSWORD) save JWT + reload → desktop
visible → authenticated API check → open Files window.

Run:  .venv/bin/python tests/test_file_station_login.py
"""
import os

def _require_test_password() -> str:
    """Test credentials are never committed — export ETHOS_TEST_PASSWORD."""
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw
import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:9000"
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()


def main() -> int:
    failures = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        ws_errors: list[str] = []
        page.on("console", lambda m: ws_errors.append(m.text) if "files/events" in m.text else None)

        # ── 1. Fresh session → login overlay shown ────────────────────────
        print("Step 1: load app with no token")
        page.goto(BASE, wait_until="networkidle")
        overlay = page.locator("#loginOverlay")
        check("login overlay visible", overlay.is_visible())

        # ── 2. Wrong password → error message, still on login screen ──────
        print("Step 2: wrong password shows error")
        page.fill('[data-testid="login-username"]', USER)
        page.fill('[data-testid="login-password"]', "definitely-wrong-pw")
        page.click('[data-testid="login-submit"]')
        err = page.locator("#loginError")
        page.wait_for_function(
            "() => { const e = document.getElementById('loginError'); return !!(e && e.textContent.trim()); }",
            timeout=15_000,
        )
        check("error message shown", bool(err.inner_text().strip()), err.inner_text())
        check("still on login screen", overlay.is_visible())

        # ── 3. Correct credentials → JWT saved + page reloads ─────────────
        print(f"Step 3: login as {USER}")
        page.fill('[data-testid="login-password"]', PASS)
        page.click('[data-testid="login-submit"]')
        # Success → token saved + window.location.reload(); overlay stays hidden.
        overlay.wait_for(state="hidden", timeout=20_000)
        page.wait_for_load_state("networkidle")

        token = page.evaluate("localStorage.getItem('token') || ''")
        check("JWT stored in localStorage", bool(token), "no token found")
        check("overlay hidden after login", not overlay.is_visible())
        # Note: desktop starts EMPTY by design (Ubuntu-style pinning) — apps are
        # launched from the App Drawer, so no desktop icons on a fresh profile.
        check("top bar visible", page.locator('#topbar-app-drawer-btn').is_visible())

        # ── 4. Authenticated API with the issued token ────────────────────
        print("Step 4: authenticated API call")
        status = page.evaluate(
            """async (t) => {
                const r = await fetch('/api/system/status', { headers: { Authorization: 'Bearer ' + t } });
                return r.status;
            }""",
            token,
        )
        check("GET /api/system/status with JWT → 200", status == 200, f"got {status}")

        # ── 5. Open Files ─────────────────────────────────────────────────
        print("Step 5: open Files via App Drawer")
        page.click('[data-testid="topbar-app-drawer-btn"]')
        drawer = page.locator('[data-testid="app-drawer-panel"]')
        drawer.wait_for(state="visible", timeout=10_000)
        check("App Drawer opened", "Files" in (page.locator('[data-testid="drawer-item-files"]').inner_text() or ""))
        page.click('[data-testid="drawer-item-files"]')
        win = page.locator('[data-testid="window-files"]')
        win.wait_for(state="visible", timeout=10_000)
        title_text = (win.locator(".window-title").inner_text() or "")
        check("Files window opened", bool(title_text), f"title={title_text!r}")

        # Let the files app render its content (directory listing via /api/files)
        page.wait_for_timeout(3000)
        body = win.locator(".window-body")
        check("Files has rendered content", bool((body.inner_html() or "").strip()))

        # Real-time sync: file-events WS must connect (no 403/401 handshake failures)
        page.wait_for_timeout(2500)
        check("file events WebSocket connected", not ws_errors, "; ".join(ws_errors[:3]))

        shot = "/tmp/ethos-filestation-login.png"
        page.screenshot(path=shot, full_page=False)
        print(f"Screenshot: {shot}")

        browser.close()

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failed): {', '.join(failures)}")
        return 1
    print("RESULT: PASS (all checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
