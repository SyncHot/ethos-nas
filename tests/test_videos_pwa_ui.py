#!/usr/bin/env python3
"""Videos PWA E2E (Playwright).

Boots an isolated EthOS instance with a tiny generated library (a show with two
subtitled episodes + one movie) and checks the phone flows that the static
guards cannot see:

    .venv/bin/python tests/test_videos_pwa_ui.py

Covers: library tabs, series → season → episode, subtitle track attachment,
up-next overlay, batch download in the default version, offline unlock after the
token is gone, and on-device resume restored with no network.
"""

from __future__ import annotations

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
DATA = Path(tempfile.mkdtemp(prefix="ethos-videos-pwa-"))
MEDIA = Path(tempfile.mkdtemp(prefix="ethos-videos-media-"))
SECRET = "videos-pwa-test"
os.environ.update({
    "ETHOS_DATA_DIR": str(DATA),
    "ETHOS_JWT_SECRET": SECRET,
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_media() -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    show = MEDIA / "Show"
    show.mkdir(parents=True, exist_ok=True)
    for name in ("Show S01E01 Pilot.mp4", "Show S01E02 Second.mp4"):
        proc = subprocess.run([
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=8:size=320x180:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "64k",
            str(show / name),
        ], capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            print(proc.stderr[-400:])
            return False
    (show / "Show S01E01 Pilot.srt").write_text(
        "1\n00:00:01,000 --> 00:00:03,000\nHello subtitle\n", encoding="utf-8")
    movie = MEDIA / "Some.Movie.2024.mkv"
    proc = subprocess.run([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=duration=8:size=320x180:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "64k",
        str(movie),
    ], capture_output=True, text=True, timeout=120)
    return proc.returncode == 0


def api(base: str, token: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, OSError):
        return 0, {}


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed — skipping")
        return 0
    if not make_media():
        print("ffmpeg is not available — skipping")
        return 0

    port = free_port()
    env = {**os.environ, "ETHOS_PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    token = _create_token("videos-pwa", "videos-pwa", "admin")
    errors: list[str] = []
    try:
        for _ in range(40):
            code, _ = api(base, token, "GET", "/api/videos/status")
            if code in (200, 401):
                break
            time.sleep(0.25)
        api(base, token, "POST", "/api/videos/download-settings", {"quality": "tiny"})
        api(base, token, "POST", "/api/videos/config",
            {"roots": [str(MEDIA)], "options": {}, "tmdb": {}})

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 390, "height": 844})
            page = context.new_page()
            page.add_init_script(
                f"localStorage.setItem('token', {json.dumps(token)});"
                "localStorage.removeItem('vd-recent-searches');")
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.goto(base + "/videos")
            page.wait_for_selector("#vd-content .vd-card", timeout=30000)
            page.wait_for_function(
                "() => navigator.serviceWorker && navigator.serviceWorker.controller",
                timeout=30000)

            tabs = page.locator(".vd-tab").all_inner_texts()
            check("library tabs render", len(tabs) == 4 and "Series1" in tabs, str(tabs))

            page.click('[data-tab="series"]')
            page.wait_for_selector('[data-action="show"]', timeout=10000)
            page.click('[data-action="show"]')
            page.wait_for_selector(".vd-show-head", timeout=10000)
            check("series view lists the season",
                  page.locator("#vd-content .vd-card").count() == 2,
                  str(page.locator("#vd-content .vd-card").count()))
            check("season has a download action",
                  page.locator('[data-action="download-season"]').count() == 1)

            page.locator("#vd-content .vd-card").first.click()
            page.wait_for_selector("#vd-sheet-title", timeout=10000)
            check("episode sheet offers the next episode",
                  page.locator('.vd-detail-actions [data-action="play"]').count() >= 2)
            page.locator('.vd-detail-actions [data-action="play"]').first.click()
            page.wait_for_selector("#vd-player:not([hidden])", timeout=20000)
            page.wait_for_function(
                "() => document.getElementById('vd-video').duration > 1", timeout=20000)
            check("player attaches a subtitle track",
                  page.locator("#vd-sub").is_hidden() is False
                  and page.locator("#vd-video track").count() >= 1,
                  f"track={page.locator('#vd-video track').count()}")
            page.evaluate(
                "() => { const v = document.getElementById('vd-video'); "
                "v.currentTime = Math.max(0, v.duration - 0.4); }")
            page.wait_for_selector("#vd-upnext:not([hidden])", timeout=15000)
            check("up-next overlay appears at the end",
                  "Up next" in page.inner_text("#vd-upnext-text"),
                  page.inner_text("#vd-upnext-text"))
            page.click("#vd-upnext-cancel")
            page.click("#vd-player-back")
            page.wait_for_timeout(500)

            # Queue both episodes in the default (480p) version.
            page.click("#vd-select-btn")
            cards = page.locator("#vd-content .vd-card")
            cards.nth(0).click()
            cards.nth(1).click()
            page.click("#vd-select-download")
            page.wait_for_timeout(1500)
            _, listing = api(base, token, "GET", "/api/videos/exports")
            jobs = listing.get("jobs", [])
            check("batch queued both episodes in the default version",
                  len(jobs) == 2 and all(job["quality"] == "tiny" for job in jobs),
                  str([(job["name"], job["quality"]) for job in jobs]))

            page.click("#vd-downloads-btn")
            page.wait_for_selector("[data-action='play-local']", timeout=60000)
            watched = page.locator("[data-action='play-local']").first.get_attribute("data-path")
            page.locator("[data-action='play-local']").first.click()
            page.wait_for_selector("#vd-player:not([hidden])", timeout=20000)
            page.wait_for_function(
                "() => document.getElementById('vd-video').duration > 1", timeout=20000)
            page.evaluate(
                "() => { const v = document.getElementById('vd-video'); v.pause(); v.currentTime = 6; }")
            page.wait_for_timeout(1200)
            page.click("#vd-player-back")
            page.wait_for_timeout(800)

            # Trip mode: no token, no network — downloads must still play.
            page.evaluate("() => localStorage.removeItem('token')")
            context.set_offline(True)
            page.reload()
            page.wait_for_selector("#vd-app:not([hidden])", timeout=30000)
            page.wait_for_selector("#vd-content .vd-card", timeout=20000)
            check("offline unlock shows the downloads",
                  page.locator("#vd-content .vd-card").count() >= 1)
            page.click("#vd-downloads-btn")
            page.wait_for_selector("[data-action='play-local']", timeout=15000)
            page.click(f"[data-action='play-local'][data-path='{watched}']")
            page.wait_for_selector("#vd-player:not([hidden])", timeout=20000)
            page.wait_for_function(
                "() => document.getElementById('vd-video').duration > 1", timeout=20000)
            page.wait_for_timeout(1500)
            resumed = page.evaluate("() => document.getElementById('vd-video').currentTime")
            check("offline playback resumes where it stopped", resumed >= 2.5,
                  f"currentTime={round(resumed, 2)}")
            check("no page errors", not errors, "; ".join(errors[:3]))
            context.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=15)
        except Exception:
            proc.kill()
            proc.communicate()
        shutil.rmtree(DATA, ignore_errors=True)
        shutil.rmtree(MEDIA, ignore_errors=True)

    failed = [row for row in RESULTS if not row[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
