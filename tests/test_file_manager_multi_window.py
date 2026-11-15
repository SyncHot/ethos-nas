"""Playwright regression — two File Manager windows must both load their listing.

Bug: a second File Manager window stayed blank ("sometimes the list does not
load"). Root cause was window "activeness" being resolved by z-index:

  * `openApp()` gave a new window `zIndex = openWindows.size + 502`, which could
    collide with (or sit below) a window the user had clicked a few times —
    `focusWindow()` bumps z on every mousedown.
  * `initFileManager()` then called a bare `fmLoad()`, which resolves the
    *focused* File Manager window, so the new window's listing (and its state)
    rendered into the FIRST window while the new one stayed empty.

The test reproduces the trigger (click the first window several times, then open
the second one) and asserts both windows end up with a breadcrumb and items.

Run:  ETHOS_TEST_PASSWORD=... .venv/bin/python tests/test_file_manager_multi_window.py
      ETHOS_TEST_BASE=http://127.0.0.1:9002 ...   # against an isolated instance
"""
import json
import os
import sys
import urllib.request

BASE = os.getenv("ETHOS_TEST_BASE", "http://localhost:9000").rstrip("/")
USER = os.getenv("ETHOS_TEST_USER", "marcin")

# Read-only UI test: it never mutates files. Still refuse obviously wrong targets.
_ALLOWED_HOSTS = ("localhost", "127.0.0.1")

JS_OPEN_TWO_WINDOWS = """
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const items = (w) => w.querySelectorAll('.fm-content .fm-file-item, .fm-content .fm-list-row').length;
  const crumb = (w) => (w.querySelector('.fm-breadcrumb')?.textContent || '').trim();

  window.openApp('files');
  await sleep(600);
  const first = document.querySelector('[data-testid="window-files"]');
  if (!first) return { error: 'first window did not open' };

  // Reproduce the trigger: focusWindow() raises the first window's z-index on
  // every mousedown, so the second window must not rely on being topmost.
  const content = first.querySelector('.fm-content');
  for (let i = 0; i < 4; i++) {
    content.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    await sleep(60);
  }

  window.openApp('files');
  await sleep(2500);

  const wins = [...document.querySelectorAll('.app-window')].filter(w => w.querySelector('.fm-container'));
  return {
    windows: wins.map(w => ({
      id: w.id,
      z: Number(w.style.zIndex || 0),
      title: (w.querySelector('.window-title-text')?.textContent || '').trim(),
      crumb: crumb(w),
      items: items(w),
    })),
  };
}
"""


def _require_test_password() -> str:
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw


def _api_login(password: str) -> str:
    body = json.dumps({"username": USER, "password": password}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/auth/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    token = data.get("token") or ""
    if not token:
        raise SystemExit(f"login returned no token: {data}")
    return token


def main() -> int:
    if not any(host in BASE for host in _ALLOWED_HOSTS):
        raise SystemExit(f"refusing to run a UI test against {BASE!r}")
    password = _require_test_password()
    token = _api_login(password)

    from playwright.sync_api import sync_playwright

    failures = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        # Authenticated session + a folder every role can read (non-admins are
        # limited to /home, /mnt, /media by the backend path scope).
        ctx.add_init_script(
            f"localStorage.setItem('token', {json.dumps(token)});"
            "localStorage.setItem('ethos-fm-last-path', '/home');"
        )
        page = ctx.new_page()
        console_errors = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        try:
            page.goto(BASE, wait_until="domcontentloaded")
            page.wait_for_selector("#taskbar", timeout=20_000)
            page.wait_for_timeout(800)

            result = page.evaluate(JS_OPEN_TWO_WINDOWS)
            check("two windows opened", not result.get("error") and len(result.get("windows", [])) == 2,
                  result.get("error") or json.dumps(result))

            wins = result.get("windows", [])
            for i, w in enumerate(wins):
                label = f"window {i + 1} ({w.get('id')})"
                check(f"{label} shows a breadcrumb", bool(w.get("crumb")), json.dumps(w))
                check(f"{label} shows directory items", w.get("items", 0) > 0, json.dumps(w))

            zs = [w.get("z", 0) for w in wins]
            check("window z-indexes are unique", len(set(zs)) == len(zs), f"z={zs}")
            if len(wins) == 2:
                check("second window is on top", zs[1] > zs[0], f"z={zs}")
                check("second window title is numbered", "[2]" in (wins[1].get("title") or ""),
                      wins[1].get("title"))
        finally:
            browser.close()

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failed): {', '.join(failures)}")
        return 1
    print("RESULT: PASS (all checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
