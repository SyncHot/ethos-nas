# Files (file manager) — audit & improvement plan

Status: **architecture/history only** (2026-11-08). The live backlog is [`TODO.md`](../TODO.md) (§9);
this file documents the design and shipped changes.

Scope: `frontend/static/apps/files.js`, `frontend/static/apps/fm-controls.js`,
`frontend/static/core/upload-engine.js`, `backend/routers/files.py`,
`backend/routers/file_ops.py`, `backend/services/files_service.py`,
`tests/test_file_station_*.py`.

## 1. Audit — findings

### 1.1 Data-loss / correctness bugs (fixed in this pass)

| # | Where | Problem | Fix |
|---|---|---|---|
| B1 | `files.py::rename_item` | `os.rename` **silently overwrites** an existing target file; renaming to `.`/`..` walks out of the directory | 409 when the target exists, reject `.`/`..`/empty names, log 4xx/5xx |
| B2 | `files.js::fmRenameSelected` (F2) | pending `blur` handler fires after Enter/Escape → duplicate rename request ("Path not found" toast); Escape could still save | one-shot `settled` guard, listener removed on commit, basename preselected |
| B3 | `files.js::fmZipSelected` | builds `state.path + '/' + n` → `//name` at root and **wrong paths in search results view** | resolve via `_fmResolveSelectedPath()` (same helper delete/rename use) |
| B4 | `files.js::_silentRefetch` | ignores `_showHidden` → dotfiles pop into view after any file event; also runs while the Trash view is open | apply hidden filter, skip Trash/search views |
| B5 | `files.js::handleFileEvent` | only the **focused** window is refreshed/updated → other Files windows stay stale | iterate every `.fm-container`, refresh each affected window |
| B6 | `files.js::fmLoad` | no request sequencing: navigating A→B quickly can render A's response last | per-window `_loadSeq` guard (shared with `_silentRefetch`) |
| B7 | `files_service.py::_ChunkStore` | uploading a **0-byte file** fails ("Failed to finalize upload") — the temp file is only created on the first chunk | create the temp file at `create()` time |
| B8 | `files_service.py::append_chunk` | chunks are appended in arrival order; an out-of-order retry corrupts the file | require `chunk_index == len(received)` |
| B9 | `files.js::_showProperties` | re-fetches the parent listing to find the item; directory "size" shows the listing's `total` (item count) in a Size row; no owner/group even though the API returns them | use `GET /api/files/stat` (returns `item_count`, owner, group, mode) |

### 1.2 UX gaps (fixed in this pass)

| # | Area | Problem | Fix |
|---|---|---|---|
| U1 | sorting | `_fmSortFiles` mixes folders and files when sorting by name/size/date; backend and every mainstream file manager group folders first | folders first, then the selected column |
| U2 | context menu | right-clicking empty space shows the **browser** menu — no New Folder / New File / Paste / Select All / Refresh | background context menu (also in Trash and search views) |
| U3 | context menu | menu stays open when clicking elsewhere (no outside-click/Escape close) | document mousedown + Escape + window blur close it |
| U4 | clipboard | copy/cut is stored **per window** — Ctrl+C in one window cannot be pasted in another | module-level clipboard shared by all Files windows |
| U5 | read-only folders | backend returns `can_write` but the UI ignores it: create/paste/rename/delete buttons look enabled and fail with 403 | track `can_write`, disable mutating actions, show "read-only" in the status bar |
| U6 | empty folder | dead end — no call to action | "New folder" / "Upload" buttons in the empty state |
| U7 | list view | "Kind" header is not clickable although `kind` sorting is implemented | clickable header |
| U8 | uploads | after an upload the grid only refreshes via the WS event; if the socket is down the list stays stale | upload-engine fires `ethos:uploads-settled`, `files.js` silently refetches |
| U12 | uploads | uploading showed **no status at all**: `startUpload()` added rows before `#uploadProgressBody` existed, and the placeholder in `index.html` kept its inline `display:none` | panel is built before the first row, always unhidden; rows keyed by a client `jobId` |
| U13 | uploads | finished transfers vanished after 3 s; failures showed only `Chunk upload failed (400)` | rows persist until cleared, server `detail` + Retry on failed rows, status-bar chip mirrors progress || U15 | folder download | a folder had **no Download action** — right-click only offered Download for files; the only route was selecting the tile and pressing the toolbar ZIP button, which most users never found | folder context menu gained **Download Folder (.zip)** (calls `fmZipSelected([path])`) and both routes now show a "Preparing ZIP" notice while the server builds the archive || U9 | shortcuts | no shortcut for a new folder (only Ctrl+N = new file) | Ctrl+Shift+N = new folder |
| U10 | search performance | `_walk_search` crawls `.git`, `node_modules`, `__pycache__`, `.cache`, virtualenvs | prune well-known heavy dirs |
| U11 | bulk trash | multi-item "move to trash" fires N parallel requests and reports `${ok}/${state.selected.size}` after the selection may have changed | capture the count up front |

### 1.3 Logging gaps (AGENTS.md rule: every failure path logs)

* `files.py::_file_info` swallowed `PermissionError`/`OSError` silently (unreadable entries vanished from listings with no trace).
* `mkdir`, `touch`, `delete_item`, `upload` and `rename` handled only `PermissionError`; generic `OSError` (ENOSPC, EROFS, EIO) produced a bare 500 with nothing in the ethos logger.
* `move_to_trash` raised 500 without logging the OS error.

### 1.4 Backlog (not in this pass)

- [ ] Move file-manager logic into `backend/services/files_service.py` (router is ~1.9 k lines).
- [ ] Split `files.js` (3.0 k lines) into `fm-view.js` / `fm-ops.js` / `fm-media.js`.
- [ ] Owner + permissions columns in list view (CSS `.fm-col-owner`, `.fm-col-perm` already exist).
- [ ] Transfer panel showing queued/running background ops (copy/move/hard-delete are polled per operation today).
- [ ] Undo for move/rename (Trash only covers delete).
- [ ] Per-tab sort/view preferences (currently per-window, reset when the app is reopened).
- [ ] Chunked-upload session TTL (abandoned uploads leave files in `backend/data/temp_chunks/`).
- [ ] Uploading a file whose name already exists overwrites it silently — needs a
      conflict prompt / "keep both" policy (`_unique_dest()` already exists for copy/move).
- [ ] Show directory sizes on demand (`/api/files/stat` returns `item_count`, not bytes).
- [ ] ZIP downloads build the complete archive in `DATA_DIR/temp` before sending a byte, so a
      large folder needs free space equal to its size and shows no progress (empty directories
      are not added). Stream the archive or expose an op with progress.
- `.7z`/`.rar`/`.zst`/`.lz4` extraction shells out to `bsdtar` (`libarchive-tools`), then `7z`/`p7zip`
  or `unrar`. The Download Manager package declares the `archive-tools` host requirement, so EthOS
  installs `bsdtar` on package install/update when the root helper and its sudoers rule are present;
  otherwise File Manager keeps showing the "install libarchive-tools or p7zip-full" error.
- [x] Bulk trash via one background op instead of N requests (U14).
- [x] Folder download from the context menu + "Preparing ZIP" notice (U15).

## 2. Implementation checklist

- [x] B1 rename collision guard + name validation + logging
- [x] B2 inline rename single-shot commit
- [x] B3 ZIP paths resolved correctly
- [x] B4 hidden files stay hidden after silent refetch
- [x] B5 all Files windows react to file events
- [x] B6 stale-response guard in `fmLoad`
- [x] B7 0-byte chunked upload
- [x] B8 sequential chunk validation
- [x] B9 Properties via `/api/files/stat`
- [x] U1 folders-first sorting
- [x] U2 background context menu
- [x] U3 context menu closes on outside click / Escape
- [x] U4 clipboard shared across windows
- [x] U5 read-only awareness
- [x] U6 empty-state call to action
- [x] U7 clickable Kind header
- [x] U8 upload-completion refresh fallback
- [x] U12 reliable upload progress panel (panel built before rows, always unhidden)
- [x] U13 persistent rows, error detail + Retry, File Manager status-bar chip
- [x] U14 path-based, op-tracked delete/trash with confirmation and reasons
- [x] U9 Ctrl+Shift+N
- [x] U10 search pruning
- [x] U11 multi-trash count captured before await
- [x] logging gaps from 1.3
- [x] regression test `tests/test_file_manager_fixes.py`

## 3. UX/UI pass 2 — 2026-11-08

Second pass focused purely on interaction quality (no backend changes).

| # | Area | Problem | Fix |
|---|---|---|---|
| S1 | sidebar | `.fm-sidebar` was a fixed-height flex column without a scroll container — Removable/Mounts entries overlapped after ~6 items and everything below was clipped | sidebar sections wrapped in `.fm-side-scroll` (scrollable, `min-height:0`, `flex-shrink:0` on children), inline dividers replaced by `.fm-section-divider` |
| S2 | sidebar | no volume indicator (only the status bar had free space) | `.fm-side-storage` footer with a used/total bar (turns amber at 75 %, red at 90 %); fed by the same `/api/system/status` request as the status bar |
| S3 | toolbar | Copy/Cut/ZIP/Paste looked enabled with nothing selected / empty clipboard and only answered with a toast | `_fmSyncActionButtons()` disables selection- and clipboard-dependent buttons; titles show the count and shortcut; read-only folders disable mutating actions |
| S4 | status bar | "N selected" without any size information | selection summary now shows total bytes of selected files and folder count (`3 selected · 6 KB · 1 folder`) |
| S5 | selection | only click / Ctrl-click / Shift-click — no rubber band | drag on empty space draws `.fm-marquee` and selects every intersecting tile/row (Shift adds, Ctrl-click stays a toggle) |
| S6 | keyboard | only Enter/Escape/F2/F5/clipboard shortcuts | Arrow keys move the selection (grid-aware column jumps), Home/End jump, Shift extends, type-ahead jumps to the first name matching the typed prefix |
| S7 | context menu | multi-selection still offered "Rename…" (which then failed) and singular labels | labels adapt ("Copy 3 Items", "Move 2 Items to Trash"), Rename disabled unless exactly one item is selected |
| S8 | drag & drop | only folders/favorites were drop targets | dragging items onto the sidebar **Trash** moves them to Trash (with a red highlight while hovering) |
| S9 | preview | audio files and PDFs were a dead end (toast only) | audio plays inline in the preview panel, PDFs render in an iframe; the panel header gained Download + "open in Code Editor" actions |
| S10 | preferences | view mode / sort order / hidden-files reset in every new window | persisted in `localStorage`: `ethos-fm-view`, `ethos-fm-sort`, `ethos-fm-hidden` (density was already persisted) |

### Verification (pass 2)

- `node --check` on `files.js` / `fm-controls.js` (as `.mjs`).
- Browser checks on the isolated instance: selection-aware toolbar, selection size, marquee, arrow keys + type-ahead, multi-select context menu, drag-to-Trash, text/audio/PDF preview, view/sort persistence across reload.
- `ETHOS_TEST_BASE=http://127.0.0.1:9002 .venv/bin/python tests/test_file_manager_fixes.py` → all checks pass.

### Remaining backlog

> **Merged into `TODO.md` §9** — track and close items there, not here. The list below is kept
> for historical context and may be stale.

- [ ] Move file-manager logic into `backend/services/files_service.py`.
- [ ] Split `files.js` (3.0 k lines) into `fm-view.js` / `fm-ops.js` / `fm-media.js`.
- [ ] Owner + permissions columns in list view.
- [ ] Transfer panel showing queued/running background ops.
- [ ] Undo for move/rename.
- [ ] Chunked-upload session TTL.
- [ ] Upload name-conflict prompt / "keep both" policy.
- [ ] On-demand directory sizes.
- [ ] Thumbnail cache eviction — `backend/data/thumbnails/` had **115k** files after
      browsing `/tmp`; needs a size/age cap (LRU sweep) instead of orphan-only cleanup.
- [ ] Breadcrumb/toolbar collapse for very narrow windows (the search box steals
      width below ~600 px; a container query should fold it into an icon).

## Third pass — UI/UX polish (top-notch pass)

Goal: make Files feel like a first-class desktop file manager (Finder /
Explorer / Nautilus level), not a web table.

| # | Area | Before | Now |
|---|---|---|---|
| P1 | path bar | plain breadcrumb text that scrolled sideways and was clipped mid-word (`…mpose › ethos-nas`) | bordered **path pill** (unused `.fm-pathbar` CSS adopted) with hover edit affordance |
| P2 | long paths | middle segments pushed the current folder out of view | trails longer than 4 segments collapse into **`…`**; the dropdown lists every hidden segment |
| P3 | navigation by path | impossible without clicking every crumb | **Ctrl+L / click the bar** opens an editable path field — Enter navigates (`~`, relative and absolute paths accepted), Esc cancels |
| P4 | history buttons | Back/Forward always looked enabled | disabled state tracks the per-window history (also `Alt+←/→`) |
| P5 | loading | bare centered spinner | **skeleton tiles** with a shimmer that matches the active density |
| P6 | errors | dead-end `❌ HTTP 404` screen | server detail (permission explanation etc.) + **Retry / Go to Home** actions |
| P7 | quick preview | double-click only | **Space = Quick Look** (text, image, audio, PDF; video opens the player), Space again closes |
| P8 | discoverability | shortcuts existed but nobody knew them | **`?` / F1** opens a keyboard cheat sheet grouped by Navigation / Files / Other |
| P9 | search | click into the box | **`/`** focuses the search field |
| P10 | session continuity | every window opened at `/` | reopens the **last visited folder** (per browser, with a safe fallback to `/home`) |
| P11 | thumbnails | one request per tile; a folder with thousands of images saturated the browser's 6 connections/host and **froze the whole UI** (observed with `/tmp`: 5 894 tiles) | `data-src` + **IntersectionObserver** + max 4 concurrent thumbnail requests; broken/queued thumbnails degrade to a neutral placeholder |
| P12 | selection | only a tinted background | tile shows a **✓ badge**; toolbar buttons carry counts + shortcuts in their tooltips |
| P13 | keyboard focus | invisible | `:focus-visible` ring on every interactive control, `prefers-reduced-motion` respected |
| P14 | accessibility | divs with click handlers | grid = `role=listbox` + `role=option`/`aria-selected`, list = semantic table, status bar is `aria-live=polite`, icon buttons have `aria-label` |
| P15 | tooltips | none | every tile/row exposes name + size + modified on hover |
| P16 | empty folder | emoji icon | SVG folder illustration + New folder / Upload actions |
| P17 | Space on a focused button | — | still activates the button (file-manager shortcuts never steal it) |

Verification (pass 3, Chrome via Playwright + production instance):

- Ctrl+L → edit → `tmp` + Enter → `/tmp`; Esc cancels; back/forward enablement correct.
- Deep path `/opt/ethos-nas/frontend/static/core` renders
  `/ › … › static › core`, `…` menu lists 6 hidden segments, no clipping.
- Skeleton is present synchronously after navigation and replaced by tiles.
- Space opens/closes Quick Look on a `.md` file; `?` sheet renders 20 rows/3 groups.
- Tiles expose `role=option`, `aria-selected`, ✓ badge; ax-tree shows
  `listbox "Folder contents"` with `option "<name> <size> <date>"`.
- Reload → Files reopens in the last folder.
- `node --check` clean; no console errors during the pass.

## 4. Upload status (2026-11-14)

Uploads previously produced no visible status on the first upload after a page
load: `core/upload-engine.js::startUpload()` created progress rows before
`_ensureProgressPanel()` had built `#uploadProgressBody`, and the placeholder in
`index.html` carried an inline `display:none` that only the *else* branch of
`_ensureProgressPanel()` cleared.

Data model (`_jobs`, `Map<jobId, job>`):

| Field | Notes |
|---|---|
| `jobId` | client id, stable across retries; row DOM id is `upload-entry-<jobId>` |
| `uploadId` | server session from `/chunked/init`, re-created on retry |
| `file` | held `File` reference, released when the row is cleared |
| `destDir` | destination passed to `/chunked/init` |
| `status` | `queued → uploading → finalizing → complete` / `failed` |
| `bytesSent`, `progress`, `speed`, `error` | drive the row label, bar and retry button |

Behaviour:

- Every upload is a row in the shared progress center (`core/progress.js`, see
  `docs/progress-ui.md`) — the same component used by copy/move/delete/trash,
  Packages and Docker engine installs. The panel is created by
  `startTask()` before the first row, so the first upload after a page load is
  visible.
- Failed rows show the server's `detail` (via `readErrorDetail()`) and a Retry
  button; retry starts a fresh server session (sequential chunk indexes make
  resuming unsafe).
- Finished rows persist until the user clicks ✓ (clear) or × (hide); hiding the
  panel also clears settled rows, in-flight transfers keep running. Clearing a
  row dismisses its upload task (`onDismiss`), so the status-bar chip stays
  accurate.
- `ethos:uploads-changed` (throttled to 250 ms, immediate on status changes)
  drives a status-bar chip in every File Manager window:
  `⬆ Uploading 2 · 45%`, `⏳ 2 waiting`, `⚠ 1 upload failed`, `✓ 3 uploaded`.
  Clicking the chip re-opens the panel (`showUploadPanel()`).
- `ethos:uploads-settled` still fires once nothing is in flight, so listings
  refresh even when the file-events WebSocket is down.
- A11y: panel body is `role=status` + `aria-live=polite`, bars are
  `role=progressbar` with `aria-valuenow`; filenames are rendered with
  `textContent`.
- Guarded by `tests/test_upload_status_ui.py` (static, no server).

## 5. Delete & trash (2026-11-14)

Symptoms reported: no feedback on big folders (looked frozen), rows staying
visible, opaque 403/500 errors, folders half-deleted, symlinks that could not be
deleted, and the wrong file deleted from search results.

Root causes:

- `fmDeleteSelected()` fired **one POST per item** with no confirmation, no
  busy guard, no `canWrite` check and reduced failures to `N/M moved`.
- `_fmResolveSelectedPath()` resolved by **name**; search rows are duplicated by
  name, so `Delete` could hit a different file than the one clicked.
- `fmLoad()` after a delete cleared the search term and left the Trash view.
- `_delete_worker` used `os.path.exists` (dangling symlinks became `not_found`)
  and `os.path.isdir` (which follows links, so `rmtree` raised
  "Cannot call rmtree on a symbolic link"); `shutil.rmtree` aborted on the first
  error and left a half-deleted tree.

Backend (op-tracked, all return `{op_id}` for `GET /api/files/ops/{op_id}`):

| Endpoint | Purpose |
|---|---|
| `POST /api/files/trash/batch {paths}` | move many items to the caller's trash |
| `DELETE /api/files/trash/items {items:[{name,owner?}]}` | purge selected trash entries |
| `DELETE /api/files/trash/all?all=1` | empty the trash (admin: every account) |
| `DELETE /api/files/delete {paths}` | existing hard delete (fixed) |

The synchronous `/api/files/trash` endpoints stay for compatibility (desktop
trash fallback, older clients, tests). Trash moves use `os.rename` when the
destination is on the same filesystem and a progress-reporting copy otherwise;
a failed copy is removed again so the trash never lists a partial item.
Hard delete unlinks symlinks (including dangling ones) and collects per-entry
`rmtree` failures into `details[].reason`.

Frontend behaviour:

- Every delete asks for confirmation (`Move to Trash` / `Permanently Delete`).
- Deletion resolves the exact `data-path` of each row; the context menu passes
  the clicked path, so duplicate names in search results are safe.
- One batch request per action, a shared progress row (`core/progress.js`,
  deferred ~600 ms so single files only show the toast) and one summary
  notification that lists up to three failure reasons (`permission_denied`,
  `not_found`, `error: …`, `partial: …`).
- Refresh keeps the current view: `_silentRefetch` for listings, `fmSearch()` for
  search results, `fmOpenTrash()` for the Trash view — never a bare `fmLoad()`.
- Trash view: row checkboxes + select-all + "Delete selected", op-tracked
  "Empty Trash", and `readErrorDetail()` for non-JSON error bodies.
- Desktop Trash drop delegates to `window.fmTrashPaths()` (one request).

Guarded by `tests/test_delete_edge_cases.py` (API, isolated instance) and
`tests/test_delete_ux.py` (static).

## 6. Verification

```bash
# isolated instance (never share backend/data with production)
ETHOS_PORT=9002 ETHOS_DATA_DIR=/tmp/ethos-test-data \
  nohup .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 9002 \
  > /tmp/ethos-nas-test.log 2>&1 & disown
ETHOS_TEST_BASE=http://127.0.0.1:9002 .venv/bin/python tests/test_file_manager_fixes.py
```

Frontend changes require only a browser reload; backend changes require a server
restart (Settings → Restart).
