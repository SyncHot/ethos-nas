# Feature: Archive Extraction (File Manager)

Right-click / double-click an archive in the File Manager to extract it in place —
The Files app's "Extract" behavior. No new app window; extraction happens
next to the archive and the directory refreshes live via the existing file-events WS.

## Rationale (from codebase scan)
- The File Manager already *creates* zips (`GET /api/files/zip` → `create_zip_archive`)
  but has no way to **unpack** anything — archives are a dead end in the UI.
- `_file_info()` already classifies `.zip/.tar/.gz/…` as `ftype = "archive"`, and the
  universal-search filter already has an `archives` kind — extraction completes that loop.
- The established mutation pipeline is reused unchanged: `broadcast_mutation(action, path, items)`
  (file-events WS → silent refetch of open windows) + Notification Center `emit_event(...)`
  on completion/failure, mirroring the `_notify_status_change()` convention from downloads.

## Design

### Backend — `POST /api/files/extract` (`backend/routers/files.py`)
- Body: `{path}` (reuses `FilePathRequest`). Runs in a worker thread via
  `asyncio.to_thread(extract_archive, …)` so large archives never block the event loop.
- Validation: file must exist (404) and have a supported extension (400).
- Success → `broadcast_mutation("extract", dest_dir, [{name, status:"ok"}])` +
  `emit_event("success", "Archive extracted", …)`; every failure path logs via the ethos
  logger (`ethos-nas.routers.files`) and emits an `error` event.

### Service — `extract_archive()` in `backend/services/files_service.py`
Extraction always lands **next to the archive** and never overwrites existing files:

| Input | Result |
|---|---|
| `.zip`, `.tar`, `.tgz/.tbz/.txz`, tar-in-`.gz/.bz2/.xz` | new subfolder named after the archive (`foo.zip` → `foo/`), numeric suffix on collision (`foo (1)/`) |
| bare single-file `.gz/.bz2/.xz` (not a tar) | decompressed file beside the archive, stem-renamed on collision (`data.txt` → `data (1).txt`) |
| `.7z`, `.rar`, `.zst`, `.lz4` | external tool if installed: `bsdtar` → `7z/7za` → `unrar`; else 500 with install hint |

Security:
- **ZIP** — per-member check rejects absolute paths and any `..` component (zip-slip), plus a
  defense-in-depth `realpath()` containment check; entries are written by content copy, so symlinks
  stored in the zip can never escape the destination.
- **tar** — stdlib `tarfile.extractall(path=dest, members=safe, filter="data")` (PEP 706): strips absolute
  paths, rejects traversal, confines links to the destination. Requires Python ≥ 3.12 (project runs 3.14).
  Members are pre-scanned with `tarfile.data_filter`: **special files** (FIFOs/devices — e.g. postfix spool
  pipes in a mail-server backup) are *skipped* and reported (`skipped_count` in the response, warning log,
  note in the toast/event) instead of aborting the whole extraction; genuinely unsafe entries still fail with 400.
- Subfolder extraction means a hostile archive can at worst fill its own fresh folder — it cannot
  clobber pre-existing files.

### Frontend — `frontend/static/apps/files.js`
- `_ARCHIVE_EXTS` set + `_isArchivePath()` helper (handles multi-suffix `.tar.gz`).
- Context menu: **Extract…** item (`fm-ctx-extract`) for archive files, next to Download.
- Double-click on an archive → extract (previously a no-op "Opening …" toast).
- `_extractArchive(path)` — POSTs `/api/files/extract`, toasts progress/result, `fmLoad()` on success.
- File-events handler: `extract` added to the action-label map so live-sync toasts read "Extracted".

## Status
- [x] Phase 1 — Audit & Discovery
- [x] Phase 2 — Proposal & Scoping (this file)
- [x] Phase 3 — Implementation
- [x] Phase 4 — Validation (isolated instance E2E)
- [x] Phase 5 — Handoff & Commit

### Verification Log
- `node --check frontend/static/apps/files.js` — OK; `.venv/bin/python -c "import backend.routers.files"` — OK.
- Live API E2E (isolated instance :9002, `ETHOS_DATA_DIR=/tmp/ethos-test-data`, `/tmp/test_extract.py`) —
  **14/14 PASS**: zip → subfolder with nested files; re-extract collision → `simple (1)/`; tar.gz → stem-named
  folder; bare `.gz` → single file beside archive, collision → `data (1).txt`; zip-slip entry (`../pwned.txt`)
  → 400 and nothing escapes to `/tmp`; non-archive ext → 400; missing file → 404; Notification Center
  receives both the success event and the error event from source `files`.
- Live UI E2E (Playwright + chromium, same instance, `/tmp/test_extract_ui.py`) — **9/9 PASS**: PAM login via
  form, File Manager navigates to fixture dir, right-click → context menu shows **Extract…**, click extracts and
  the new folder appears live in the grid; double-click on `.tar.gz` also extracts; no page JS errors.
