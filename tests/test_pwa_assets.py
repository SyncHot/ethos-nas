#!/usr/bin/env python3
"""PWA plumbing checks (TODO.md P3-14).

Static only — no server needed:

    .venv/bin/python tests/test_pwa_assets.py

Guards the things that silently break installability: a manifest that does not
parse, icons that are missing or the wrong size, the manifest not being linked
from the shell, or a service worker that would cache `/api/` responses (or, worse,
serve stale modules cache-first).
"""

import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()[:26]
    except OSError:
        return None
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def main() -> int:
    manifest_path = FRONTEND / "manifest.webmanifest"
    check("manifest exists", manifest_path.is_file())
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        check("manifest is valid JSON", False, str(exc))
        manifest = {}

    for key in ("name", "short_name", "start_url", "display", "theme_color", "background_color"):
        check(f"manifest has {key}", bool(manifest.get(key)), str(manifest.get(key)))
    check("manifest display is standalone",
          manifest.get("display") in ("standalone", "fullscreen", "minimal-ui"),
          str(manifest.get("display")))

    icons = manifest.get("icons") or []
    sizes = {icon.get("sizes") for icon in icons}
    check("manifest ships 192 and 512 icons", {"192x192", "512x512"} <= sizes, str(sizes))
    check("manifest ships a maskable icon",
          any(i.get("purpose") == "maskable" for i in icons), str(icons))
    for icon in icons:
        src = icon.get("src") or ""
        path = FRONTEND / src.lstrip("/")
        dims = png_size(path)
        declared = (icon.get("sizes") or "").split("x")
        ok = (dims is not None and len(declared) == 2
              and f"{dims[0]}x{dims[1]}" == icon.get("sizes"))
        check(f"icon {src} is a PNG of the declared size", ok, f"{dims} vs {icon.get('sizes')}")

    touch = FRONTEND / "static" / "icons" / "apple-touch-icon.png"
    check("apple-touch-icon is 180×180", png_size(touch) == (180, 180), str(png_size(touch)))

    html = (FRONTEND / "index.html").read_text()
    check("shell links the manifest", 'rel="manifest" href="/manifest.webmanifest"' in html)
    check("shell sets a theme colour", 'name="theme-color"' in html)
    check("shell sets an apple-touch-icon", 'rel="apple-touch-icon"' in html)

    # ── Sticky Notes PWA (`/notes`) ─────────────────────────────────────────
    notes_manifest_path = FRONTEND / "manifest-notes.webmanifest"
    check("notes manifest exists", notes_manifest_path.is_file())
    try:
        notes_manifest = json.loads(notes_manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        check("notes manifest is valid JSON", False, str(exc))
        notes_manifest = {}
    for key in ("name", "short_name", "id", "start_url", "scope", "display",
                "theme_color", "background_color"):
        check(f"notes manifest has {key}", bool(notes_manifest.get(key)), str(notes_manifest.get(key)))
    check("notes manifest is scoped to /notes",
          notes_manifest.get("id") == "/notes"
          and notes_manifest.get("start_url") == "/notes"
          and notes_manifest.get("scope") == "/notes", str(notes_manifest.get("scope")))
    check("notes manifest ships New note / New task shortcuts",
          {s.get("url") for s in notes_manifest.get("shortcuts") or []}
          >= {"/notes?new=note", "/notes?new=checklist"})
    target = notes_manifest.get("share_target") or {}
    check("notes manifest is a GET share target",
          target.get("action") == "/notes" and target.get("method") == "GET", str(target))

    notes_icons = notes_manifest.get("icons") or []
    notes_sizes = {icon.get("sizes") for icon in notes_icons}
    check("notes manifest ships 192 and 512 icons",
          {"192x192", "512x512"} <= notes_sizes, str(notes_sizes))
    check("notes manifest ships a maskable icon",
          any(i.get("purpose") == "maskable" for i in notes_icons), str(notes_icons))
    for icon in notes_icons:
        src = icon.get("src") or ""
        dims = png_size(FRONTEND / src.lstrip("/"))
        check(f"notes icon {src} is a PNG of the declared size",
              dims is not None and f"{dims[0]}x{dims[1]}" == icon.get("sizes"),
              f"{dims} vs {icon.get('sizes')}")
    notes_touch = FRONTEND / "static" / "icons" / "notes-apple-touch-icon.png"
    check("notes apple-touch-icon is 180×180", png_size(notes_touch) == (180, 180),
          str(png_size(notes_touch)))

    notes_html = (FRONTEND / "notes.html").read_text()
    check("notes page links its manifest",
          'rel="manifest" href="/manifest-notes.webmanifest"' in notes_html)
    check("notes page loads the shared tokens + notes styles",
          "/static/css/tokens.css" in notes_html and "/static/css/notes.css" in notes_html)
    check("notes page loads its module", "/static/notes/notes.js" in notes_html)

    main_py = (ROOT / "backend" / "main.py").read_text()
    check("core serves the notes shell", "serve_notes" in main_py and "notes.html" in main_py)
    check("notes shell + manifest are public paths",
          '"/notes",' in main_py and '"/notes/",' in main_py
          and '"/manifest-notes.webmanifest",' in main_py)

    sw_path = FRONTEND / "sw.js"
    check("service worker exists", sw_path.is_file())
    sw = sw_path.read_text() if sw_path.is_file() else ""
    check("worker skips /api/", "startsWith('/api/')" in sw or 'startsWith("/api/")' in sw)
    check("worker skips tokenised URLs", "searchParams.has('token')" in sw)
    check("worker skips Range requests", "headers.has('range')" in sw)
    check("worker is network-first (fetches before cache lookup)",
          sw.index("await fetch(request)") < sw.index("caches.match(request"), "order")
    check("worker prunes old caches on activate", "caches.delete" in sw)
    check("worker precaches the notes shell",
          "'/notes'," in sw and "/static/css/notes.css" in sw and "/static/notes/notes.js" in sw)
    check("worker handles Web Push", "addEventListener('push'" in sw)
    check("worker handles notification clicks", "addEventListener('notificationclick'" in sw)
    check("worker routes notification clicks to /notes",
          "startsWith('/notes')" in sw and "openWindow(target)" in sw)

    app = (FRONTEND / "static" / "app.js").read_text()
    check("bootstrap registers the worker", "serviceWorker.register('/sw.js')" in app)
    check("registration requires a secure context", "isSecureContext" in app)

    # ── Videos PWA (`/videos`) ──────────────────────────────────────────────
    videos_manifest_path = FRONTEND / "manifest-videos.webmanifest"
    check("videos manifest exists", videos_manifest_path.is_file())
    try:
        videos_manifest = json.loads(videos_manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        check("videos manifest is valid JSON", False, str(exc))
        videos_manifest = {}
    for key in ("name", "short_name", "id", "start_url", "scope", "display",
                "theme_color", "background_color"):
        check(f"videos manifest has {key}", bool(videos_manifest.get(key)),
              str(videos_manifest.get(key)))
    check("videos manifest is scoped to /videos",
          videos_manifest.get("id") == "/videos"
          and videos_manifest.get("start_url") == "/videos"
          and videos_manifest.get("scope") == "/videos", str(videos_manifest.get("scope")))
    videos_icons = videos_manifest.get("icons") or []
    check("videos manifest ships 192 and 512 icons",
          {"192x192", "512x512"} <= {i.get("sizes") for i in videos_icons},
          str({i.get("sizes") for i in videos_icons}))
    check("videos manifest ships a maskable icon",
          any(i.get("purpose") == "maskable" for i in videos_icons), str(videos_icons))
    for icon in videos_icons:
        src = icon.get("src") or ""
        dims = png_size(FRONTEND / src.lstrip("/"))
        check(f"videos icon {src} is a PNG of the declared size",
              dims is not None and f"{dims[0]}x{dims[1]}" == icon.get("sizes"),
              f"{dims} vs {icon.get('sizes')}")
    videos_touch = FRONTEND / "static" / "icons" / "videos-apple-touch-icon.png"
    check("videos apple-touch-icon is 180×180", png_size(videos_touch) == (180, 180),
          str(png_size(videos_touch)))

    videos_html = (FRONTEND / "videos.html").read_text()
    check("videos page links its manifest",
          'rel="manifest" href="/manifest-videos.webmanifest"' in videos_html)
    check("videos page loads the shared tokens + videos styles",
          "/static/css/tokens.css" in videos_html and "/static/css/videos.css" in videos_html)
    check("videos page loads its module", "/static/videos/videos.js" in videos_html)
    check("videos page has a toast host and a player",
          'id="toastContainer"' in videos_html and 'id="vd-player"' in videos_html)

    check("core serves the videos shell", "serve_videos" in main_py and "videos.html" in main_py)
    check("videos shell + manifest are public paths",
          '"/videos",' in main_py and '"/videos/",' in main_py
          and '"/manifest-videos.webmanifest",' in main_py)
    check("worker precaches the videos shell",
          "'/videos'," in sw and "/static/videos/videos.js" in sw
          and "/static/css/videos.css" in sw)
    check("worker keeps the offline media cache on activate",
          "MEDIA_CACHE" in sw
          and "new Set([CACHE_VERSION, MEDIA_CACHE, BOOKS_CACHE])" in sw)
    check("worker serves offline media with Range",
          "'/__media/'" in sw and "Partial Content" in sw and "Content-Range" in sw)
    shell_versions = [int(n) for n in re.findall(r"ethos-shell-v(\d+)", sw)]
    check("videos shell cache version was bumped",
          bool(shell_versions) and max(shell_versions) >= 6, str(shell_versions))

    # ── Music PWA (`/music`) — the phone client that replaced the APK ───────
    music_manifest_path = FRONTEND / "manifest-music.webmanifest"
    check("music manifest exists", music_manifest_path.is_file())
    try:
        music_manifest = json.loads(music_manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        check("music manifest is valid JSON", False, str(exc))
        music_manifest = {}
    for key in ("name", "short_name", "id", "start_url", "scope", "display",
                "theme_color", "background_color"):
        check(f"music manifest has {key}", bool(music_manifest.get(key)),
              str(music_manifest.get(key)))
    check("music manifest is scoped to /music",
          music_manifest.get("id") == "/music"
          and music_manifest.get("start_url") == "/music"
          and music_manifest.get("scope") == "/music", str(music_manifest.get("scope")))
    check("music manifest ships Search / Downloads shortcuts",
          {s.get("url") for s in music_manifest.get("shortcuts") or []}
          >= {"/music?view=search", "/music?view=downloads"})
    music_icons = music_manifest.get("icons") or []
    check("music manifest ships 192 and 512 icons",
          {"192x192", "512x512"} <= {i.get("sizes") for i in music_icons},
          str({i.get("sizes") for i in music_icons}))
    check("music manifest ships a maskable icon",
          any(i.get("purpose") == "maskable" for i in music_icons), str(music_icons))
    for icon in music_icons:
        src = icon.get("src") or ""
        dims = png_size(FRONTEND / src.lstrip("/"))
        check(f"music icon {src} is a PNG of the declared size",
              dims is not None and f"{dims[0]}x{dims[1]}" == icon.get("sizes"),
              f"{dims} vs {icon.get('sizes')}")
    music_touch = FRONTEND / "static" / "icons" / "music-apple-touch-icon.png"
    check("music apple-touch-icon is 180×180", png_size(music_touch) == (180, 180),
          str(png_size(music_touch)))

    music_html = (FRONTEND / "music.html").read_text()
    check("music page links its manifest",
          'rel="manifest" href="/manifest-music.webmanifest"' in music_html)
    check("music page loads the shared tokens + music styles",
          "/static/css/tokens.css" in music_html and "/static/css/music.css" in music_html)
    check("music page loads its module", "/static/music/music.js" in music_html)
    check("music page has a toast host, mini player and full player",
          'id="toastContainer"' in music_html and 'id="mu-mini"' in music_html
          and 'id="mu-player"' in music_html)
    check("music page loads hls.js for live radio",
          "/static/vendor/hls.min.js" in music_html)

    check("core serves the music shell", "serve_music" in main_py and "music.html" in main_py)
    check("music shell + manifest are public paths",
          '"/music",' in main_py and '"/music/",' in main_py
          and '"/manifest-music.webmanifest",' in main_py)
    check("worker precaches the music shell",
          "'/music'," in sw and "/static/music/music.js" in sw
          and "/static/css/music.css" in sw and "/static/music/offline.js" in sw)
    check("worker falls back to the music shell when offline",
          "(path === '/music' || path === '/music/')" in sw)
    check("music shell cache version was bumped", max(shell_versions or [0]) >= 7, str(shell_versions))

    # ── Books PWA (`/books`) — audiobooks, EPUB/PDF, offline downloads ────────
    books_manifest_path = FRONTEND / "manifest-books.webmanifest"
    check("books manifest exists", books_manifest_path.is_file())
    try:
        books_manifest = json.loads(books_manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        check("books manifest is valid JSON", False, str(exc))
        books_manifest = {}
    for key in ("name", "short_name", "id", "start_url", "scope", "display",
                "theme_color", "background_color"):
        check(f"books manifest has {key}", bool(books_manifest.get(key)),
              str(books_manifest.get(key)))
    check("books manifest is scoped to /books",
          books_manifest.get("id") == "/books"
          and books_manifest.get("start_url") == "/books"
          and books_manifest.get("scope") == "/books", str(books_manifest.get("scope")))
    check("books manifest ships Continue / Downloads shortcuts",
          {s.get("url") for s in books_manifest.get("shortcuts") or []}
          >= {"/books?view=continue", "/books?view=downloads"})
    books_icons = books_manifest.get("icons") or []
    check("books manifest ships 192 and 512 icons",
          {"192x192", "512x512"} <= {i.get("sizes") for i in books_icons},
          str({i.get("sizes") for i in books_icons}))
    check("books manifest ships a maskable icon",
          any(i.get("purpose") == "maskable" for i in books_icons), str(books_icons))
    for icon in books_icons:
        src = icon.get("src") or ""
        dims = png_size(FRONTEND / src.lstrip("/"))
        check(f"books icon {src} is a PNG of the declared size",
              dims is not None and f"{dims[0]}x{dims[1]}" == icon.get("sizes"),
              f"{dims} vs {icon.get('sizes')}")
    books_touch = FRONTEND / "static" / "icons" / "books-apple-touch-icon.png"
    check("books apple-touch-icon is 180×180", png_size(books_touch) == (180, 180),
          str(png_size(books_touch)))

    books_html = (FRONTEND / "books.html").read_text()
    check("books page links its manifest",
          'rel="manifest" href="/manifest-books.webmanifest"' in books_html)
    check("books page loads the shared tokens + books styles",
          "/static/css/tokens.css" in books_html and "/static/css/books.css" in books_html)
    check("books page loads its module", "/static/books/books.js" in books_html)
    check("books page has a toast host, mini player and full player",
          'id="toastContainer"' in books_html and 'id="bk-mini"' in books_html
          and 'id="bk-player"' in books_html and 'id="bk-reader"' in books_html)

    check("core serves the books shell", "serve_books" in main_py and "books.html" in main_py)
    check("books shell + manifest are public paths",
          '"/books",' in main_py and '"/books/",' in main_py
          and '"/manifest-books.webmanifest",' in main_py)
    check("worker precaches the books shell",
          "'/books'," in sw and "/static/books/books.js" in sw
          and "/static/css/books.css" in sw and "/static/books/offline.js" in sw)
    check("worker precaches the vendored readers",
          "/static/vendor/epub.min.js" in sw and "/static/vendor/pdf.min.js" in sw
          and "/static/vendor/pdf.worker.min.js" in sw)
    check("worker falls back to the books shell when offline",
          "(path === '/books' || path === '/books/')" in sw)
    check("worker serves /__books from its own cache with Range",
          "BOOKS_CACHE" in sw and "'/__books/'" in sw
          and "serveMedia(request, url, BOOKS_CACHE" in sw and "Content-Range" in sw)
    check("books shell cache version was bumped", max(shell_versions or [0]) >= 9, str(shell_versions))
    books_js = (FRONTEND / "static" / "books" / "books.js").read_text()
    check("books pwa has history-backed overlays (Android Back)",
          "openOverlay" in books_js and "popstate" in books_js)
    check("books pwa retries and cancels downloads from the UI",
          "retry-download" in books_js and "cancel-download" in books_js)
    check("books pwa opens downloaded titles without a valid token offline",
          "offlineOnly" in books_js and "offline.loadLibrary" in books_js)
    check("books pwa renders the library in windows with more pages",
          "data-act=\"more\"" in books_js and "visibleBooks" in books_js)
    check("books pwa caches EPUB locations and swipes pages",
          "saveLocations" in (FRONTEND / "static" / "books" / "offline.js").read_text()
          and "__bkSwipe" in (FRONTEND / "static" / "books" / "epub.js").read_text())
    check("books pwa supports pinch-zoom in PDFs",
          "pinchStart" in (FRONTEND / "static" / "books" / "pdf.js").read_text())

    books_offline = (FRONTEND / "static" / "books" / "offline.js").read_text()
    check("books downloads stream into Cache Storage (no whole-file Blob in RAM)",
          "pipeThrough" in books_offline and "cache.put" in books_offline
          and "arrayBuffer()" not in books_offline.replace("await res.arrayBuffer()", ""))
    check("books offline uses the shared progress + notification modules",
          "core/progress.js" in books_offline and "core/notification-center.js" in books_offline)
    books_player = (FRONTEND / "static" / "books" / "player.js").read_text()
    check("books player drives the Media Session for background playback",
          "mediaSession" in books_player and "MediaMetadata" in books_player
          and "setPositionState" in books_player)
    books_js = (FRONTEND / "static" / "books" / "books.js").read_text()
    check("books app never builds its own toast/panel",
          "core/notification-center.js" in books_js and "toastContainer" not in books_js)
    settings_js = (FRONTEND / "static" / "apps" / "settings.js").read_text()
    check("Settings links the /books phone app",
          "books-pwa-link" in settings_js and 'href="/books"' in settings_js)

    updates_py = (ROOT / "backend" / "routers" / "updates.py").read_text()
    check("the Music APK channel is retired",
          "RETIRED_APPS" in updates_py and 'music' in updates_py
          and "_retired(app)" in updates_py)
    settings_js = (FRONTEND / "static" / "apps" / "settings.js").read_text()
    check("Settings no longer offers a Music APK",
          "apk-link-music" not in settings_js and "music-pwa-link" in settings_js)
    check("Settings links the /music phone app", 'href="/music"' in settings_js)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
