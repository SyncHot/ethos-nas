"""Smoke test — Music web app (current UI, no fixtures needed).

Checks the parts of the player that are easy to break and cheap to verify:

* radio catalog API (``GET /api/music/radio/stations``) and its payload shape,
* the six bottom-nav tabs render (Start / Szukaj / Podcasty / Radio / Biblioteka / Ustawienia),
* Radio tab: station list + category chips + live playback (``● NA ŻYWO`` in the full player),
* no uncaught page errors during the whole run.

Run:  .venv/bin/python tests/test_music_smoke.py
Env:  ETHOS_TEST_BASE (default http://localhost:9000), ETHOS_TEST_USER, ETHOS_TEST_PASS

The 50-track playback suite lives in ``test_music.py``; it targets DOM hooks the
rewritten player no longer exposes, so it is currently obsolete.
"""
import base64
import hashlib
import hmac
import json
import os

def _require_test_password() -> str:
    """Test credentials are never committed — export ETHOS_TEST_PASSWORD."""
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
USER = os.environ.get("ETHOS_TEST_USER", "marcin")
PASS = _require_test_password()

TABS = ["Start", "Szukaj", "Podcasty", "Radio", "Biblioteka", "Ustawienia"]


def _token() -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from backend.config import settings

    def b64url(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).decode().rstrip("=")

    h = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64url(json.dumps({
        "sub": "smoke", "username": USER, "role": "admin",
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = b64url(hmac.new(settings.JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


def api(path: str) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {_token()}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"detail": e.read(300).decode(errors="replace")}


failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# ── API ──────────────────────────────────────────────────────────────────────
status, body = api("/api/music/radio/stations")
stations = body.get("stations") or []
check("radio API 200", status == 200, f"status={status}")
check("radio catalog non-empty", len(stations) >= 20, f"{len(stations)} stations")
check("radio categories", len(body.get("categories") or []) >= 3, str(body.get("categories")))
check(
    "radio station shape",
    all(s.get("id") and s.get("name") and s.get("stream_url") for s in stations[:5]),
)

status, body = api("/api/music/state")
check("state API 200", status == 200, f"status={status}")

# SSRF guard: a localhost feed must be rejected (never fetched).
status, body = api("/api/music/podcast-feed?url=" + urllib.parse.quote("http://127.0.0.1:9000/api/system/status", safe=""))
check("podcast-feed blocks localhost", status == 422, f"status={status} {body.get('detail','')}")

# ── UI ───────────────────────────────────────────────────────────────────────
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(BASE, wait_until="networkidle")
    if page.locator('[data-testid="login-username"]').count():
        page.fill('[data-testid="login-username"]', USER)
        page.fill('[data-testid="login-password"]', PASS)
        page.click('[data-testid="login-submit"]')
        page.wait_for_timeout(2500)

    page.evaluate(
        """async () => {
            const m = await import('/static/apps/music.js');
            const host = document.createElement('div');
            host.id = 'smoke';
            host.style.cssText = 'position:fixed;inset:0;z-index:99999;background:#121212';
            document.body.appendChild(host);
            m.renderMusicPage(host);
        }"""
    )
    page.wait_for_timeout(1500)

    nav = page.evaluate(
        "() => [...document.querySelectorAll('#smoke nav button')].map(b => b.textContent.trim())"
    )
    check("six nav tabs", nav == TABS, str(nav))

    for label in TABS:
        page.evaluate(
            """(label) => {
                const b = [...document.querySelectorAll('#smoke nav button')].find(x => x.textContent.includes(label));
                b.click();
            }""",
            label,
        )
        page.wait_for_timeout(800)
        text = page.evaluate("() => (document.querySelector('#smoke .em-content') || {}).innerText || ''")
        check(f"tab renders: {label}", len(text.strip()) > 0)

    page.evaluate(
        """() => {
            const b = [...document.querySelectorAll('#smoke nav button')].find(x => x.textContent.includes('Radio'));
            b.click();
        }"""
    )
    page.wait_for_timeout(2500)
    rows = page.evaluate("() => document.querySelectorAll('#smoke .em-row').length")
    chips = page.evaluate("() => [...document.querySelectorAll('#smoke .em-btn')].map(e => e.textContent.trim())")
    check("radio rows rendered", rows >= 20, f"{rows} rows")
    check("radio category chips", len(chips) >= 4, str(chips))

    page.evaluate("() => document.querySelectorAll('#smoke .em-row')[0].click()")
    page.wait_for_timeout(1200)
    page.evaluate(
        """() => {
            const bar = document.querySelector('#smoke .em-mini');
            if (bar) bar.click();
        }"""
    )
    page.wait_for_timeout(1200)
    full = page.evaluate("() => (document.querySelector('#smoke .em-full') || {}).innerText || ''")
    check("live player badge", "NA ŻYWO" in full, full.replace("\n", " | ")[:80])

    check("no page errors", not errors, "; ".join(errors))
    browser.close()

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("ALL CHECKS PASSED")
