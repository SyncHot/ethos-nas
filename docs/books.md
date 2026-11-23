# Books — Spec & AI Handoff

A Package Center app for reading EPUB/PDF books and listening to audiobooks that already live on the
NAS. The package ships a native EthOS window plus a stdlib-only Python service; the core is
unchanged. Every byte is streamed from the core file API with the caller's own JWT, so books are
never copied and `path_scope` stays the only filesystem gate. Verified against the code on
2026-11-15.

## Package layout

The app lives in the sibling repository, not in the core checkout:

| What | Where |
|---|---|
| Manifest (`runtime: venv`, `user_access`, port 8089) | `~/apps/compose/ethos-packages/ethos-books/manifest.json` |
| Service (stdlib HTTP, JSON stores, covers) | `~/apps/compose/ethos-packages/ethos-books/server/main.py` |
| Native UI (shell, library, readers, player) | `~/apps/compose/ethos-packages/ethos-books/frontend/app.js` |
| Vendored readers (pinned by sha256 in the signed index) | `~/apps/compose/ethos-packages/ethos-books/frontend/vendor/{jszip,epub,pdf,pdf.worker}.min.js` |
| Package README (catalog row, API, licenses) | `~/apps/compose/ethos-packages/ethos-books/README.md` |
| Catalog entry | `~/apps/compose/ethos-packages/index.json` (rebuilt, never hand-edited) |
| Playwright coverage | `tests/test_books_ui.py` |
| Package docs | this file, plus `docs/app-store.md` (published-packages table) and `docs/apps.md` |

## Why there is no core change

| Need | Provided by |
|---|---|
| List a folder, scoped per role | `GET /api/files?path=` (`backend/routers/files.py::list_directory`) |
| Stream a book with Range | `GET /api/files/raw?path=` (Starlette `FileResponse`) |
| Save a title to the device | `GET /api/files/download?path=` (attachment, own JWT in `?token=`) |
| Cover cache for images | `GET /api/files/thumbnail` (unused by Books — covers come from the book file) |
| Per-account state | the package service, reached through `/api/packages/ethos-books/proxy/*` |
| Identity | `X-Ethos-User` stamped by `backend/routers/packages.py::package_ui_proxy` |

The service **never opens a book path**. The browser scans with `ctx.apiJsonFetch`, extracts EPUB
metadata with the vendored JSZip, renders PDF covers with the vendored pdf.js, and posts only
metadata/cover bytes. That is what keeps the package inside the same security invariants as the
core: no new privileged code, no second path resolver.

## Data model

`<DATA_DIR>/packages/data/ethos-books/`:

| File | Shape |
|---|---|
| `library.json` | `{version: 1, users: {<user>: {folders: [{path, added_at, last_scan, count}], books: {<id>: Book}}}}` |
| `progress.json` | `{version: 1, users: {<user>: {<id>: {percent, locator, position_s, finished, updated_at}}}}` |
| `settings.json` | `{version: 1, users: {<user>: {theme, font_family, font_size, margin, line_height, flow, skip_seconds, speed, extensions}}}` |
| `covers/<id>.<ext>` | WebP/PNG/JPEG cover bytes (≤ 512 KB), written by the client |

`Book`: `{id, path, format, kind, title, author, series, series_index, language, size, modified,
cover, cover_ext, manual, added_at, updated_at, tracks[]}`.

- `id` = `bk_` + `sha1(path)[:12]` (computed by the service; the UI receives it in the scan reply).
- `format` ∈ `epub|pdf|m4b|m4a|mp3|flac|ogg|opus`; `kind` = `book` for EPUB/PDF, `audiobook` for audio.
- For a folder audiobook, `path` is the folder and `tracks[]` lists the files inside it.
- `manual: true` means the user edited title/author/series; rescans keep those values.
- Caps: 20 000 books, 64 folders, 200 000 characters total per field family, 500 entries per scan
  batch, 200 tracks per audiobook. Corrupt stores are renamed `*.corrupt-<ts>` and replaced.

## API

All paths are reached as `/api/packages/ethos-books/proxy/<path>`; the caller must be signed in and
the core forwards `X-Ethos-User`/`X-Ethos-Role`. `GET /health` is the supervisor probe.

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `library` | — | folders, books, progress, settings, stats |
| PUT | `folders` | `{folders: [{path}]}` | replaces roots; drops titles whose root disappeared |
| POST | `scan` | `{root, entries[], removed[], done}` | upsert/prune; returns `{added, updated, removed, total, ids}` |
| POST | `books/{id}/cover` | `{data: base64, format}` | 413 above 512 KB |
| GET | `covers/{id}` | — | `image/webp`, private cache |
| PUT | `books/{id}/progress` | `{percent, locator, position_s, finished}` | |
| PUT | `books/{id}/meta` | `{title, author, series, series_index}` | sets `manual` |
| DELETE | `books/{id}` | — | forgets the record only; the file is untouched |
| GET/PUT | `settings` | reader/player prefs | |

Path validation refuses anything outside `/home`, `/mnt`, `/media` and any non-absolute or
`..`-containing path — defence in depth, since the service does not read paths at all.

## Frontend wiring

- `export function render(container, ctx)` — blob module loaded by `_renderNativePackage` in
  `frontend/static/app.js`; uses only `ctx.*` helpers.
- Shell: 218 px rail, panels, summary tiles, tables, tags; both themes via `:root.dark`.
- Views: Library, Continue, Audiobooks, Folders & Scan, Settings. Reader and player are full-bleed
  overlays inside the window (`.bk-reader`), not new windows.
- Vendored readers are fetched with `ctx.apiFetch` and executed as blob `<script>`s (a plain
  `<script src>` cannot send the bearer token). pdf.js gets the worker as a blob URL too, because it
  derives `workerSrc` from the script URL otherwise.
- Downloads build a `/api/files/download?path=…&token=…` link and click a hidden anchor, so the
  browser streams the file straight to disk — a 1 GB audiobook never sits in page memory. The card
  menu, the reader/player bar and the audiobooks table save a whole title; a folder audiobook is one
  file per track (the player has a `⤓` button on every track). Received shares download read-only
  because `/download` uses `sanitize_read`, same as streaming.
- Progress is throttled (2.5 s) and flushed on reader close and on the window close guard
  (`ctx.windows.registerCloseGuard('pkg-ethos-books', …)`).
- Notifications go through `ctx.notifications.toast`; diagnostics through `ctx.log`; long scans use
  one `ctx.progress.startTask`.

## Phone PWA (`/books`)

The mobile client is a core-hosted PWA, not part of the package: `/books` must load before sign-in
and a service worker's scope is its own directory, so a page under `/api/packages/…` could never be
installed. It reads the same per-account API through the package proxy and streams bytes from
`/api/files/raw`, so `path_scope` stays the only filesystem gate. Audiobooks play in the background
(Media Session), downloaded titles live in Cache Storage (`/__books/…`), progress uses the same
`locator`/`position_s` contract, and the EPUB/PDF readers reuse the same pinned libraries. Package
1.2.0 adds per-track durations and embedded audio covers to the scan, merges progress by
`updated_at` and answers `ETag`/`304` so the phone revalidates instead of re-downloading. Full
spec: [`docs/books-pwa.md`](books-pwa.md). Folder management and scanning stay in this window.

## Decisions & constraints

- **Metadata-only service.** Chosen over "service owns the files" so `path_scope` and the service
  user stay the only filesystem authority, and so a 50 GB audiobook is not copied onto the NAS twice.
- **Client-side scanning.** The service cannot call the core as the user and must not read shares, so
  the browser walks `/api/files` (depth ≤ 8, ≤ 20 000 entries, skips `@eaDir`, `#recycle`, dotfiles)
  and posts batches of 100.
- **Vendored libraries.** epub.js 0.3.93 (BSD-2), JSZip 3.10.1 (MIT), pdf.js 3.11.174 (Apache-2.0);
  pinned by `sha256` in the signed manifest, licenses shipped in `frontend/vendor/`.
- **PDF covers** are rendered during the scan (page one, ≤ 80 PDFs per pass).
- **Android parity:** not applicable — no Android Books app exists; nothing to mirror.

## Status

- [x] Package service (library, scan, covers, progress, settings, per-user isolation)
- [x] Native UI shell + Library / Continue / Audiobooks / Folders / Settings views
- [x] EPUB reader (epub.js, CFI resume, TOC, reading themes, fonts/margins)
- [x] PDF reader (pdf.js, page navigation/zoom/resume, inline fallback)
- [x] Audiobook player (folder/M4B grouping, resume, speed, skip, sleep timer)
- [x] Downloads to the device: a title from the card menu, reader bar or audiobooks table, and a
  single track from the player (`/api/files/download`, no copy on the NAS)
- [x] Signed catalog entry + package README
- [x] `tests/test_books_ui.py` (Playwright, skips without the packages checkout)
- [x] Docs: this file, `docs/app-store.md`, `docs/apps.md`
- [x] Phone PWA `/books`: background playback, offline downloads, EPUB/PDF readers
  ([`docs/books-pwa.md`](books-pwa.md))

## Known gaps / next steps

- M4B chapter detection and per-chapter skip (currently one track per file).
- Shelves/collections, reading statistics, full-text search inside books.
- MOBI/AZW3 and comic (CBZ) support.
- PDF text layer (selection/search) — pages are rendered as images today.
