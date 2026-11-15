"""Playwright end-to-end test — desktop Trash shortcut.

Flow: login → the pinned Trash icon renders in the bottom-right corner → its
badge matches GET /api/files/trash → right-click menu offers Open / Empty /
Refresh → clicking the icon opens Files in the Trash view.

Emptying the shared trash is destructive, so that step only runs with
ETHOS_TEST_ALLOW_TRASH_EMPTY=1 (same flag as tests/test_file_station_broadcasts.py).

Run:  .venv/bin/python tests/test_desktop_trash_ui.py   (server up on :9000, or
        set ETHOS_TEST_BASE to an isolated instance)
"""
import os
import sys

from playwright.sync_api import sync_playwright


def _require_test_password() -> str:
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw


BASE = os.getenv("ETHOS_TEST_BASE", "http://localhost:9000")
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()
ALLOW_EMPTY = os.environ.get("ETHOS_TEST_ALLOW_TRASH_EMPTY") == "1"


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        js_errors: list[str] = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))

        # ── 1. Login ─────────────────────────────────────────────────────
        print(f"Step 1: login as {USER} on {BASE}")
        page.goto(BASE)
        page.wait_for_selector("#loginOverlay", state="visible", timeout=20_000)
        page.fill('[data-testid="login-username"]', USER)
        page.fill('[data-testid="login-password"]', PASS)
        page.click('[data-testid="login-submit"]')
        page.wait_for_selector("#loginOverlay", state="hidden", timeout=20_000)

        # The seed marker is per browser profile; clear it so this run proves the
        # default pinning works even if a previous run unpinned the icon.
        page.evaluate("""() => {
            for (const k of Object.keys(localStorage)) {
                if (k.startsWith('ethos-desktop-trash-seeded:')) localStorage.removeItem(k);
            }
        }""")
        page.reload()
        page.wait_for_selector("#loginOverlay", state="hidden", timeout=20_000)

        # ── 2. Icon renders bottom-right ─────────────────────────────────
        print("Step 2: Trash icon placement")
        icon = page.locator('[data-testid="desktop-icon-trash"]')
        icon.wait_for(state="visible", timeout=15_000)
        check("Trash icon present on desktop", icon.count() == 1)

        box = icon.bounding_box()
        check(
            "Trash icon sits in the bottom-right corner",
            bool(box) and (box["x"] + box["width"]) > 1440 * 0.75 and (box["y"] + box["height"]) > 900 * 0.5,
            f"box={box}",
        )

        # ── 3. Badge matches the API ─────────────────────────────────────
        print("Step 3: badge count")
        total = page.evaluate("""async () => {
            const r = await fetch('/api/files/trash?limit=1', {
                headers: { Authorization: 'Bearer ' + localStorage.getItem('token') },
            });
            return (await r.json()).total;
        }""")
        page.wait_for_timeout(800)  # let the badge poll/fill settle
        badge = page.locator('[data-testid="desktop-trash-badge"]')
        if total:
            shown = badge.is_visible() and badge.inner_text().strip() in (str(total), "99+")
            check("badge shows the trash item count", shown, f"total={total} badge={badge.inner_text() if badge.count() else '<missing>'}")
        else:
            check("badge hidden when the trash is empty", badge.count() == 0 or not badge.is_visible())

        # ── 4. Right-click menu ──────────────────────────────────────────
        print("Step 4: context menu")
        icon.click(button="right")
        page.wait_for_selector('[data-testid="icon-ctx-trash-empty"]', timeout=5_000)
        check("menu offers Empty Trash", page.locator('[data-testid="icon-ctx-trash-empty"]').count() == 1)
        check("menu offers Refresh count", page.locator('[data-testid="icon-ctx-trash-refresh"]').count() == 1)
        page.mouse.click(60, 400)  # close the menu

        # ── 5. Click opens Files' Trash view ─────────────────────────────
        print("Step 5: open the Trash view")
        icon.click()
        page.wait_for_selector('[data-testid="window-files"]', timeout=15_000)
        page.wait_for_function(
            """() => {
                const crumb = document.querySelector('[data-testid="window-files"] .fm-breadcrumb');
                return !!(crumb && crumb.textContent.includes('Trash'));
            }""",
            timeout=15_000,
        )
        check("clicking the icon opens the Trash view", True)

        # ── 6. Optional destructive check ────────────────────────────────
        if ALLOW_EMPTY and "localhost" in BASE:
            print("Step 6: empty the trash (ETHOS_TEST_ALLOW_TRASH_EMPTY=1)")
            # The Trash icon sits behind app windows; close Files first.
            page.click('[data-testid="window-files"] .close-btn')
            page.wait_for_selector('[data-testid="window-files"]', state="detached", timeout=10_000)
            icon.click(button="right")
            page.click('[data-testid="icon-ctx-trash-empty"]')
            page.wait_for_selector('[data-testid="ethos-modal"]', timeout=5_000)
            page.click('[data-testid="ethos-modal-ok"]')
            page.wait_for_function(
                """() => {
                    const b = document.getElementById('desktopTrashBadge');
                    return !b || b.hidden;
                }""",
                timeout=15_000,
            )
            check("badge cleared after emptying", True)
        else:
            print("Step 6: skipping empty-trash (set ETHOS_TEST_ALLOW_TRASH_EMPTY=1 on a throwaway instance)")

        check("no uncaught JS errors", not js_errors, "; ".join(js_errors[:3]))
        browser.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("Desktop Trash UI test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
