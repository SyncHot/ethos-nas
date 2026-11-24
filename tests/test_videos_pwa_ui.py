#!/usr/bin/env python3
"""Videos PWA E2E (Playwright).

Boots an isolated EthOS instance with a tiny generated library (a show with two
subtitled episodes + one movie) and checks the phone flows that the static
guards cannot see:

    .venv/bin/python tests/test_videos_pwa_ui.py

Covers: library tabs, series → season → episode, subtitle track attachment,
up-next overlay, batch download in the default version, resumable transfers
(cut stream → `Range: bytes=<done>-` → assembled file), offline unlock after the
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

            # ── One tap: "auto" needs no version choice at all ─────────────
            api(base, token, "POST", "/api/videos/download-settings", {"quality": "auto"})
            page.reload()
            page.wait_for_selector("#vd-content .vd-card", timeout=30000)
            page.wait_for_selector('.vd-tab[data-tab="movies"]', timeout=10000)
            page.click('[data-tab="movies"]')
            page.wait_for_selector("#vd-content .vd-card", timeout=20000)
            page.locator("#vd-content .vd-card").first.click()
            page.wait_for_selector("#vd-sheet-title", timeout=10000)
            page.wait_for_timeout(400)
            check("quality stays out of the way in the sheet",
                  page.locator("details.vd-quality-row > summary").count() == 1,
                  str(page.locator("details.vd-quality-row").count()))
            primary = page.locator('.vd-detail-actions [data-action="download"]')
            label = primary.inner_text() if primary.count() else ""
            check("one Download button carries no version suffix",
                  primary.count() == 1 and "·" not in label, label)
            primary.click()
            page.wait_for_timeout(1500)
            _, auto_listing = api(base, token, "GET", "/api/videos/exports")
            movie_job = next((job for job in auto_listing.get("jobs", [])
                              if job["name"] == "Some.Movie.2024.mkv"), None)
            check("auto copies instead of encoding",
                  movie_job is not None and movie_job["quality"] == "original"
                  and movie_job["mode"] in ("remux", "passthrough"),
                  str(movie_job and (movie_job["quality"], movie_job["mode"])))
            page.locator("#vd-sheet > .vd-sheet-backdrop").click(position={"x": 10, "y": 10})
            page.wait_for_selector("#vd-sheet", state="hidden", timeout=10000)

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

            # ── Resumable transfer: a cut stream continues with Range ──────
            # A fake job whose first response closes after 100 of 300 bytes.
            # The client must keep those bytes, ask for `bytes=100-`, and only
            # then write the playable `/__media/<id>` entry.
            fake_id = "e2e-resume-job"
            fake_total = 300
            seen_ranges: list[str] = []
            fake_job = {
                "id": fake_id, "status": "done", "name": "Resume.Clip.2026",
                "path": "/tmp/Resume.Clip.2026.mkv", "quality": "tiny",
                "quality_label": "480p", "size": fake_total, "percent": 100,
                "download_url": f"/api/videos/exports/{fake_id}/download",
            }

            def fake_exports(route, request):
                if request.method == "POST":
                    route.fulfill(status=202, content_type="application/json",
                                  body=json.dumps(fake_job))
                else:
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({
                        "jobs": [fake_job], "defaults": {"quality": "tiny"},
                        "limits": {"queued": 40, "window": 3, "ttl": 86400},
                    }))

            def fake_download(route, request):
                header = request.headers.get("range") or ""
                seen_ranges.append(header)
                start = int(header.split("=")[1].split("-")[0]) if header else 0
                # First attempt is cut short on purpose; the resume gets the rest.
                end = 99 if start == 0 else fake_total - 1
                body = bytes((i * 7) % 251 for i in range(start, end + 1))
                route.fulfill(status=206, body=body, headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {start}-{end}/{fake_total}",
                    "Content-Length": str(len(body)),
                })

            context.route("**/api/videos/exports", fake_exports)
            context.route("**/api/videos/exports/*/download", fake_download)
            try:
                page.evaluate(
                    "async () => { const m = await import('/static/videos/offline.js');"
                    " await m.queueDownload({ path: '/tmp/Resume.Clip.2026.mkv',"
                    " title: 'Resume.Clip.2026' }, 'tiny'); }")
                done = False
                for _ in range(80):
                    done = page.evaluate(
                        "async (id) => { const m = await import('/static/videos/offline.js');"
                        " const row = m.downloadsSnapshot().find((r) => r.id === id);"
                        " return !!row && row.status === 'done' && row.bytesDone === 300; }",
                        fake_id)
                    if done:
                        break
                    page.wait_for_timeout(250)
                check("a cut transfer ends as done", done)
                check("a cut transfer resumes from the stored offset",
                      len(seen_ranges) >= 2 and seen_ranges[0] == ""
                      and seen_ranges[1] == "bytes=100-", str(seen_ranges))
                stored = page.evaluate(
                    "async (id) => { const cache = await caches.open('ethos-media-v1');"
                    " const hit = await cache.match('/__media/' + id);"
                    " return hit ? (await hit.arrayBuffer()).byteLength : -1; }", fake_id)
                check("the assembled file holds every byte", stored == fake_total, str(stored))
                leftovers = page.evaluate(
                    "async (id) => { const cache = await caches.open('ethos-media-v1');"
                    " const keys = await cache.keys();"
                    " return keys.filter((k) => { const u = new URL(k.url);"
                    " return u.pathname === '/__media/' + id && u.searchParams.has('part'); }).length; }",
                    fake_id)
                check("partial entries are cleaned after assembly", leftovers == 0, str(leftovers))
            finally:
                context.unroute("**/api/videos/exports/*/download", fake_download)
                context.unroute("**/api/videos/exports", fake_exports)
                page.evaluate(
                    "async (id) => { const m = await import('/static/videos/offline.js');"
                    " await m.deleteDownload(id); }", fake_id)

            # ── A broken row comes back by itself (no "Resume all" tap) ────
            # Seed the state a NAS restart leaves behind: a stored row that
            # failed because its job vanished, then just open the app.
            movie_path = str(MEDIA / "Some.Movie.2024.mkv")
            page.evaluate("""async (path) => {
              const db = await new Promise((resolve, reject) => {
                const req = indexedDB.open('ethos-videos-db', 2);
                req.onsuccess = () => resolve(req.result);
                req.onerror = () => reject(req.error);
              });
              await new Promise((resolve, reject) => {
                const tx = db.transaction('downloads', 'readwrite');
                tx.objectStore('downloads').put({
                  id: 'e2e-auto-resume', path, title: 'Some.Movie.2024',
                  status: 'failed', bytesDone: 0, bytesTotal: 0, parts: [],
                  error: 'The NAS no longer has this download — queue it again',
                  created_at: Date.now(), item: { path, title: 'Some.Movie.2024' },
                });
                tx.oncomplete = () => resolve();
                tx.onerror = () => reject(tx.error);
              });
              return true;
            }""", movie_path)
            page.reload()
            page.wait_for_selector("#vd-app:not([hidden])", timeout=30000)
            requeued = None
            for _ in range(40):
                _, auto_exports = api(base, token, "GET", "/api/videos/exports")
                requeued = next((job for job in auto_exports.get("jobs", [])
                                 if job["path"] == movie_path and job["id"] != "e2e-auto-resume"),
                                None)
                if requeued:
                    break
                page.wait_for_timeout(250)
            check("a broken row requeues itself when the app opens",
                  requeued is not None, str(requeued and requeued.get("id")))
            replaced = False
            for _ in range(20):
                replaced = page.evaluate(
                    "async (id) => { const m = await import('/static/videos/offline.js');"
                    " const rows = m.downloadsSnapshot();"
                    " return !rows.some((r) => r.id === 'e2e-auto-resume')"
                    "   && rows.some((r) => r.id === id); }", requeued["id"] if requeued else "")
                if replaced:
                    break
                page.wait_for_timeout(250)
            check("the stale row is replaced by the new job", replaced, str(replaced))

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
                "() => { const v = document.getElementById('vd-video');"
                " return v && v.readyState >= 1 && v.duration > 1; }", timeout=20000)
            # Read the position right after playback starts and pause there: a
            # resumed player sits near the saved 6 s, a restarted one near 0 —
            # unlike a fixed wait, this cannot pass just because time moved on.
            page.wait_for_timeout(400)
            resumed = page.evaluate(
                "() => { const v = document.getElementById('vd-video'); v.pause(); return v.currentTime; }")
            check("offline playback resumes where it stopped", resumed >= 5.5,
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
