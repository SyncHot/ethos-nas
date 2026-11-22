# Feature: Archive Extraction (File Manager)

Right-click an archive → **Extract Here** (folder next to the archive) or
**Extract to…** (destination picker, folder name, password, overwrite).
Double-click keeps the fast path. No new app window; extraction lands where the
user asks and every open Files window refreshes through the file-events WS.

## Rationale (from codebase scan)
- The File Manager already *creates* zips (`GET /api/files/zip` → `create_zip_archive`)
  — extraction completes the loop.
- `_format_info()` classifies `.zip/.7z/…` as `ftype = "archive"`, and the
  universal-search filter already has an `archives` kind.
- The established mutation pipeline is reused unchanged: `broadcast_mutation(action, path, items)`
  (file-events WS → silent refetch of open windows) + Notification Center `emit_event(...)`
  on completion/failure.

## Supported formats

| Input | How | Result |
|---|---|---|
| `.zip`, `.zipx`, `.jar`, `.war`, `.ear`, `.whl`, `.pyz`, `.cbz`, `.xpi`, `.crx`, `.vsix`, `.nupkg`, `.apk` | stdlib `zipfile` (ZIPX/AES → 7-Zip) | files under the chosen folder |
| `.tar`, `.tgz`, `.tbz`, `.txz`, `.tzst`, `.tar.gz/.bz2/.xz/.zst` | stdlib `tarfile` + `filter="data"` | files under the chosen folder |
| `.tlz`, `.tlz4`, `.tlzma`, `.taz`, `.tar.lz4`, `.tar.lz`, `.tar.lzma`, `.tar.Z` | `bsdtar` / 7-Zip | files under the chosen folder |
| bare `.gz`, `.bz2`, `.xz`, `.lzma` | stdlib | one file next to the archive |
| bare `.lz`, `.Z`, `.zst`, `.lz4` | `bsdtar` / 7-Zip (stdout) | one file next to the archive |
| `.7z`, `.rar`, `.cab`, `.iso`, `.ar`, `.deb`, `.cpio`, `.rpm` | `bsdtar` → 7-Zip → `unar` → `unrar` | files under the chosen folder |
| split sets `.part01.rar`, `.r00`, `.7z.001`, `.zip.001`, `.001` | first volume resolved automatically, then 7-Zip/`unar`/`unrar` | files under the chosen folder |

The frontend list mirrors `ARCHIVE_EXTS` in `backend/services/files_service.py`;
`tests/test_archive_extract.py` fails when the two drift apart.

## Design

### Backend — `POST /api/files/extract` (`backend/routers/files.py`)
Body (`FileExtractRequest`, `extra="forbid"`):

| Field | Meaning |
|---|---|
| `path` | archive — any volume of a split set works |
| `dest_dir` | destination folder; default: the archive's folder. Scoped with `path_scope.sanitize()` |
| `subfolder` | omitted → folder named after the archive, collision-suffixed (`foo (1)/`); `""` → straight into `dest_dir` ("extract here"); other → that folder |
| `overwrite` | `false` (default) keeps existing files and reports `skipped_existing`; `true` replaces them |
| `password` | encrypted archives; read from stdin / `zipfile`, never argv, never logged |

Runs in `asyncio.to_thread(extract_archive, …)`. Response:
`{ok, dest_dir, extracted_count, skipped_count, skipped, skipped_existing, tool, archive}`.
Errors: 404 missing, 400 invalid/unsafe/unsupported, 403 scope or service-user
permissions (`_permission_denied`), 409 `{detail, missing_requirement, hint}` when
an extractor is not installed.

### Service — `extract_archive()` in `backend/services/files_service.py`
Signature `extract_archive(archive_path, dest_dir=None, subfolder=None, overwrite=False, password=None)`;
the defaults are exactly the pre-v2 behaviour, so the Download Manager auto-extract
path (`routers/downloads.py`) is untouched. Split sets are resolved with
`split_volume_info()` / `find_first_volume()` (same naming rules as the Download
Manager's package grouping).

Security:
- **ZIP** — per-member zip-slip rejection (absolute paths, any `..` component) plus a
  `realpath()` containment check; entries are written by content copy, so stored
  symlinks can never escape the destination.
- **tar** — `tarfile.extractall(..., filter="data")` (PEP 706); members are pre-scanned
  with `tarfile.data_filter`. Special files (FIFOs/devices — e.g. postfix spool pipes)
  are *skipped* and reported (`skipped_count`) instead of aborting the whole
  extraction; genuinely unsafe entries still fail with 400.
- **CLI extractors** — argument lists only (no `shell=True`, no interpolation),
  `stdin=DEVNULL` unless a password is supplied, 6 h ceiling against a hung prompt, and
  per-tool "keep existing" flags (`bsdtar -k`, `7z -aos`, `unar -s`, `unrar -o-`).
- Folder mode always creates a fresh folder, so a hostile archive can at worst fill
  its own new folder.

### Frontend — `frontend/static/apps/files.js`
- `_ARCHIVE_EXTS`, `_TAR_MULTI_SUFFIXES`, `_SPLIT_PATTERNS`, `_archiveBaseName()`,
  `_isSplitVolumePath()`, `_isArchivePath()`.
- Context menu: **Extract Here** (`fm-ctx-extract`) and **Extract to…**
  (`fm-ctx-extract-to`); with a multi-selection the labels count the selected archives
  and each archive gets its own folder.
- Double-click on an archive → quick "extract here" (folder next to it).
- `_extractArchives(paths, opts)` — one `POST` per archive, one shared indeterminate
  `startTask({appId:'files', testIdPrefix:'fm-op'})` row, summary toast,
  `_silentRefetch(root)`; `extract` is excluded from the duplicate WS toast.
- `_extractToDialog(paths)` — `showFormModal` + `pickFolder` (`core/file-picker.js`):
  destination (remembered in `localStorage['ethos-fm-extract-dest']`), folder name,
  password, overwrite.
- 409 `missing_requirement` → confirm dialog → `POST /api/system/deps/{id}/install`
  (admin only) → retry. Non-admins get the apt hint instead.

## Provisioning
`archive-tools` (`services/system_deps.py`, `deploy/ethos-system-deps`) installs
`libarchive-tools` **and** `p7zip-full`; the requirement id, sudoers rules and
`deploy/provision.json` are unchanged. `POST /api/system/deps/{id}/install` (admin)
runs the root-owned `ethos-system-deps` helper for any serviceable requirement.

## Status
- [x] Phase 1 — Audit & Discovery (v1 shipped in `7a1ac58f`, v2.3.9)
- [x] Phase 2 — Proposal & Scoping (destination, split volumes, passwords, batch)
- [x] Phase 3 — Implementation (engine, API, provisioning, UI)
- [x] Phase 4 — Validation (isolated instance E2E + Playwright)
- [x] Phase 5 — Handoff & Commit

### Verification Log
- `node --check frontend/static/apps/files.js` — OK; backend imports — OK.
- `tests/test_archive_extract.py` on an isolated instance (:9013,
  `ETHOS_DATA_DIR=/tmp/ethos-extract-iso`, API-key login) — **50/50 PASS**:
  format-registry parity, split-volume resolution, folder/collision/destination/
  folder-name modes, extract-here skip + overwrite, tar.gz, bare `.gz`, zip-slip → 400,
  encrypted ZIP (400 without a password, 200 with one, password absent from every
  payload), 404/400/422 validation paths, and 409 missing-extractor naming the first
  volume.
- Playwright on the same instance — the context menu shows both entries for `.zip`
  and `.part02.rar`; the dialog prefills destination + folder name; **Browse…** opens
  the shared folder picker and returns the chosen path; extraction reports
  `Extracted to …/out/Kurs (2 files)`, one progress row finishes at 100 %, no
  duplicate toast, the tree lands on disk; a split volume raises the "Archive tools
  required" prompt with the apt hint.
