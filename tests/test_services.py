"""Playwright smoke test — Services (unified Service Manager).

Flow: fresh browser → login → open Services from the App Drawer → window opens
(the original bug: clicking did nothing because no renderer was registered) →
table renders rows from both /api/containers and /api/systemd → search filters
→ tabs filter by source.

Run:  .venv/bin/python tests/test_services.py
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
        js_errors: list[str] = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))

        # ── 1. Login ───────────────────────────────────────────────────────
        print("Step 1: login")
        page.goto(BASE, wait_until="networkidle")
        overlay = page.locator("#loginOverlay")
        if overlay.is_visible():
            page.fill('[data-testid="login-username"]', USER)
            page.fill('[data-testid="login-password"]', PASS)
            page.click('[data-testid="login-submit"]')
            overlay.wait_for(state="hidden", timeout=20_000)
            page.wait_for_load_state("networkidle")
        check("logged in (JWT stored)", bool(page.evaluate("localStorage.getItem('token') || ''")))

        # ── 2. Open Services from the App Drawer ───────────────────────────
        print("Step 2: open Services window")
        page.click('[data-testid="topbar-app-drawer-btn"]')
        page.locator('[data-testid="app-drawer-panel"]').wait_for(state="visible", timeout=10_000)
        check("drawer lists Services", page.locator('[data-testid="drawer-item-services"]').count() == 1)
        page.click('[data-testid="drawer-item-services"]')

        win = page.locator('[data-testid="window-services"]')
        try:
            win.wait_for(state="visible", timeout=10_000)
            opened = True
        except Exception:
            opened = False
        check("Services window opens on click (the original bug)", opened)
        if not opened:
            page.screenshot(path="/tmp/ethos-services-fail.png")
            browser.close()
            print(f"\nRESULT: FAIL ({len(failures)} failed): {', '.join(failures)}")
            return 1

        # ── 3. Data renders from both sources ──────────────────────────────
        print("Step 3: table renders rows")
        page.wait_for_function(
            "() => { const t = document.querySelector('#servicesTable tbody tr'); "
            "return t && !t.textContent.includes('Loading'); }",
            timeout=20_000,
        )
        row_count = page.locator("#servicesTable tbody tr").count()
        check("table has rows", row_count > 0, f"rows={row_count}")

        # Cross-check counts against the raw APIs (same token the page uses)
        api_counts = page.evaluate(
            """async () => {
                const t = localStorage.getItem('token');
                const h = { Authorization: 'Bearer ' + t };
                const [c, s] = await Promise.all([
                    fetch('/api/containers', { headers: h }).then(r => r.json()),
                    fetch('/api/systemd', { headers: h }).then(r => r.json()),
                ]);
                return { containers: c.containers.length, units: s.services.length };
            }"""
        )
        expected = api_counts["containers"] + api_counts["units"]
        check("row count matches API (containers+systemd)", row_count == expected,
              f"rows={row_count} expected={expected} ({api_counts})")

        footer = page.locator('[data-testid="services-footer"]').inner_text()
        check("footer shows totals", str(expected) in footer, footer)
        chip_run = page.locator('[data-testid="services-chip-running"]').inner_text()
        check("running chip populated", "running" in chip_run and not chip_run.endswith("0 running"), chip_run)

        # ── 4. Search filters live ─────────────────────────────────────────
        print("Step 4: search")
        page.fill('[data-testid="services-search"]', "home-assistant")
        page.wait_for_timeout(300)
        rows = page.locator("#servicesTable tbody tr").all_inner_texts()
        check("search narrows results", len(rows) >= 1 and all("home-assistant" in r.lower() for r in rows), str(rows)[:200])
        page.fill('[data-testid="services-search"]', "zzz-no-such-service-zzz")
        page.wait_for_timeout(300)
        check("search empty state", "No services match" in (page.locator("#servicesTable tbody").inner_text() or ""))
        page.fill('[data-testid="services-search"]', "")

        # ── 5. Tabs filter by source ───────────────────────────────────────
        print("Step 5: tabs")
        page.click('[data-testid="services-tab-docker"]')
        page.wait_for_timeout(300)
        check("Containers tab row count", page.locator("#servicesTable tbody tr").count() == api_counts["containers"])
        page.click('[data-testid="services-tab-systemd"]')
        page.wait_for_timeout(300)
        check("Systemd tab row count", page.locator("#servicesTable tbody tr").count() == api_counts["units"])
        page.click('[data-testid="services-tab-all"]')

        # ── 6. Action buttons present + confirm dialog wiring ──────────────
        print("Step 6: actions")
        check("start/stop/restart buttons rendered",
              page.locator("#servicesTable .svc-btn").count() > 0)
        first_btn = page.locator("#servicesTable .svc-btn").first
        label = (first_btn.get_attribute("title") or "").lower()
        if "stop" in label:
            first_btn.click()
            modal_ok = page.locator('[data-testid="ethos-modal-ok"]')
            try:
                modal_ok.wait_for(state="visible", timeout=5_000)
                check("confirm dialog appears before action", True)
                page.click('[data-testid="ethos-modal-cancel"]')  # never actually stop a service in tests
            except Exception as e:
                check("confirm dialog appears before action", False, str(e))

        shot = "/tmp/ethos-services.png"
        page.screenshot(path=shot)
        print(f"Screenshot: {shot}")
        check("no JS errors on the page", not js_errors, "; ".join(js_errors[:3]))

        browser.close()

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failed): {', '.join(failures)}")
        return 1
    print("RESULT: PASS (all checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
