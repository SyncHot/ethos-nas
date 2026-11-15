"""OBSOLETE (2026-11-04) — kept for reference, not runnable against the current UI.

This suite drives DOM hooks that the rewritten web player no longer renders:
``[data-music-tab]``, ``#musicLibrary``, ``[data-mini-*]``, ``#musicSearchInput``,
``window.__musicAudio``. Use ``tests/test_music_smoke.py`` (tabs, radio, API, no
JS errors) until the 50-track scenarios are ported to the new markup.

Playwright end-to-end test — Music app (50-track playback suite).

Covers both playback paths of the music app:

1. **Library path** — 50 generated m4a tracks served by ``/api/music/library-file``
   (Range serving over local files): every track is played through the UI and
   asserted to actually start playing with the correct title; plus seek,
   next/prev, auto-advance on track end, repeat-one, shuffle and the full
   player. The download dir is pointed at a temp folder for the run (restored
   afterwards), so the user's real library is untouched.

2. **YouTube path** — search → play through ``/api/music/stream`` (yt-dlp
   resolve + Range proxy): first two results are played, seeked and advanced;
   the stream endpoint is also exercised directly with ranged requests.

Every step asserts against both the DOM/audio element and the REST API.
Uncaught page JS errors collected during the whole run fail the suite.

Run:  .venv/bin/python tests/test_music.py   (server must be up on :9000)
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
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()
N_TRACKS = 50
TEST_DIR = Path(f"/tmp/ethos-music-pwtest-{int(time.time())}")


# ── API helper (issues its own JWT, independent of the browser) ───────────────

def _token() -> str:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from backend.config import settings

    def b64url(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).decode().rstrip("=")

    h = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64url(json.dumps({
        "sub": "pw-test", "username": USER, "role": "admin",
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = b64url(hmac.new(settings.JWT_SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


TOK = _token()


def api(path: str, method: str = "GET", body=None, headers=None):
    """Return (status, lowercase-headers-dict, raw-bytes) for an API call."""
    hdrs = {"Authorization": f"Bearer {TOK}"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=120)
        return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read(4096)


def lib_url(name: str) -> str:
    return "/api/music/library-file?" + urllib.parse.urlencode({"name": name, "token": TOK})


# ── Test library generation (50 m4a tracks via ffmpeg) ───────────────────────

def make_track(path: Path, freq: float, duration: int) -> None:
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency={freq:.0f}:duration={duration}",
        "-f", "lavfi", "-i", f"sine=frequency={freq * 1.5:.0f}:duration={duration}",
        "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0,volume=0.3",
        "-c:a", "aac", "-b:a", "96k", str(path),
    ], check=True)


def build_library() -> list[dict]:
    """Create N_TRACKS m4a files + meta sidecars in TEST_DIR.

    Track 50 (newest → first row of the library view) is short (4 s) so the
    auto-advance / repeat-one tests don't have to wait for long tracks.
    Returns items in the same order the API will list them (mtime desc).
    """
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    durations = [20, 35, 60, 90, 120, 180]
    base_mtime = time.time() - N_TRACKS * 60
    for i in range(1, N_TRACKS + 1):
        d = 4 if i == N_TRACKS else durations[(i - 1) % len(durations)]
        name = f"Test Artist - PW Song {i:02d}"
        make_track(TEST_DIR / f"{name}.m4a", freq=200 + i * 7, duration=d)
        meta = {
            "video_id": "",
            "title": f"PW Song {i:02d}",
            "artist": "Test Artist",
            "thumbnail": "",
            "duration": d,
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        (TEST_DIR / f"{name}.m4a.meta.json").write_text(json.dumps(meta, indent=2))
        # stagger mtimes so the API order is deterministic: song 50 first … song 1 last
        t = base_mtime + i * 60
        for suffix in ("", ".meta.json"):
            os.utime(TEST_DIR / f"{name}.m4a{suffix}", (t, t))
    return [{"title": f"PW Song {i:02d}"} for i in range(N_TRACKS, 0, -1)]


# ── Audio element state (read from the page) ─────────────────────────────────

AUDIO_STATE = """() => {
    const a = window.__musicAudio;
    if (!a) return null;
    return { paused: a.paused, readyState: a.readyState, t: a.currentTime, d: a.duration,
             err: a.error ? a.error.code : 0 };
}"""


def wait_until(page, expr: str, timeout_ms: int = 15_000) -> bool:
    try:
        page.wait_for_function(expr, timeout=timeout_ms)
        return True
    except Exception:
        return False


def audio_state(page):
    return page.evaluate(AUDIO_STATE)


def wait_playing(page, title: str | None = None, timeout_ms: int = 20_000) -> bool:
    """Wait until the app's audio element is playing (and optionally on `title`)."""
    if title:
        expr = f"""() => {{
            const a = window.__musicAudio;
            const el = document.getElementById('miniTitle');
            return a && !a.paused && a.readyState >= 2 && a.currentTime > 0.05
                   && el && el.textContent === {title!r};
        }}"""
    else:
        expr = """() => {
            const a = window.__musicAudio;
            return a && !a.paused && a.readyState >= 2 && a.currentTime > 0.05;
        }"""
    return wait_until(page, expr, timeout_ms)


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    original_dir = None
    items: list[dict] = []

    # ── Setup: point the music library at a temp dir with 50 generated tracks ─
    print("Setup: generating 50-track test library in", TEST_DIR)
    s, _, b = api("/api/music/config")
    original_dir = json.loads(b).get("download_dir") if s == 200 else None
    items = build_library()
    s, _, b = api("/api/music/config", "POST", {"download_dir": str(TEST_DIR)})
    assert s == 200, f"config swap failed: {s} {b[:200]}"

    try:
        # ── API-level checks on all 50 files (Range serving) ───────────────────
        print(f"\nStep 1: /api/music/library lists {N_TRACKS} tracks")
        s, _, b = api("/api/music/library")
        lib = json.loads(b).get("items", []) if s == 200 else []
        check("library count == 50", len(lib) == N_TRACKS, f"got {len(lib)}")
        check("API order matches generated order",
              [it["title"] for it in lib] == [it["title"] for it in items],
              str([it["title"] for it in lib[:3]]))

        print("\nStep 1b: /api/music/config exposes ytdlp_version")
        s, _, b = api("/api/music/config")
        cfg = json.loads(b) if s == 200 else {}
        check("config has non-empty ytdlp_version", bool(cfg.get("ytdlp_version")), str(cfg))

        print("\nStep 2: ranged GET on every library file (first KB + mid-file)")
        range_fail = []
        for it in lib:
            s, h, body = api(lib_url(it["name"]), headers={"Range": "bytes=0-1023"})
            cr_ok = h.get("content-range", "") == f"bytes 0-1023/{it['size']}"
            if not (s == 206 and len(body) == 1024 and cr_ok):
                range_fail.append(f"{it['name']}: {s} cr={h.get('content-range')} n={len(body)}")
            mid = it["size"] // 2
            s, h, body = api(lib_url(it["name"]), headers={"Range": f"bytes={mid}-{mid + 1023}"})
            if not (s == 206 and len(body) == 1024):
                range_fail.append(f"{it['name']} mid: {s} n={len(body)}")
        check("all 50 files serve correct ranged bytes", not range_fail, "; ".join(range_fail[:3]))

        s, _, _ = api(lib_url(lib[0]["name"]), headers={"Range": f"bytes={lib[0]['size'] + 10}-"})
        check("out-of-range request → 416", s == 416, str(s))

        # ── Browser: login + open the app ─────────────────────────────────────
        print("\nStep 3: PAM login + open Music window")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            js_errors: list[str] = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))

            page.goto(BASE, wait_until="networkidle")
            page.fill('[data-testid="login-username"]', USER)
            page.fill('[data-testid="login-password"]', PASS)
            page.click('[data-testid="login-submit"]')
            page.wait_for_selector("#loginOverlay", state="hidden", timeout=20_000)
            check("JWT stored after login", bool(page.evaluate("localStorage.getItem('token') || ''")))

            page.evaluate("window.openApp('music')")
            # NOTE (T-0008): this test asserts the pre-rewrite music UI
            # (#musicSearchInput, #musicLibrary, #miniBarWrap, #musicFull …).
            # The current app (e739f0c) renders the em-* UI (em-nav / em-searchfield /
            # em-mini-progress) and has none of these elements, so this wait always
            # times out regardless of render budget. The 15s here is NOT the bug —
            # the whole suite needs a rewrite against the current UI (tracked
            # separately). Do not "fix" this by raising the timeout.
            page.wait_for_selector("#musicSearchInput", timeout=15_000)
            check("Music app rendered (search view)", True)

            # ── Library tab shows 50 rows ─────────────────────────────────────
            print("\nStep 4: library tab")
            page.click('[data-music-tab="library"]')
            ok = wait_until(page, """() => {
                const b = document.querySelector('[data-music-tab="library"]');
                return b && b.textContent.includes('50');
            }""", timeout_ms=20_000)
            check("tab label shows Library (50)", ok)
            n_rows = page.evaluate("document.querySelectorAll('#musicLibrary [data-lib-idx]').length")
            check("50 rows rendered", n_rows == N_TRACKS, str(n_rows))

            # ── Play all 50 tracks through the UI ─────────────────────────────
            print(f"\nStep 5: play all {N_TRACKS} library tracks (click row → assert playing)")
            t0 = time.time()
            failed_tracks = []
            for i in range(N_TRACKS):
                page.click(f'#musicLibrary [data-lib-idx="{i}"]')
                if not wait_playing(page, title=items[i]["title"], timeout_ms=20_000):
                    failed_tracks.append(i + 1)
            check("all 50 tracks started playback", not failed_tracks, f"failed: {failed_tracks}")
            print(f"      (took {time.time() - t0:.0f}s)")

            # ── Seek via the mini progress bar ────────────────────────────────
            print("\nStep 6: seek to ~50% via mini progress bar")
            page.click('#musicLibrary [data-lib-idx="4"]')  # a long track (120 s)
            wait_playing(page, title=items[4]["title"])
            st = audio_state(page)
            half = st["d"] / 2
            box = page.locator("#miniBarWrap").bounding_box()
            assert box, "mini progress bar not visible"
            page.click("#miniBarWrap", position={"x": box["width"] * 0.5, "y": box["height"] / 2})
            ok = wait_until(page, f"""() => Math.abs(window.__musicAudio.currentTime - {half:.1f}) < 4.0""", timeout_ms=8_000)
            check("seek landed near 50%", ok, f"t={audio_state(page)['t']:.1f} want≈{half:.1f}")

            # ── Next / previous buttons ───────────────────────────────────────
            print("\nStep 7: next/previous buttons")
            page.click("[data-mini-next]")
            check("next → track advances", wait_playing(page, title=items[5]["title"]))
            page.click("[data-mini-prev]")
            check("previous → back one track", wait_playing(page, title=items[4]["title"]))

            # ── Auto-advance when a track ends (short 4 s track at row 0) ─────
            print("\nStep 8: auto-advance on track end")
            page.click('#musicLibrary [data-lib-idx="0"]')
            wait_playing(page, title=items[0]["title"])
            page.evaluate("window.__musicAudio.currentTime = window.__musicAudio.duration - 0.3")
            check("ended → next track auto-plays",
                  wait_playing(page, title=items[1]["title"], timeout_ms=12_000), str(audio_state(page)))

            # ── Repeat-one restarts the same track ────────────────────────────
            print("\nStep 9: repeat-one")
            page.click('#musicLibrary [data-lib-idx="2"]')  # 60 s track
            wait_playing(page, title=items[2]["title"])
            page.click("[data-mini-repeat]")  # off → all
            page.click("[data-mini-repeat]")  # all → one
            page.evaluate("window.__musicAudio.currentTime = window.__musicAudio.duration - 0.3")
            ok = wait_until(page, """() => {
                const a = window.__musicAudio;
                return !a.paused && a.currentTime < 1.5;   // restarted from the top
            }""", timeout_ms=12_000)
            check("repeat-one restarts same track", ok, str(audio_state(page)))
            page.click("[data-mini-repeat]")  # one → off

            # ── Shuffle picks a different track on next ───────────────────────
            print("\nStep 10: shuffle")
            cur_idx = page.evaluate("""() => [...document.querySelectorAll('#musicLibrary [data-lib-idx]')]
                .findIndex(r => r.querySelector('[data-row-pos]') &&
                                r.querySelector('.text-sm').textContent ===
                                document.getElementById('miniTitle').textContent)""")
            page.click("[data-mini-shuffle]")  # on
            page.click("[data-mini-next]")
            ok = wait_playing(page, timeout_ms=20_000)
            new_idx = page.evaluate("""() => [...document.querySelectorAll('#musicLibrary [data-lib-idx]')]
                .findIndex(r => r.querySelector('.text-sm').textContent ===
                                document.getElementById('miniTitle').textContent)""")
            check("shuffle next plays a different track", ok and new_idx >= 0 and new_idx != cur_idx,
                  f"before={cur_idx} after={new_idx}")
            page.click("[data-mini-shuffle]")  # off

            # ── Full player: queue + seek slider ──────────────────────────────
            print("\nStep 11: full player (queue list + seek slider)")
            page.click('#musicLibrary [data-lib-idx="2"]')  # back to a known long track
            wait_playing(page, title=items[2]["title"])
            page.click("[data-mini-expand]")
            check("full player opens", wait_until(page, "() => !!document.getElementById('musicFull')", timeout_ms=8_000))
            n_q = page.evaluate("document.querySelectorAll('#musicQueue [data-q-idx]').length")
            check("queue lists all 50 tracks", n_q == N_TRACKS, str(n_q))
            st = audio_state(page)
            target = max(10.0, min(st["d"] - 10, 42.0))
            page.evaluate(f"""() => {{
                const s = document.getElementById('fullSeek');
                s.value = {target:.0f};
                s.dispatchEvent(new Event('input', {{bubbles: true}}));
                s.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}""")
            ok = wait_until(page, f"""() => Math.abs(window.__musicAudio.currentTime - {target:.1f}) < 2.0""", timeout_ms=8_000)
            check("full-player seek slider works", ok, f"t={audio_state(page)['t']:.1f} want≈{target:.0f}")
            page.click("[data-full-close]")

            # ── YouTube path: search → stream proxy → play/seek/next ──────────
            print("\nStep 12: YouTube search + /api/music/stream playback")
            page.click('[data-music-tab="search"]')
            page.fill("#musicSearchInput", "never gonna give you up rick astley")
            ok = wait_until(page, "() => document.querySelectorAll('#musicResults [data-idx]').length >= 2", timeout_ms=45_000)
            check("search returns results", ok)
            if ok:
                first_title = page.evaluate(
                    "document.querySelector('#musicResults [data-idx] .text-sm')?.textContent || ''")
                page.click("#musicResults [data-idx='0']")
                ok = wait_playing(page, title=first_title or None, timeout_ms=45_000)
                st = audio_state(page)
                check("YouTube track 1 plays via stream proxy (duration > 30s)",
                      ok and st["d"] > 30, str(st))

                # direct ranged request against the same video id
                vid = page.evaluate("""() => {
                    const m = (window.__musicAudio.src || '').match(/video_id=([^&]+)/);
                    return m ? decodeURIComponent(m[1]) : '';
                }""")
                if vid:
                    s, h, body = api(f"/api/music/stream?video_id={vid}&token=" + urllib.parse.quote(TOK),
                                     headers={"Range": "bytes=0-2047"})
                    check("stream API ranged GET → 206/200 with bytes",
                          s in (200, 206) and len(body) >= 1024, f"{s} n={len(body)}")

                # seek to the middle of the song through the proxy
                st = audio_state(page)
                if st["d"] > 60:
                    mid = st["d"] / 2
                    page.evaluate(f"window.__musicAudio.currentTime = {mid:.1f}")
                    ok = wait_until(page, f"""() => Math.abs(window.__musicAudio.currentTime - {mid:.1f}) < 3.0""", timeout_ms=20_000)
                    check("seek to mid-song through proxy", ok, f"t={audio_state(page)['t']:.1f}")

                # next result plays too
                second_title = page.evaluate(
                    "document.querySelector('#musicResults [data-idx=\"1\"] .text-sm')?.textContent || ''")
                page.click("[data-mini-next]")
                check("YouTube track 2 plays (next)",
                      wait_playing(page, title=second_title or None, timeout_ms=45_000), str(audio_state(page)))

            # ── No uncaught JS errors during the whole run ────────────────────
            print("\nStep 13: page JS errors")
            check("no uncaught page JS errors", not js_errors, "; ".join(js_errors[:3]))

            browser.close()

    finally:
        # ── Cleanup: restore original download dir, remove temp library ───────
        if original_dir is not None:
            api("/api/music/config", "POST", {"download_dir": original_dir})
        shutil.rmtree(TEST_DIR, ignore_errors=True)
        print(f"\nCleanup done (restored download_dir={original_dir}, removed {TEST_DIR})")

    print(f"\n{'=' * 60}\nRESULT: {'ALL PASS' if not failures else str(len(failures)) + ' FAILURES'}"
          + ("\n" + "\n".join(" - " + f for f in failures) if failures else ""))
    return len(failures)


if __name__ == "__main__":
    sys.exit(main())
