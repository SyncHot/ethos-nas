#!/usr/bin/env python3
"""EthOS Music PWA E2E (Playwright).

Boots an isolated instance, mocks every /api/music/* call in the browser and
verifies the phone player at /music: the login gate, search, adding a track to
a playlist (which mirrors it to the device), the offline reload and playback
from the IndexedDB copy, plus the installable manifest.

    .venv/bin/python tests/test_music_pwa.py

Playwright is optional — the test skips (exit 0) when it is not installed.
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
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(tempfile.mkdtemp(prefix="ethos-music-pwa-"))
os.environ.update({
    "ETHOS_DATA_DIR": str(DATA),
    "ETHOS_JWT_SECRET": "music-pwa-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402

results: list[tuple[str, bool, str]] = []

# A tiny WAV so the download has a real audio body to store.
WAV = (b"RIFF" + (36).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
       + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + (8000).to_bytes(4, "little")
       + (8000).to_bytes(4, "little") + (1).to_bytes(2, "little") + (8).to_bytes(2, "little")
       + b"data" + (0).to_bytes(4, "little"))


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except urllib.error.HTTPError:
            return True
        except Exception:  # noqa: BLE001 — still starting
            time.sleep(0.2)
    return False


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
    token = _create_token("music-pwa", "music-pwa", "admin")

    playlist = {"id": "pl1", "name": "Test", "tracks": []}
    state = {"liked": [], "playlists": [playlist], "history": [],
             "podcasts": {"subscribed": [], "episodes": {}}, "radio": {"favorites": [], "recents": []}}
    mode = {"offline": False}
    calls: list[tuple[str, str]] = []

    def handler(route):
        request = route.request
        parsed = urllib.parse.urlparse(request.url)
        path = parsed.path
        method = request.method
        calls.append((method, path))
        if mode["offline"]:
            route.abort("internetdisconnected")
            return

        def reply(payload, status=200):
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps(payload))

        if path == "/api/music/state" and method == "GET":
            reply(state)
            return
        if path == "/api/music/radio/state":
            reply({"favorites": [], "recents": ["r1"]})
            return
        if path == "/api/music/radio/stations":
            reply({"categories": ["Komercyjne"], "stations": [
                {"id": "r1", "name": "RMF", "category": "Komercyjne", "stream_url": "https://x/rmf.mp3",
                 "city": "Krakow", "logo": "", "tags": "pop", "hls": False},
            ], "total": 1})
            return
        if path == "/api/sharing/incoming":
            reply({"grants": []})
            return
        if path == "/api/music/search/suggest":
            reply({"items": []})
            return
        if path == "/api/music/search":
            offset = int((urllib.parse.parse_qs(parsed.query).get("offset") or ["0"])[0])
            if offset == 0:
                reply({
                    "query": "test",
                    "tracks": {"items": [
                        {"id": "vid12345678", "title": "Testowy utwór",
                         "artist": "Test Artysta", "duration": 180, "thumbnail": ""},
                        {"id": "vid87654321", "title": "Polubiony utwór",
                         "artist": "Test Artysta", "duration": 200, "thumbnail": ""},
                    ], "total": 3, "has_more": True, "offset": 0},
                    "podcasts": {"items": []}, "radio": {"items": []},
                    "library": {"items": []}, "playlists": {"items": []},
                })
            else:
                reply({
                    "query": "test",
                    "tracks": {"items": [
                        {"id": "vid33333333", "title": "Trzeci wynik",
                         "artist": "Test Artysta", "duration": 150, "thumbnail": ""},
                    ], "total": 3, "has_more": False, "offset": 2},
                    "podcasts": {"items": []}, "radio": {"items": []},
                    "library": {"items": []}, "playlists": {"items": []},
                })
            return
        if path == "/api/music/stream":
            route.fulfill(status=200, content_type="audio/wav", body=WAV)
            return
        if path == "/api/music/history" and method == "POST":
            route.fulfill(status=204, body="")
            return
        if path == "/api/music/playlists/pl1/tracks" and method == "PUT":
            body = json.loads(request.post_data or "{}")
            if body.get("track"):
                playlist["tracks"].append(body["track"])
            reply({"ok": True})
            return
        if path.startswith("/api/music/"):
            reply({"ok": True})
            return
        route.continue_()

    try:
        if not wait_for(f"{base}/api/system/status"):
            print("server did not start")
            return 1

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(viewport={"width": 390, "height": 844})
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))

            # 1. public page, login gate without a token
            response = page.goto(f"{base}/music", wait_until="domcontentloaded")
            check("/music is served without a token", response is not None and response.status == 200,
                  str(response.status if response else "no response"))
            page.wait_for_selector('[data-testid="music-login"]', state="visible", timeout=8000)
            check("login card is shown without a token",
                  page.is_visible('[data-testid="music-login"]'))
            check("manifest is linked", page.get_attribute('link[rel="manifest"]', "href")
                  == "/manifest-music.webmanifest")

            # 2. signed-in session (JWT injected like an existing desktop session)
            page.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page.route("**/api/music/**", handler)
            page.goto(f"{base}/music", wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="music-app"]', state="visible", timeout=10000)
            page.wait_for_selector(".mu-h1", timeout=8000)
            check("home renders for a signed-in account", page.is_visible(".mu-h1"))

            # service worker takes over (needed for the offline reload)
            page.evaluate("navigator.serviceWorker.ready.then(() => true)")
            page.wait_for_timeout(600)

            # 3. search → one mocked result
            page.click('[data-testid="music-nav-search"]')
            page.fill("#mu-search-input", "test")
            page.press("#mu-search-input", "Enter")
            page.wait_for_selector(".mu-row-title", timeout=8000)
            check("search renders a track", "Testowy utwór" in page.inner_text("#mu-results"))

            # 3b. tapping a result plays the whole result list, not one track
            page.wait_for_timeout(600)   # let a debounced re-search finish re-rendering
            page.locator('.mu-row[data-key]').first.click()
            page.wait_for_selector('[data-testid="music-player"]', state="visible", timeout=5000)
            page.click("#mu-queue-btn")
            check("tapping a search result queues the whole list",
                  page.locator("#mu-queue-list .mu-row").count() == 2,
                  str(page.locator("#mu-queue-list .mu-row").count()))
            page.click("#mu-queue-close")
            page.click("#mu-player-close")

            # 4. add to playlist → the playlist mirrors to the device
            page.click(".mu-row-more")
            page.get_by_role("button", name="Dodaj do playlisty").click()
            page.get_by_role("button", name="Test", exact=True).click()
            page.wait_for_timeout(300)
            check("adding a track to a playlist is sent to the NAS",
                  any(path == "/api/music/playlists/pl1/tracks" for _method, path in calls))

            # 4b. the heart is the normal "Polubione" playlist → also downloaded
            page.locator(".mu-row").nth(1).locator("[data-act='track-menu']").click()
            page.get_by_role("button", name="Dodaj do playlisty").click()
            page.get_by_role("button", name="Polubione", exact=True).click()
            page.wait_for_timeout(300)
            check("liking writes to the Polubione playlist",
                  any(path == "/api/music/playlists/liked/tracks" for _method, path in calls),
                  str(calls[-4:]))
            page.click('[data-testid="music-nav-library"]')
            check("the library shows Polubione as a playlist",
                  "Polubione" in page.inner_text("#mu-content"))
            page.click('[data-testid="music-nav-search"]')

            rows = []
            for _ in range(40):
                rows = page.evaluate("""() => new Promise((resolve, reject) => {
                    const req = indexedDB.open('ethos-music-db', 1);
                    req.onerror = () => reject(req.error);
                    req.onsuccess = () => {
                        const db = req.result;
                        const tx = db.transaction('rows', 'readonly');
                        const all = tx.objectStore('rows').getAll();
                        all.onsuccess = () => resolve(all.result.map((row) => ({
                            key: row.key, status: row.status, title: row.track && row.track.title,
                        })));
                        all.onerror = () => reject(all.error);
                    };
                })""")
                if any(row["status"] == "done" and row["title"] == "Testowy utwór" for row in rows):
                    break
                page.wait_for_timeout(250)
            check("the playlist track and the liked track are downloaded",
                  {"Testowy utwór", "Polubiony utwór"} <= {
                      row["title"] for row in rows if row["status"] == "done"},
                  str(rows))

            # 4c. playlist cards + header report what is already on the device
            page.click('[data-testid="music-nav-library"]')
            page.wait_for_timeout(500)
            badges = page.locator(".mu-card-badge").all_inner_texts()
            check("playlist cards show a downloaded/total badge",
                  any("/" in text for text in badges), str(badges))
            check("a fully downloaded playlist is marked as such",
                  any("1/1" in text for text in badges), str(badges))
            page.click('[data-act="open-playlist"][data-id="pl1"]')
            page.wait_for_selector("[data-playlist-download]", timeout=8000)
            check("the playlist header shows the offline count",
                  "1 z 1" in page.inner_text("[data-playlist-download]"),
                  page.inner_text("[data-playlist-download]"))
            page.click('[data-act="back"]')
            page.wait_for_selector(".mu-h1", timeout=5000)
            check("back returns to the library", "Biblioteka" in page.inner_text("#mu-content"))

            # 4d. progress panel is actually styled on the phone page
            styled = page.evaluate("""async () => {
                const m = await import('/static/core/progress.js');
                const task = m.startTask({ appId: 'music', title: 'probe' });
                const panel = document.getElementById('taskProgressPanel');
                const pos = panel ? getComputedStyle(panel).position : 'missing';
                const width = panel ? panel.getBoundingClientRect().width : 0;
                task.finish('done', { percent: 100 });
                m.clearFinishedTasks(null, { globalOnly: true });
                const linked = [...document.styleSheets].some((sheet) => (sheet.href || '').includes('progress.css'));
                return { pos, width, linked };
            }""")
            check("the download progress panel is styled on /music",
                  styled["pos"] == "fixed" and styled["width"] > 100 and styled["linked"], str(styled))

            # 4e. radio recents are rendered
            page.click('[data-testid="music-nav-radio"]')
            page.wait_for_selector("#mu-radio-recent .mu-row", timeout=8000)
            check("recently played stations are listed",
                  "Ostatnio słuchane" in page.inner_text("#mu-radio-recent"))

            # 4f. a manual (non-playlist) download is pinned across a re-sync
            pinned = page.evaluate("""async () => {
                const offline = await import('/static/music/offline.js');
                const track = { id: 'vid00000000', title: 'Przypiety utwor', artist: 'Test', duration: 60, thumbnail: '' };
                offline.downloadTrack(track);
                for (let i = 0; i < 60; i++) {
                    await new Promise((resolve) => setTimeout(resolve, 100));
                    if (offline.hasLocal(track)) break;
                }
                const res = await fetch('/api/music/state', {
                    headers: { Authorization: 'Bearer ' + (localStorage.getItem('token') || '') },
                });
                const state = await res.json();
                offline.syncPlaylists(state.playlists);
                await new Promise((resolve) => setTimeout(resolve, 500));
                return {
                    has: offline.hasLocal(track),
                    rows: offline.downloadsSnapshot().map((row) => [row.track && row.track.title, row.status, !!row.pinned]),
                };
            }""")
            check("a manual download outside a playlist survives a re-sync",
                  pinned["has"] and any(row[0] == 'Przypiety utwor' and row[2] for row in pinned["rows"]),
                  str(pinned))

            cancelled = page.evaluate("""async () => {
                const offline = await import('/static/music/offline.js');
                const track = { id: 'vid11111111', title: 'Anulowany utwor', artist: 'Test', duration: 60, thumbnail: '' };
                offline.downloadTrack(track);
                await offline.deleteDownload(track);
                await new Promise((resolve) => setTimeout(resolve, 900));
                return {
                    has: offline.hasLocal(track),
                    rows: offline.downloadsSnapshot().map((row) => row.track && row.track.title),
                };
            }""")
            check("deleting a download removes it and it stays gone",
                  not cancelled["has"] and 'Anulowany utwor' not in cancelled["rows"], str(cancelled))

            # 4d. downloads are parallel by default and the setting sticks
            page.click('[data-testid="music-nav-settings"]')
            page.wait_for_selector("#mu-parallel", timeout=8000)
            check("download parallelism defaults to 4", page.input_value("#mu-parallel") == "4")
            page.select_option("#mu-parallel", "2")
            page.wait_for_timeout(150)
            check("the parallelism choice is kept on the device",
                  page.evaluate("localStorage.getItem('ethos.music.parallel')") == "2")

            # 5. offline reload: shell from the SW, library from the IDB snapshot
            mode["offline"] = True
            context.set_offline(True)
            page.goto(f"{base}/music?view=downloads", wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="music-app"]', state="visible", timeout=15000)
            page.wait_for_selector(".mu-row-title", timeout=8000)
            check("the downloaded track is listed offline",
                  "Testowy utwór" in page.inner_text("#mu-content"))
            check("the offline pill is shown",
                  page.evaluate("!document.getElementById('mu-net').hidden"))

            # 6. playback prefers the stored copy (blob, no network)
            stream_before = sum(1 for _m, path in calls if path == "/api/music/stream")
            page.locator(".mu-dl-list .mu-row", has_text="Testowy utwór").first.click()
            page.wait_for_selector('[data-testid="music-player"]', state="visible", timeout=8000)
            page.wait_for_timeout(1200)
            check("tapping a download opens the player",
                  page.is_visible('[data-testid="music-player"]'))
            check("the player shows the downloaded track",
                  any(title in page.inner_text('[data-testid="music-player"]')
                      for title in ("Testowy utwór", "Polubiony utwór")),
                  page.inner_text('[data-testid="music-player"]')[:120])
            stream_after = sum(1 for _m, path in calls if path == "/api/music/stream")
            check("offline playback does not hit the stream proxy", stream_after == stream_before,
                  f"{stream_before} -> {stream_after}")

            blobs = page.evaluate("""() => new Promise((resolve, reject) => {
                const req = indexedDB.open('ethos-music-db', 1);
                req.onerror = () => reject(req.error);
                req.onsuccess = () => {
                    const db = req.result;
                    const tx = db.transaction('blobs', 'readonly');
                    const all = tx.objectStore('blobs').getAll();
                    all.onsuccess = () => resolve(all.result.map((row) => ({
                        key: row.key, size: row.audio ? row.audio.size : 0,
                    })));
                    all.onerror = () => reject(all.error);
                };
            })""")
            check("the audio blob is stored on the device",
                  any(row["size"] == len(WAV) for row in blobs), str(blobs))

            check("no uncaught page errors", not errors, "; ".join(errors))
            browser.close()

        return 1 if any(not ok for _name, ok, _detail in results) else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(DATA, ignore_errors=True)
        failed = [r for r in results if not r[1]]
        print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")


if __name__ == "__main__":
    sys.exit(main())
