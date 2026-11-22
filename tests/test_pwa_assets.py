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

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
