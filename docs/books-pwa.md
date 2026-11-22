# Books PWA (`/books`)

Installable phone client for the Books package: audiobooks that keep playing with the screen off,
EPUB/PDF reading, and **offline downloads** of whole titles. Same account, library, progress and
reader settings as the desktop window — the phone simply reads what the package already knows.

Spec of the desktop app and the metadata-only service: [`docs/books.md`](books.md). Shared PWA
plumbing (one root worker, `/notes` `/videos` `/music` are the other surfaces): `docs/notes-pwa.md`.

## Why a separate page

The desktop shell is a window manager — unusable on a phone. `/books` is a standalone document with
its own manifest, so Android pins it as an app and the browser keeps it in its own task:

| Property | Value |
|---|---|
| id / start_url / scope | `/books` |
| display | `standalone` |
| orientation | any (portrait first) |
| theme colour | `#12100e` |
| icons | `books-192`, `books-512`, `books-maskable-512`, apple-touch 180 |
| shortcuts | `?view=continue` (Słucham), `?view=downloads` (Pobrane) |

The page is core shell (like `/music`), **not** part of the package: `/books` must be reachable
before sign-in and a service worker's scope is its own directory, so a package-served page under
`/api/packages/…` could never be installed. The package stays unchanged — no new privileged code,
and the phone still goes through `/api/files/raw` (path scope) and the package proxy (identity).

## Routes and auth

`/books`, `/books/` and `/manifest-books.webmanifest` are **exact** public paths in
`backend/main.py`. Everything else needs the normal desktop JWT:

- the page reads `localStorage.token` (an existing desktop session is reused) or shows its own
  sign-in card and posts `/api/auth/login`;
- package calls go to `/api/packages/ethos-books/proxy/…` with `Authorization: Bearer` (the core
  stamps `X-Ethos-User`, so a profile only ever sees its own library and shares);
- book bytes come from `/api/files/raw?path=…`, with `&token=` for `<audio>` (media elements cannot
  send headers) and for the downloader;
- **install and downloads need HTTPS** (secure context): service workers, Cache Storage and the
  install prompt do not exist over plain `http://192.168.x.x`. The page still streams online there
  and says so in Pobrane.

## Features

- **Biblioteka** — cover grid with search, "Wszystko / Audiobooki / Książki i PDF" filters and a
  shared-titles section; a title opens a detail sheet.
- **Słucham** — everything in progress, newest first, with a position bar.
- **Pobrane** — device storage, per-title progress, retry and delete.
- **Ustawienia** — the package's reader/player settings (theme, font, size, margin, line height,
  flow, skip, speed), written back so the desktop picks them up.
- **No scanning.** Folders and scans stay in the desktop window; an empty library explains that.

Navigation is `?view=…` inside the single `/books` path, so the worker only has one shell to keep.

## Background playback

One `Audio` element plus the **Media Session** API:

- lock-screen / notification metadata (title, author, chapter) and artwork from the package cover;
- play, pause, previous/next chapter, ±skip and absolute seek from the lock screen or headset;
- `setPositionState` keeps the system seek bar in sync;
- speed, sleep timer and per-track skip come from the package settings;
- the position is written every 5 s, on pause, on chapter end and on `pagehide`/hidden tab.

Expectations: playback continues when the screen turns off and when another app is in front.
**Swiping the app out of Recents kills the page and stops playback** — a browser app has no
foreground service. Downloaded tracks are what make a flight/tunnel work.

## Offline downloads

Two stores, mirroring `/videos`:

| Store | Content |
|---|---|
| Cache Storage `ethos-books-v1` | the bytes: `/__books/<bookId>` (EPUB/PDF) and `/__books/<bookId>/t<index>` (one audiobook track) |
| IndexedDB `ethos-books-db` | `rows` (download state per file), `blobs` (covers), `meta` (library snapshot, progress outbox) |

- A download streams through a `TransformStream` straight into Cache Storage, so a 1 GB audiobook is
  never held in RAM as one Blob.
- `sw.js` answers `/__books/…` from that cache and synthesises `206`/`416` slices for `Range`, so
  seeking works offline; a missing entry answers `404` and the player falls back to the NAS once.
- One file at a time; a full device pauses the queue at 92 % usage (persistent storage requested),
  reports through `core/progress.js` and warns through the Notification Center.
- Interrupted transfers are marked failed with "Przerwane — pobierz ponownie"; retry re-queues only
  the missing tracks, so a partial audiobook resumes where it stopped.
- Deleting a title removes its rows and its cache entries; the NAS copy is never touched.

## Progress sync

`locator`/`position_s`/`percent` keep the package's format, so both clients agree:

| Format | Locator | Position |
|---|---|---|
| EPUB | CFI | — |
| PDF | page number (string) | — |
| Audiobook | track index (string) | seconds inside the track |

Writes are local-first: the newest payload per book is stored in IndexedDB and PUT when the NAS is
reachable (2.5 s debounce, on `online`, on tab focus and on close). A 4xx (the book was forgotten)
drops the entry; a network error keeps it. Last write wins.

## Readers

Vendored in the core (`frontend/static/vendor/`, same pinned builds as the package, see
`THIRD-PARTY.md`) so an installed app opens a downloaded title with no network at all:

- **EPUB** — epub.js 0.3.93 + JSZip 3.10.1. Paginated or scrolled, light/sepia/dark, font, size,
  margins and line height from the shared settings, table of contents, tap zones in paginated mode,
  CFI resume, percentage from generated locations.
- **PDF** — pdf.js 3.11.174 with its worker. Canvas at device pixel ratio, page navigation + input,
  zoom, page resume.
- Both prefer `/__books/<id>` and fall back to `/api/files/raw`.

## Files

| File | Role |
|---|---|
| `frontend/books.html` | login card, library, mini player, full player, reader overlay, sheet |
| `frontend/manifest-books.webmanifest` | install metadata, shortcuts |
| `frontend/static/css/books.css` | mobile-first layout on the shared tokens |
| `frontend/static/books/books.js` | auth, views, routing, sheet, reader/player wiring |
| `frontend/static/books/api.js` | proxy/file URLs, formatting, shared helpers |
| `frontend/static/books/offline.js` | Cache Storage downloads, covers, snapshot, progress outbox |
| `frontend/static/books/player.js` | audio engine, Media Session, speed/skip/sleep, session restore |
| `frontend/static/books/epub.js` | EPUB reader |
| `frontend/static/books/pdf.js` | PDF reader |
| `frontend/static/vendor/{epub,jszip,pdf,pdf.worker}.min.js` | pinned readers |
| `frontend/sw.js` | shell precache, `/__books/…` Range handler, navigate fallback |
| `scripts/make_icons.py` | `render_book()` icon set |

## Tests

```bash
.venv/bin/python tests/test_books_pwa.py      # Playwright: login, readers, download, offline play
.venv/bin/python tests/test_pwa_assets.py     # manifest/icons/public paths/worker guards
.venv/bin/python tests/test_frontend_modules.py
```

`test_books_pwa.py` mocks the package proxy and `/api/files/raw`, generates a real EPUB and PDF, and
asserts that offline playback never touches the NAS.

## Status

- [x] Public `/books` page, manifest, icon set, Settings link
- [x] Library, search, filters, continue list, shared titles
- [x] Audiobook player with Media Session, speed, skip, sleep timer, session restore
- [x] Episode-style progress sync (local-first outbox, package locator format)
- [x] Offline downloads into Cache Storage with Range playback, quota guard and retry
- [x] EPUB reader (paginated/scrolled, themes, TOC, CFI resume)
- [x] PDF reader (canvas, zoom, page resume)
- [x] `tests/test_books_pwa.py` + `test_pwa_assets.py` guards
- [ ] M4B chapter list (one track per file today)
- [ ] Folder add + scan from the phone (deliberately desktop-only for now)
- [ ] iOS tuning (storage cap, older Media Session behaviour)
