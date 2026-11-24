#!/usr/bin/env python3
"""EthOS Books PWA E2E (Playwright).

Boots an isolated instance, mocks the Books package proxy (`/api/packages/
ethos-books/proxy/*`) and the core file API, and verifies the phone app at
`/books`: the public login gate, the library, downloading an audiobook into
Cache Storage, offline playback from `/__books/…`, the progress write back to
the package and the installable manifest.

    .venv/bin/python tests/test_books_pwa.py

Playwright is optional — the test skips (exit 0) when it is not installed.
"""

from __future__ import annotations

import base64
import io
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
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(tempfile.mkdtemp(prefix="ethos-books-pwa-"))
os.environ.update({
    "ETHOS_DATA_DIR": str(DATA),
    "ETHOS_JWT_SECRET": "books-pwa-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402

results: list[tuple[str, bool, str]] = []

# A short audible WAV (0.1 s of silence, 8 kHz mono 8-bit) so the download has a
# body the browser can actually decode — a zero-length data chunk would make
# <audio> error out and fall back to the network, which is what the offline
# assertion is about.
_SAMPLES = 800
WAV = (b"RIFF" + (36 + _SAMPLES).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little")
       + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + (8000).to_bytes(4, "little")
       + (8000).to_bytes(4, "little") + (1).to_bytes(2, "little") + (8).to_bytes(2, "little")
       + b"data" + _SAMPLES.to_bytes(4, "little") + bytes([128]) * _SAMPLES)

# A 1×1 PNG so a cover download actually returns image bytes.
COVER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


AUDIOBOOK = {
    "id": "bk_audio00001",
    "path": "/mnt/books/Test Book",
    "format": "m4b",
    "kind": "audiobook",
    "title": "Testowy audiobook",
    "author": "Test Autor",
    "series": "",
    "series_index": 0,
    "language": "pl",
    "size": 2 * len(WAV),
    "duration_s": 8.0,
    "modified": 0,
    "cover": True,
    "cover_ext": "webp",
    "manual": False,
    "added_at": "2026-11-20T10:00:00+00:00",
    "updated_at": "2026-11-20T10:00:00+00:00",
    "tracks": [
        {"path": "/mnt/books/Test Book/01.mp3", "title": "Rozdział 1", "size": len(WAV),
         "duration": 4.0, "modified": 0},
        {"path": "/mnt/books/Test Book/02.mp3", "title": "Rozdział 2", "size": len(WAV),
         "duration": 4.0, "modified": 0},
    ],
}

EPUB = {
    "id": "bk_epub000001",
    "path": "/mnt/books/Test.epub",
    "format": "epub",
    "kind": "book",
    "title": "Testowa książka",
    "author": "Test Autor",
    "series": "",
    "series_index": 0,
    "language": "pl",
    "size": 2048,
    "modified": 0,
    "cover": True,
    "cover_ext": "webp",
    "manual": False,
    "added_at": "2026-11-20T10:00:00+00:00",
    "updated_at": "2026-11-20T10:00:00+00:00",
}

PDF_BOOK = {
    "id": "bk_pdf0000001",
    "path": "/mnt/books/Test.pdf",
    "format": "pdf",
    "kind": "book",
    "title": "Testowy PDF",
    "author": "Test Autor",
    "series": "",
    "series_index": 0,
    "language": "pl",
    "size": 1024,
    "modified": 0,
    "cover": False,
    "cover_ext": "webp",
    "manual": False,
    "added_at": "2026-11-20T10:00:00+00:00",
    "updated_at": "2026-11-20T10:00:00+00:00",
}


def make_epub() -> bytes:
    """A minimal valid EPUB 3 (one chapter) for the real epub.js reader."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
            compress_type=zipfile.ZIP_DEFLATED,
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">urn:uuid:bk-test</dc:identifier>'
            '<dc:title>Test</dc:title><dc:language>pl</dc:language>'
            '<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>'
            '<manifest>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="ch1"/></spine></package>',
            compress_type=zipfile.ZIP_DEFLATED,
        )
        archive.writestr(
            "OEBPS/nav.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
            '<head><title>Spis</title></head><body><nav epub:type="toc"><ol>'
            '<li><a href="ch1.xhtml">Rozdział 1</a></li></ol></nav></body></html>',
            compress_type=zipfile.ZIP_DEFLATED,
        )
        archive.writestr(
            "OEBPS/ch1.xhtml",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>R1</title></head>'
            '<body><h1>Rozdział testowy</h1><p>Treść książki testowej.</p></body></html>',
            compress_type=zipfile.ZIP_DEFLATED,
        )
    return buffer.getvalue()


def make_pdf(text: str = "Hello PDF") -> bytes:
    """A minimal one-page PDF for the real pdf.js renderer."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    ]
    stream = b"BT /F1 24 Tf 40 300 Td (" + text.encode() + b") Tj ET"
    objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                   + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\n"
            b"startxref\n" + str(xref).encode() + b"\n%%EOF\n")
    return bytes(out)


EPUB_BYTES = make_epub()
PDF_BYTES = make_pdf()
EPUB["size"] = len(EPUB_BYTES)
PDF_BOOK["size"] = len(PDF_BYTES)

LIBRARY = {
    "folders": [{"path": "/mnt/books", "added_at": "2026-11-20T10:00:00+00:00",
                 "last_scan": "2026-11-21T10:00:00+00:00", "count": 2}],
    "books": [AUDIOBOOK, EPUB, PDF_BOOK],
    "shared_books": [],
    "progress": {
        "bk_audio00001": {"percent": 0.42, "locator": "0", "position_s": 12,
                          "finished": False, "updated_at": "2026-11-21T10:00:00+00:00"},
    },
    "settings": {
        "theme": "system", "font_family": "serif", "font_size": 100, "margin": 2,
        "line_height": 1.6, "flow": "paginated", "skip_seconds": 30, "speed": 1.0,
        "extensions": ["epub", "pdf", "m4b", "mp3"],
    },
    "stats": {"books": 2, "audiobooks": 1, "finished": 0,
              "bytes": 2 * len(WAV) + len(EPUB_BYTES) + len(PDF_BYTES)},
}


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
    token = _create_token("books-pwa", "books-pwa", "admin")

    mode = {"offline": False, "failRaw": 1}
    calls: list[tuple[str, str]] = []
    progress: list[dict] = []

    def handler(route):
        request = route.request
        path = urllib.parse.urlparse(request.url).path
        method = request.method
        calls.append((method, path))
        if mode["offline"]:
            route.abort("internetdisconnected")
            return

        def reply(payload, status=200):
            route.fulfill(status=status, content_type="application/json",
                          body=json.dumps(payload))

        proxy = "/api/packages/ethos-books/proxy"
        if path == f"{proxy}/library" and method == "GET":
            reply(LIBRARY)
            return
        if path == f"{proxy}/settings" and method == "GET":
            reply(LIBRARY["settings"])
            return
        if path == f"{proxy}/settings" and method == "PUT":
            body = json.loads(request.post_data or "{}")
            LIBRARY["settings"].update({k: v for k, v in body.items() if k in LIBRARY["settings"]})
            reply(LIBRARY["settings"])
            return
        if path.startswith(f"{proxy}/books/") and path.endswith("/progress") and method == "PUT":
            body = json.loads(request.post_data or "{}")
            progress.append(body)
            reply({"status": "ok", "progress": body})
            return
        if path.startswith(f"{proxy}/covers/"):
            route.fulfill(status=200, content_type="image/png", body=COVER_PNG)
            return
        if path == "/api/files/raw":
            source = (urllib.parse.parse_qs(urllib.parse.urlparse(request.url).query)
                      .get("path", [""])[0])
            if source.endswith(".mp3") and mode["failRaw"] > 0:
                # A permanent 4xx failure: the UI must offer "Ponów". (A 5xx or
                # a network error is a NAS restart and retries automatically.)
                mode["failRaw"] -= 1
                route.fulfill(status=404, content_type="application/json",
                              body=json.dumps({"detail": "simulated missing file"}))
                return
            if source.endswith(".epub"):
                route.fulfill(status=200, content_type="application/epub+zip", body=EPUB_BYTES)
            elif source.endswith(".pdf"):
                route.fulfill(status=200, content_type="application/pdf", body=PDF_BYTES)
            else:
                route.fulfill(status=200, content_type="audio/wav", body=WAV)
            return
        if path.startswith(proxy):
            reply({"ok": True})
            return
        route.continue_()

    try:
        if not wait_for(f"{base}/api/system/status"):
            print("server did not start")
            return 1

        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
            context = browser.new_context(viewport={"width": 390, "height": 844})
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))

            # 1. public page + login gate
            response = page.goto(f"{base}/books", wait_until="domcontentloaded")
            check("/books is served without a token",
                  response is not None and response.status == 200,
                  str(response.status if response else "no response"))
            page.wait_for_selector('[data-testid="books-login"]', state="visible", timeout=8000)
            check("login card is shown without a token",
                  page.is_visible('[data-testid="books-login"]'))
            check("manifest is linked",
                  page.get_attribute('link[rel="manifest"]', "href")
                  == "/manifest-books.webmanifest")

            # 2. signed-in session (JWT injected like an existing desktop session)
            page.add_init_script(
                f"if (!localStorage.getItem('__booksNoToken')) "
                f"localStorage.setItem('token', {json.dumps(token)});")
            page.add_init_script("""
                window.__psCalls = 0;
                try {
                    const session = navigator.mediaSession;
                    if (session && session.setPositionState) {
                        const original = session.setPositionState.bind(session);
                        session.setPositionState = (value) => {
                            window.__psCalls += 1;
                            try { original(value); } catch (_) { /* headless */ }
                        };
                    }
                } catch (_) { /* no Media Session in this build */ }
            """)
            page.route("**/api/packages/ethos-books/**", handler)
            page.route("**/api/files/raw**", handler)
            page.goto(f"{base}/books", wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="books-app"]', state="visible", timeout=10000)
            page.wait_for_selector('[data-testid="books-hero"]', timeout=8000)
            check("library renders for a signed-in account",
                  "Testowy audiobook" in page.inner_text("#bk-content"))
            check("the continue row is built from shared progress",
                  page.locator('[data-testid="books-item"]').count() >= 1)
            check("the EPUB title is listed too",
                  "Testowa książka" in page.inner_text("#bk-content"))

            # 1b. covers are painted online and stored for offline use
            page.wait_for_selector('[data-cover] img', timeout=8000)
            check("covers render from the package proxy",
                  page.locator('[data-cover] img').count() >= 2,
                  str(page.locator('[data-cover] img').count()))
            deadline = time.time() + 10
            runs = 0
            while time.time() < deadline:
                runs = page.evaluate(
                    "async () => (await import('/static/books/offline.js')).autoCacheRuns()")
                if runs:
                    break
                page.wait_for_timeout(200)
            check("the app caches covers automatically", runs >= 1, str(runs))
            deadline = time.time() + 10
            cached_covers = 0
            while time.time() < deadline:
                cached_covers = page.evaluate("""async () => {
                    const db = await new Promise((res, rej) => {
                        const request = indexedDB.open('ethos-books-db');
                        request.onsuccess = () => res(request.result);
                        request.onerror = () => rej(request.error);
                    });
                    return await new Promise((res, rej) => {
                        const request = db.transaction('blobs', 'readonly')
                            .objectStore('blobs').getAll();
                        request.onsuccess = () => res((request.result || [])
                            .filter((row) => String(row.key).startsWith('cover:')).length);
                        request.onerror = () => rej(request.error);
                    });
                }""")
                if cached_covers >= 2:
                    break
                page.wait_for_timeout(200)
            check("covers are stored in IndexedDB", cached_covers >= 2, str(cached_covers))

            # 1c. Settings can prefetch every cover before a flight
            page.goto(f"{base}/books?view=settings", wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="books-cache-covers"]', timeout=10000)
            page.click('[data-testid="books-cache-covers"]')
            page.wait_for_function(
                "() => (document.getElementById('bk-cover-stat')?.textContent || '').trim() === '2 z 2'",
                timeout=15000)
            check("Settings caches every cover offline", True)
            page.goto(f"{base}/books", wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="books-hero"]', timeout=8000)

            # service worker takes over (needed for the offline reload)
            page.evaluate("navigator.serviceWorker.ready.then((r) => !!r.active)")
            page.wait_for_timeout(500)

            # 2b. the real readers (vendored epub.js / pdf.js) open a book
            page.locator(".bk-card", has_text="Testowa książka").click()
            page.wait_for_selector('[data-act="open"]', timeout=6000)
            page.click('[data-act="open"]')
            page.wait_for_selector('[data-testid="books-reader"]', state="visible", timeout=8000)
            page.wait_for_selector(".bk-epub-host iframe", timeout=15000)
            epub_text = page.evaluate("""() => {
                const frame = document.querySelector('.bk-epub-host iframe');
                return frame && frame.contentDocument ? frame.contentDocument.body.innerText : '';
            }""")
            check("the EPUB reader renders the chapter", "Rozdział testowy" in epub_text, epub_text[:80])
            page.go_back()
            page.wait_for_timeout(400)
            check("Android Back closes the reader instead of the app",
                  page.is_hidden('[data-testid="books-reader"]')
                  and page.is_visible('[data-testid="books-app"]'))

            page.locator(".bk-card", has_text="Testowy PDF").click()
            page.wait_for_selector('[data-act="open"]', timeout=6000)
            page.click('[data-act="open"]')
            page.wait_for_selector('canvas[data-testid="books-pdf-canvas"]', timeout=15000)
            check("the PDF reader renders the first page",
                  page.locator('canvas[data-testid="books-pdf-canvas"]').count() == 1)
            page.click("#bk-reader-back")
            page.wait_for_timeout(300)

            # 3. download the audiobook from its detail sheet
            page.locator('[data-testid="books-item"]', has_text="Testowy audiobook").first.click()
            page.wait_for_selector('[data-act="download"]', timeout=6000)
            check("the detail sheet offers a device download",
                  page.is_visible('[data-act="download"]'))
            page.click('[data-act="download"]')
            page.wait_for_selector('[data-testid="books-download-row"]', timeout=8000)

            # The mocked first transfer fails once → the row must offer a retry.
            page.wait_for_selector('[data-act="retry-download"]', timeout=15000)
            check("a failed download offers Ponów", page.is_visible('[data-act="retry-download"]'))
            page.click('[data-act="retry-download"]')

            rows = []
            for _ in range(60):
                rows = page.evaluate("""() => new Promise((resolve, reject) => {
                    const req = indexedDB.open('ethos-books-db', 1);
                    req.onerror = () => reject(req.error);
                    req.onsuccess = () => {
                        const db = req.result;
                        const tx = db.transaction('rows', 'readonly');
                        const all = tx.objectStore('rows').getAll();
                        all.onsuccess = () => resolve(all.result.map((row) => ({
                            key: row.key, status: row.status, bytes: row.bytes,
                        })));
                        all.onerror = () => reject(all.error);
                    };
                })""")
                if rows and all(row["status"] == "done" for row in rows):
                    break
                page.wait_for_timeout(250)
            check("every track of the audiobook is downloaded",
                  len(rows) == 2 and all(row["status"] == "done" for row in rows), str(rows))

            cache_keys = page.evaluate("""async () => {
                const cache = await caches.open('ethos-books-v1');
                const keys = await cache.keys();
                return keys.map((key) => new URL(key.url).pathname);
            }""")
            check("the tracks are stored in the books cache",
                  "/__books/bk_audio00001/t0" in cache_keys
                  and "/__books/bk_audio00001/t1" in cache_keys, str(cache_keys))

            # 4. play it once online: the position is written back to the package
            page.click('[data-act="open-download"]')
            page.wait_for_selector('[data-act="open"]', timeout=6000)
            page.click('[data-act="open"]')
            page.wait_for_selector('[data-testid="books-player"]', state="visible", timeout=8000)
            check("the player opens for the downloaded audiobook",
                  page.is_visible('[data-testid="books-player"]'))
            check("the player shows the book title",
                  "Testowy audiobook" in page.inner_text('[data-testid="books-player"]'))
            page.wait_for_timeout(1500)
            check("the lock-screen position is updated while playing",
                  page.evaluate("window.__psCalls > 0"),
                  str(page.evaluate("window.__psCalls")))
            page.evaluate("window.dispatchEvent(new Event('pagehide'))")
            for _ in range(30):
                if progress:
                    break
                page.wait_for_timeout(250)
            check("playback progress is PUT to the package",
                  bool(progress), str(progress[:1]))
            check("progress keeps the package locator format (track index)",
                  bool(progress) and str(progress[-1].get("locator")) in ("0", "1"),
                  str(progress[-1:] if progress else []))

            # 5. offline reload: shell + snapshot from the device
            mode["offline"] = True
            context.set_offline(True)
            page.goto(f"{base}/books?view=downloads", wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="books-app"]', state="visible", timeout=15000)
            page.wait_for_selector('[data-testid="books-download-row"]', timeout=8000)
            check("the download is listed offline",
                  "Testowy audiobook" in page.inner_text("#bk-content"))
            check("the offline pill is shown",
                  page.evaluate("!document.getElementById('bk-net').hidden"))

            # 5b. covers already on the device still paint without the network
            page.goto(f"{base}/books", wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="books-app"]', state="visible", timeout=15000)
            page.wait_for_selector('[data-cover] img', timeout=8000)
            check("cached covers render while offline",
                  page.locator('[data-cover] img').count() >= 2,
                  str(page.locator('[data-cover] img').count()))
            page.goto(f"{base}/books?view=downloads", wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="books-download-row"]', timeout=8000)

            # 6. playback comes from /__books, not from the NAS
            file_calls_before = sum(1 for _m, path in calls if path == "/api/files/raw")
            page.click('[data-act="open-download"]')
            page.wait_for_selector('[data-act="open"]', timeout=6000)
            page.click('[data-act="open"]')
            page.wait_for_selector('[data-testid="books-player"]', state="visible", timeout=8000)
            page.wait_for_timeout(1500)
            file_calls_after = sum(1 for _m, path in calls if path == "/api/files/raw")
            check("offline playback does not stream from the NAS",
                  file_calls_after == file_calls_before,
                  f"{file_calls_before} -> {file_calls_after}")
            check("the player is usable while offline",
                  page.is_visible("#bk-play") and page.is_visible("#bk-seek"))

            # 7. an evicted cache entry is reported, not silently used
            gone = page.evaluate("""async () => {
                const cache = await caches.open('ethos-books-v1');
                await cache.delete(new Request('/__books/bk_audio00001/t1'));
                const module = await import('/static/books/offline.js');
                const missing = await module.reconcileDownloads();
                return { missing, row: module.rowFor('bk_audio00001', 1) };
            }""")
            check("an evicted file is marked missing",
                  gone.get("missing") == 1 and gone.get("row", {}).get("status") == "missing",
                  str(gone))
            check("the download badge disappears for a missing file",
                  bool(page.evaluate("""async () => {
                      const module = await import('/static/books/offline.js');
                      return !module.isDownloaded({ id: 'bk_audio00001', kind: 'audiobook',
                          tracks: [{ path: 'a' }, { path: 'b' }] });
                  }""")))

            # 8. expired token + no network: the downloaded library stays usable
            page.evaluate("localStorage.setItem('__booksNoToken', '1'); localStorage.removeItem('token')")
            page.goto(f"{base}/books?view=downloads", wait_until="domcontentloaded")
            page.wait_for_selector('[data-testid="books-app"]', state="visible", timeout=15000)
            page.wait_for_selector('[data-testid="books-download-row"]', timeout=8000)
            check("downloaded titles open without a valid token while offline",
                  page.is_hidden('[data-testid="books-login"]')
                  and "Testowy audiobook" in page.inner_text("#bk-content"))
            check("the offline-only pill explains the mode",
                  page.is_visible("#bk-net"))

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
