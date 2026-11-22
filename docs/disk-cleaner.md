# Disk Cleaner — Spec & AI Handoff

Space analysis and reclaim for the NAS administrator. The desktop window lives in the
signed packages repository (`ethos-cleaner` 2.0.0); every scan, protection check and delete
runs in the core so permissions, path scope and progress stay in one place.
Everything below verified against the code on 2026-11-22.

## File map

| What | Where |
|---|---|
| Package window | `~/apps/compose/ethos-packages/ethos-cleaner/frontend/app.js` |
| Package manifest | `~/apps/compose/ethos-packages/ethos-cleaner/manifest.json` |
| Backend router | `backend/routers/disk_clean.py` |
| Router mount | `backend/main.py` — `app.include_router(disk_clean_router, prefix="/api/disk-clean")` |
| Service (walks, jobs, suggestions, deletes, history) | `backend/services/disk_clean_service.py` |
| Deletion workers (reused) | `backend/services/files_service.py` — `start_trash_operation`, `start_hard_delete_operation`, `get_file_op` |
| Recycle bin | `backend/services/trash_service.py` |
| Managed caches | `backend/services/retention_service.py` — `usage()`, `sweep(only)` |
| Store | `<DATA_DIR>/disk_clean_history.json` (`json_store`, version 1, capped at 50) |
| Tests | `tests/test_disk_clean.py`, `tests/test_security_invariants.py`, `tests/test_router_smoke.py`, `tests/test_retention.py` |
| UI QA | `tests/test_packages_qa_ui.py`, `scripts/ethos-qa static` |

## Data model

`<DATA_DIR>/disk_clean_history.json`:

```json
{"version": 1, "items": [
  {"at": "2026-11-22T21:04:05Z", "action": "trash|delete|caches|vacuum",
   "items": 12, "failed": 0, "bytes": 104857600, "scope": "cleaner|journal>7d|thumbnails,…"}
]}
```

Newest entry is appended last on disk; `GET /history` returns it reversed. The list is capped at
`HISTORY_LIMIT` (50) on every write. Scan jobs are in-memory only (id `uuid4[:12]`): the last
`MAX_SCAN_JOBS × 10` finished jobs are kept for `SCAN_JOB_TTL_S` (600 s).

## API

All routes are **admin-only** and mount `path_scope.scope_request`; every path goes through
`path_scope.sanitize()`.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/disk-clean/scan?path&depth` | Legacy synchronous tree (kept for 1.0.x clients), 120 s budget → 504 |
| GET | `/api/disk-clean/top-files?path&limit` | Legacy synchronous biggest files |
| POST | `/api/disk-clean/scans` | `{path, depth 1..5, mode: tree\|files, limit}` → `{job_id}`; 429 when two jobs run |
| GET | `/api/disk-clean/scans/{job_id}` | `{state: running\|done\|failed\|cancelled, progress: {entries, bytes, current}, result}` |
| POST | `/api/disk-clean/scans/{job_id}/cancel` | Cooperative `threading.Event`, checked every 2048 entries |
| GET | `/api/disk-clean/suggestions?refresh` | Cached sweep (`SUGGESTION_TTL_S` 120 s) with `summary` + `journal` |
| POST | `/api/disk-clean/delete` | `{paths ≤100, mode: permanent\|trash, async}` → legacy result or `{op_id}` |
| POST | `/api/disk-clean/caches/clean` | `{areas: temp_chunks\|transcodes\|trash\|thumbnails\|video_thumbs}` |
| POST | `/api/disk-clean/vacuum-journal` | `{days 1..3650}` → `{output, freed_bytes}` |
| GET | `/api/disk-clean/history?limit` | Newest first, capped at 50 |
| POST | `/api/system/retention/sweep` | Optional `{areas:[…]}`; no body = every managed area |

`mode="trash"` and `async: true` return an `op_id`; progress is the File Manager's
`GET /api/files/ops/{op_id}` (byte counters, per-path failures). The service watches that operation
and writes one history row when it settles. Without `async` the legacy per-path response
`{deleted, errors, freed_bytes}` is returned unchanged.

## Frontend wiring

- Loaded as a native package module (`ui.mode: "native"`), rendered through
  `_renderNativePackage` in `frontend/static/app.js`; `ctx` provides `apiFetch`, `progress`,
  `modal`, `pickFolder`, `notifications`, `logs` and `escape`.
- Five views: Overview (volumes, managed-cache and trash totals, recent reclaims), Analyze
  (job progress + cancel, breadcrumbs, list/map, biggest files), Cleanup (suggestions, safety
  filters, bulk chunked delete), Caches & apps (managed caches, Docker prune, journal, orphan
  data) and Activity (history + unified Logs link).
- Shared progress panel only (`ctx.progress.startTask` / `update` / `finish`), shared confirms
  (`ctx.modal.confirm` with `okText`/`cancelText`), shared toasts (`ctx.notifications.toast`).
- Every control carries `data-testid` or a stable `data-action`; no inline handlers.

## Decisions & constraints

- **Trash-first.** Analyzer deletions default to the caller's per-user recycle bin; junk categories
  (caches, temp, logs, XDG trash) are permanent because moving them to the bin is meaningless.
- **Protection is explicit.** `protected_reason()` refuses `/`, the system roots, anything inside
  `<DATA_DIR>`, the checkout and `/var/lib/docker`, and any ancestor of those (deleting `/var`
  would take the Docker root with it). This closed a hole where the old router could delete
  EthOS state.
- **Walks degrade, never hang.** Entry cap (400k), wall-clock budgets and a partial-result flag.
- **One sweep, one cache.** `/suggestions` computes each root's children in a single pass and
  reuses the result for two minutes; every mutate action invalidates it.
- **No new WebSocket.** Scan progress is polled; deletes reuse `/api/files/ops`.
- Excluded from 2.0: duplicate finder, scheduled cleanup, Docker build-cache/container prune,
  “Open in Files” deep-links.

## Status

- [x] Backend endpoints (scan jobs, suggestions cache, delete modes, cache reclaim, history)
- [x] Path-scope + protected-tree enforcement
- [x] Package UI 2.0.0 (shared shell, both themes, five views)
- [x] API tests (`tests/test_disk_clean.py`, 41 checks) and CI registration
- [x] Package QA (`scripts/ethos-qa static`, `tests/test_packages_qa_ui.py`)
- [ ] Duplicate finder
- [ ] Scheduled auto-cleanup
