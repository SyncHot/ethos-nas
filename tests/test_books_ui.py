#!/usr/bin/env python3
"""Books package UI E2E (Playwright).

Seeds the local `ethos-packages/ethos-books` package as an installed native app,
starts its venv service on a free loopback port, and drives the real window
against the real core file API: scan a fixture folder, open an EPUB, check the
player/reader surfaces, and confirm the library is per-account.

Requires the sibling repo `~/apps/compose/ethos-packages`; skips otherwise.

Run: .venv/bin/python tests/test_books_ui.py
"""
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
import zipfile
from pathlib import Path

DATA = tempfile.mkdtemp(prefix="ethos-books-ui-data-")
os.environ.update({
    "ETHOS_DATA_DIR": DATA,
    "ETHOS_JWT_SECRET": "books-ui-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path.home() / "apps/compose/ethos-packages/ethos-books"
sys.path.insert(0, str(ROOT))

from backend.routers.auth import _create_token  # noqa: E402
from backend.services import packages_store  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_ready(base: str) -> bool:
    end = time.time() + 45
    while time.time() < end:
        try:
            urllib.request.urlopen(base + "/api/system/status", timeout=2)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def api(base: str, token: str, path: str, method: str = "GET", body=None, timeout=15):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def make_epub(path: Path, title: str, author: str) -> None:
    """A minimal, valid EPUB 2 that epub.js can open."""
    chapter = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Chapter</title></head>'
        f'<body><h1>{title}</h1><p>First paragraph of the fixture book.</p>'
        '<p>Second paragraph so the reader has something to paginate.</p></body></html>'
    )
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="id">urn:uuid:books-ui-fixture</dc:identifier>
  </metadata>
  <manifest>
    <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx"><itemref idref="chapter1"/></spine>
</package>"""
    ncx = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:books-ui-fixture"/></head>
  <docTitle><text>Fixture</text></docTitle>
  <navMap><navPoint id="n1" playOrder="1"><navLabel><text>Chapter</text></navLabel>
  <content src="chapter1.xhtml"/></navPoint></navMap>
</ncx>"""
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        zf.writestr("OEBPS/chapter1.xhtml", chapter)


def make_pdf(path: Path) -> None:
    from PIL import Image

    Image.new("RGB", (240, 340), "white").save(path, "PDF")


def main() -> int:
    if not (PACKAGE / "frontend/app.js").is_file():
        print(f"package repo not found at {PACKAGE} — skipping")
        return 0

    books = Path(tempfile.mkdtemp(prefix=".ethos-books-ui-", dir=str(Path.home())))
    make_epub(books / "Dune.epub", "Dune", "Frank Herbert")
    make_pdf(books / "Manual.pdf")
    audio_dir = books / "Author" / "Series" / "Book One"
    audio_dir.mkdir(parents=True)
    (audio_dir / "01 - Start.mp3").write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    (audio_dir / "02 - End.mp3").write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")

    service_port = free_port()
    installed = Path(DATA) / "packages" / "apps" / "ethos-books"
    for rel in ("server/main.py", "frontend/app.js", "frontend/vendor/jszip.min.js",
                "frontend/vendor/epub.min.js", "frontend/vendor/pdf.min.js",
                "frontend/vendor/pdf.worker.min.js", "icon.svg"):
        target = installed / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PACKAGE / rel, target)
    packages_store.add_package({
        "id": "ethos-books",
        "name": "Books",
        "version": "1.0.0",
        "icon": "📚",
        "description": "Books UI test",
        "trust": "pinned",
        "runtime": "venv",
        "user_access": True,
        "ui": {"mode": "native", "entry": "frontend/app.js", "port": service_port},
        "python": {"entry": "server/main.py", "port": service_port, "requirements": [],
                   "requires_python": "3.14", "wheels": []},
        "files": [
            {"path": "server/main.py", "url": "local", "sha256": "0" * 64, "mode": 420},
            {"path": "frontend/app.js", "url": "local", "sha256": "0" * 64, "mode": 420},
            {"path": "frontend/vendor/jszip.min.js", "url": "local", "sha256": "0" * 64, "mode": 420},
            {"path": "frontend/vendor/epub.min.js", "url": "local", "sha256": "0" * 64, "mode": 420},
            {"path": "frontend/vendor/pdf.min.js", "url": "local", "sha256": "0" * 64, "mode": 420},
            {"path": "frontend/vendor/pdf.worker.min.js", "url": "local", "sha256": "0" * 64, "mode": 420},
            {"path": "icon.svg", "url": "local", "sha256": "0" * 64, "mode": 420},
        ],
    })

    service_env = {
        **os.environ,
        "ETHOS_PACKAGE_PORT": str(service_port),
        "ETHOS_PACKAGE_DATA_DIR": str(Path(DATA) / "packages" / "data" / "ethos-books"),
    }
    service = subprocess.Popen(
        [sys.executable, str(installed / "server" / "main.py")],
        cwd=str(installed), env=service_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    token = _create_token("admin", "admin", "admin")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        if not wait_ready(base):
            print((proc.stderr.read() if proc.stderr else "")[-2000:])
            return 1
        # Register the scan folder through the authenticated proxy before the UI opens.
        api(base, token, "/api/packages/ethos-books/proxy/folders", "PUT",
            {"folders": [{"path": str(books)}]})

        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            context.add_init_script(f"localStorage.setItem('token', {json.dumps(token)});")
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base)
            page.wait_for_timeout(2500)
            page.evaluate("window.openApp('pkg-ethos-books')")
            page.wait_for_selector(".bk-root", timeout=15000)
            check("package window renders", page.locator(".bk-root").count() == 1)
            check("rail has all sections", page.locator(".bk-nav-item").count() == 5)
            check("native UI is not an iframe in the main view", page.locator(".bk-main iframe").count() == 0)

            page.wait_for_function(
                "() => (document.querySelector('#bk-status')?.textContent || '').includes('No titles')",
                timeout=15000)
            check("empty library states its next step", True)

            page.click("#bk-rescan")
            page.wait_for_function(
                "() => document.querySelectorAll('.bk-card').length >= 3",
                timeout=60000)
            check("scan found the fixture titles", page.locator(".bk-card").count() >= 3,
                  str(page.locator(".bk-card").count()))
            titles = page.locator(".bk-card-title").all_inner_texts()
            check("EPUB metadata parsed", any("Dune" in title for title in titles), str(titles))
            check("folder audiobook grouped", any("Book One" in title for title in titles), str(titles))

            # Downloads stream through the core file API (/api/files/download) with
            # the caller's token, so the browser saves the real file, not a copy.
            page.locator('.bk-card', has_text="Dune").first.locator('[data-testid="books-menu"]').click()
            with page.expect_download(timeout=15000) as book_download_info:
                page.locator('[data-testid="books-act-download"]').click()
            book_download = book_download_info.value
            check("card menu downloads the EPUB", book_download.suggested_filename == "Dune.epub",
                  book_download.suggested_filename)
            saved_path = book_download.path()
            check("downloaded book has content", bool(saved_path and Path(saved_path).stat().st_size > 0))

            page.locator('.bk-card', has_text="Dune").first.locator('[data-testid="books-open"]').click()
            page.wait_for_selector(".bk-epub-host iframe", timeout=20000)
            check("EPUB renders in the window", page.locator(".bk-epub-host iframe").count() == 1)
            check("reader offers a download",
                  page.locator('[data-testid="books-reader-download"]').count() == 1)
            page.wait_for_timeout(1200)
            position = page.locator("[data-reader-position]").inner_text()
            check("reader reports a position", "%" in position, position)
            page.click('[data-testid="books-reader-back"]')
            page.wait_for_selector(".bk-root .bk-reader", state="detached", timeout=5000)
            check("reader closes back to the library", page.locator(".bk-reader").count() == 0)

            page.locator('.bk-card', has_text="Manual").first.locator('[data-testid="books-open"]').click()
            page.wait_for_selector('[data-testid="books-pdf-canvas"], [data-testid="books-pdf-frame"]', timeout=25000)
            check("PDF opens in the window",
                  page.locator('[data-testid="books-pdf-canvas"]').count() == 1,
                  "fell back to the inline frame")
            page.click('[data-testid="books-reader-close"]')
            page.wait_for_selector(".bk-root .bk-reader", state="detached", timeout=5000)

            page.click('.bk-nav-item[data-view="audiobooks"]')
            page.wait_for_selector('[data-testid="books-audio-group"]', timeout=10000)
            check("audiobook group listed", page.locator('[data-testid="books-audio-group"]').count() == 1)
            page.locator('[data-testid="books-play"]').first.click()
            page.wait_for_selector(".bk-player", timeout=10000)
            check("player opens with tracks", page.locator(".bk-track").count() == 2)
            with page.expect_download(timeout=15000) as track_download_info:
                page.locator('[data-testid="books-track-download"]').first.click()
            track_download = track_download_info.value
            check("player downloads a single track",
                  track_download.suggested_filename == "01 - Start.mp3",
                  track_download.suggested_filename)
            page.click('[data-testid="books-reader-close"]')

            page.click('.bk-nav-item[data-view="settings"]')
            page.wait_for_selector('[data-testid="books-set-theme"]', timeout=10000)
            page.select_option('[data-testid="books-set-theme"]', "sepia")
            page.wait_for_timeout(800)
            settings = api(base, token, "/api/packages/ethos-books/proxy/settings")
            check("settings persist per account", settings["settings"]["theme"] == "sepia", str(settings))

            page.click('.bk-nav-item[data-view="library"]')
            page.wait_for_selector(".bk-card", timeout=10000)
            page.evaluate("document.documentElement.classList.remove('light'); document.documentElement.classList.add('dark')")
            background = page.evaluate(
                "getComputedStyle(document.querySelector('.bk-root')).getPropertyValue('--pkg-bg').trim()")
            check("dark palette applies", background.startswith("#081"), background)

            page.set_viewport_size({"width": 720, "height": 900})
            page.wait_for_timeout(400)
            direction = page.evaluate("getComputedStyle(document.querySelector('.bk-root')).flexDirection")
            check("phone width uses the horizontal rail", direction == "column", direction)

            check("no page errors", not errors, "; ".join(errors[:3]))
            browser.close()
    finally:
        for process in (proc, service):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(books, ignore_errors=True)

    print(f"\n{len(FAILURES)} failure(s)")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
