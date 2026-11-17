# Downloads — Spec & AI Handoff

Self-hosted download manager (HTTP(S), .torrent/magnet via debrid) exposed as the
**Download Manager package** window and as a **qBittorrent-compatible API** for Radarr/Sonarr.
Everything below was verified against the code on **2026-11-14**.

## Distribution
The UI ships from Packages as `download-manager` (native module, no container,
no port). The engine, stores and `/api/downloads/*` stay in EthOS core, so the API and the
*qBittorrent shim work whether or not the package is installed.

- Package source: `ethos-packages/download-manager/` (manifest `ui.mode: native`,
  `frontend/app.js` exports `render(container, ctx)`).
- Removed from core: `frontend/static/apps/downloads.js` and its desktop registration.
- Existing layouts that pinned `downloads` are migrated to `pkg-download-manager`
  (`LEGACY_LAYOUT_MAP` in `backend/routers/packages.py`).
- Package window sections: Overview, Downloads (drag-and-drop queue, actions, details),
  Add (URL/magnet/.torrent), Settings (general, schedule, limits, debrid, qB key,
  categories, RSS rules, watch folders).

## File map

| What | Where |
|---|---|
| Backend router (endpoints + engine + providers) | `backend/routers/downloads.py` |
| qBittorrent / NzbDrone shim | `backend/services/qb_compat.py` |
| Debrid provider clients | `backend/services/debrid.py` |
| Config defaults/migration/normalization | `backend/services/download_config.py` |
| HTTP transfer engine (range/part/limits) | `backend/services/download_transfer.py` |
| Task-list serialization / history cap | `backend/services/download_tasks.py` |
| RSS parsing / rule matching | `backend/services/rss_service.py` |
| Torrent metadata | `backend/services/bencode.py` |
| Router mount | `backend/main.py` — `/api/downloads` plus `/api/downloads/qb/api` and `/api/downloads/qb/api/v2` |
| Frontend app (Packages) | `ethos-packages/download-manager/frontend/app.js` |
| Registration | Packages install → desktop icon `pkg-download-manager` (legacy `downloads` mapped) |
| Path scoping | `backend/services/path_scope.py` (`scope_request` dependency) |
| Stores | `backend/data/downloads_config.json`, `backend/data/downloads_tasks.json`, `backend/data/downloads_rss_seen.json` (via `services/json_store.py`) |
| Tests | `tests/test_downloads.py`, `tests/test_qb_compat.py`, `tests/test_bencode.py`, `tests/test_debrid_service.py`, `tests/test_download_config.py`, `tests/test_download_tasks.py`, `tests/test_download_transfer.py`, `tests/test_rss_service.py`, `tests/test_notifications.py` (download E2E) |
| Integration notes | `docs/sonarr-download-station.md` |

## Data model

`downloads_config.json` (json_store v7, migrators fold legacy `watch_folder`, categories, RSS and schedule shapes):

```
{ download_dir, max_concurrent (1..16), auto_download_debrid,
  auto_retry, retry_max (0..10), retry_delay (10..3600 s, doubled per attempt),
  duplicate_policy (rename | skip | overwrite),
  auto_extract, auto_extract_delete,                            # unpack archives after download
  watch_enabled, watch_folders: [{name, folder, destination}],   # max 8
  schedule: {enabled, start: "HH:MM", end: "HH:MM"},             # daily start window
  speed_limit: {global_bps, per_task_bps},                       # 0 = unlimited
  categories: [{name, save_path}],                               # "" = <download_dir>/<name>
  rss_feeds: [{id, url, enabled, pattern, exclude, category, destination, interval_minutes}],
  debrid: { provider: ""|realdebrid|alldebrid|torbox,
            realdebrid_api_token, alldebrid_api_key, torbox_api_key },
  qb_api_key }                                                   # generated on first admin read
```

`downloads_tasks.json` is a JSON array. Task fields (public + internal):

| Field | Notes |
|---|---|
| `id` | `dl_<UTCtimestamp>_<hex>` |
| `url`, `name`, `path`, `kind` | `http` \| `debrid` \| `debrid_link` |
| `status` | `waiting`, `downloading`, `debrid_downloading`, `paused`, `completed`, `error`, `canceled`, `skipped` |
| `size`, `downloaded`, `speed`, `error`, `progress` | `progress` is derived (`0..1`) |
| `created_at`, `started_at`, `finished_at` | epoch seconds |
| `_resume_pos`, `_saved_resume_pos`, `_last_resume_save` | HTTP resume bookkeeping |
| `_retry_count`, `_next_retry_at` | backoff bookkeeping; exposed as `retry_count` / `retry_at` (epoch s) |
| `_mirrors`, `_mirror_index`, `_active_url` | extra source URLs, which one is in use; exposed as `mirror_count` / `mirror_index` |
| `_extract_state`, `_extract_dest`, `_extract_error` | `running` / `done` / `error` + destination; exposed as `extract_state` / `extract_dest` / `extract_error` |
| `provider`, `_debrid_id`, `_debrid_status`, `_debrid_progress`, `_files` | debrid internals |
| `priority` | higher starts earlier; default 0, FIFO within the same value |
| `category` | qBittorrent/Sonarr/Radarr category or a user pick; decides the destination |
| `_link_url` (inside `_files`) | provider download URL; RD from `links[]`, AD from unlock, TorBox from `requestdl` |

Never persisted: `_events`, `_debrid_key` (rehydrated from config at task start). Terminal history is
capped at 500 records (`MAX_TASK_HISTORY`).

Derived at read time (also never persisted): `package`, `package_key`, `part` — multi-part archive
grouping. `.partNN.rar/.zip/.7z`, legacy `.r00` and split `.z01`/`.001` volumes are matched after
stripping a trailing `.html` (file-host landing pages like `…part02.rar.html`); `package_key =
sha1(destination folder + base name)[:12]` and `part` is the volume number. Because it is derived
from the name and destination, older task records group without any store migration.

## API

All `/api/downloads/*` routes require a JWT (`Authorization: Bearer`). The router mounts
`path_scope.scope_request`; `download_dir`, per-task `folder` and watch paths are `sanitize()`d, so
non-admins are limited to `/home`, `/mnt`, `/media`.

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/tasks` | user | `status`, `limit` (0..5000), `offset`; returns `{tasks,total,active,max_concurrent}` |
| GET | `/tasks/{id}` | user | 404 unknown |
| POST | `/tasks` | user | `{url, name?, folder?, category?, mirrors?}`; explicit folder > category save_path > download_dir |
| POST | `/tasks/torrent` | user | multipart `.torrent` upload (bencode parsed locally) |
| POST | `/tasks/{id}/pause` | user | active → paused |
| POST | `/tasks/{id}/resume` | user | paused/error → waiting |
| POST | `/tasks/{id}/reset` | user | purge partial payload and restart (also returns to the primary source) |
| POST | `/tasks/{id}/skip` | user | exclude the record from the queue/package without deleting it (409 for completed) |
| POST | `/tasks/{id}/unskip` | user | back to `waiting` |
| POST | `/tasks/{id}/mirrors` | user | `{urls:[http(s)…]}` (max 10); 400 for magnet tasks or when no URL is usable |
| POST | `/tasks/{id}/cancel` | user | `?delete_file=`; removes the record |
| DELETE | `/tasks/{id}` | user | alias of cancel |
| POST | `/tasks/pause-all` / `/tasks/resume-all` / `/tasks/clear-completed` | user | bulk |
| POST | `/tasks/clear-failed` | user | removes every `error` record; deletes leftover `.part` payloads, keeps finished files and debrid folders |
| POST | `/tasks/retry-failed` | user | re-queue every `error` task with a fresh retry budget |
| POST | `/packages/{key}/pause` · `/packages/{key}/resume` | user | every part of a derived multi-part package |
| POST | `/packages/{key}/cancel` | user | `?delete_file=`; removes all parts (provider torrents included) |
| POST | `/packages/{key}/extract` | user | unpack a completed package; 409 unless every part is `completed`, 400 when no archive volume is found |
| POST | `/tasks/{id}/extract` | user | same for a single completed download |
| POST | `/tasks/{id}/files/select` | user | `{file_ids:[int|str]}` (debrid) |
| POST | `/tasks/reorder` | user | `{order:[task_id]}` — first id starts first (sets priorities) |
| POST | `/tasks/{id}/priority` | user | `{priority:-1000..1000}` |
| GET | `/config` | user | debrid keys masked; qB key **masked unless admin**, plus `qb_api_key_set` |
| POST | `/config` | **admin** | partial body; invalid values are 400 |
| POST | `/config/qb-key/rotate` | **admin** | returns the new key |
| POST | `/verify` | **admin** | 401 = rejected key (provider message included), 429 = provider rate limit, 502 = provider unreachable; a masked key (`****…`) verifies the saved one |
| GET | `/summary` | user | counts + `active_speed` (HTTP + debrid) |
| GET | `/rss` | user | configured RSS rules |
| POST | `/rss/{id}/run` | **admin** | run one feed now; `{feeds, queued}` |

qBittorrent shim (`/api/downloads/qb/api[/v2]`) is outside the JWT middleware; everything except
`app/version` and `app/webapiVersion` requires a SID from `/auth/login` (password = `qb_api_key`) or a
`?token=` user API key. `savePath` must be inside `download_dir`; `/torrents/info` returns a JSON array
(field names are qB v2 snake_case; states map to `queuedDL|downloading|metaDL|pausedDL|uploading|error`).

## Frontend wiring

`renderDownloads()` renders the window and starts `downloads.init()`, which registers one shared
`poll(() => fetchTasks(), 2000, {appId:'downloads'})` — visibility-aware and auto-stopped on
`onAppClose`. Fetch responses carry a sequence token so stale responses are discarded. The **Select
Files** dialog keeps a per-task draft (`_selectDrafts`) because every poll replaces `lastFetchedTasks`.
Multi-part archives are grouped JDownloader-style: tasks that share `package_key` (2+ parts) render as
one row with the aggregate status (any active → downloading, then waiting/error/paused/completed),
summed size/speed, `n/m done` and the first error; its actions are package-level Resume / Pause /
Delete / Details, and filters, search and the Overview “Recent activity” table use the same grouping.
Single tasks keep their per-row actions. **Show parts** expands the package into ordinary task
rows, so one dead/misnamed part can be deleted, reset or resumed without dropping the archive.
Transient failures are retried automatically: the row shows `retry 2/3 in 4m`, the toolbar has
**Retry failed** (all error records), and manual Resume/Reset/Retry gives the task a fresh budget.
Adding the same URL to the same folder returns the existing task with `duplicate: true`; the
`duplicate_policy` config decides what happens when the target file already exists.
A task can carry alternative sources: the Add form takes one mirror URL per line, and the row shows
`mirror 2/3` while a fallback source is in use. A broken volume of a package can be **Skipped**
(and **Unskipped**) from its expanded part row: the record stays visible, keeps its package slot but
never starts again.
Completed archives get an **Extract** action (single row or whole package) and show
`Extracting…` / `Extracted` / `Extract failed`; settings expose **Extract archives after download**
and **Delete archives after a successful extraction**.
Long actions (reset, delete-with-file, torrent queue) render through `core/progress.js`
(`startTask`/`finish`). Waiting rows expose a drag handle: dropping a row onto another
waiting row posts the new order to `/tasks/reorder` (the up/down buttons remain as the
keyboard/screen-reader fallback). Helpers come from `core/utils.js`; server strings are
escaped with `escapeHtml`/`escapeAttr`/`jsAttr`.

## Decisions & constraints

- **Debrid-only torrents**: the engine does not run BitTorrent; magnets are resolved by the provider and
  the selected files are streamed over HTTPS. No seeding/ratio; qB reports `uploading` after completion
  so NzbDrone imports.
- **Provider URLs**: Real-Debrid download URLs come from `links[]` in `/torrents/info`; `selectFiles`
  takes `files=` and `instantDownload` does not exist on the RD API. TorBox has no file selection and
  verifies keys at `/v1/api/user/me` (the old `/user/info` path 404s). Real-Debrid rejects bad keys with
  `{"error":"bad_token"}`; AllDebrid answers HTTP 200 with `status:"error"`, which `_ad_verify_token`
  converts into a 401 so the window shows the reason instead of a generic outage.
- **Destinations**: user paths are role-scoped; provider-supplied file paths go through `_safe_join()`
  (`..`, absolute paths and symlink escapes are rejected).
- **Secrets**: debrid keys are masked in `GET /config` and never written to the task store. The qB key is
  a credential — only admins see it; rotation is explicit.
- **qB shim auth is server-to-server** (API key/SID), so it is not path-scoped by role; its `savePath` is
  confined to `download_dir` instead.
- **Partial files**: in-flight data lives in `<dest>.part`; Gallery/indexers and
  Radarr/Sonarr only ever see the final name after the atomic rename.
- **Retry**: a failed task is re-queued (`waiting`) with `_retry_count`/`_next_retry_at` while
  `auto_retry` is on and the message is transient; the delay doubles from `retry_delay` (cap 6 h).
  Permanent messages (dead hoster link, bad token, no provider, disk full) go straight to `error`.
  The pump skips waiting tasks whose retry is in the future; a single `call_later` wakes it. Manual
  Resume/Reset/Retry all clear the counters.
- **Skipped parts**: `skipped` is a terminal status outside the queue — the pump never starts it, the
  retry timer ignores it, the qB shim reports `pausedDL` (NzbDrone must not import it) and
  `_auto_extract_task` refuses to unpack a package with a skipped volume (the archive would be
  incomplete). Skipping drops the `.part` payload but keeps the record and its package key, so the
  part row and the aggregate counts stay meaningful; `unskip` re-queues it with a fresh retry budget.
- **Mirrors / hoster failover**: `mirrors[]` on `POST /tasks` (or `/tasks/{id}/mirrors`) keeps up to 10
  extra sources. A permanent failure (dead link, 403/404, expired premium link) or an exhausted retry
  budget switches to the next mirror instead of ending in `error`; the switch drops `<dest>.part`
  (never resume across hosts), resets size/downloaded and gives the mirror its own retry budget.
  `url` stays the primary source — identity, duplicate detection and the UI label never change;
  `_active_url` is what the worker fetches. Mirrors apply to `http` and `debrid_link` tasks only
  (a magnet has no hoster fallback), and a manual Reset restarts the whole chain from the primary.
- **Duplicates**: the same URL + destination in `waiting`/`downloading`/`paused` returns the existing
  task (`duplicate: true`, no new record). For HTTP downloads, `duplicate_policy` applies when the
  exact destination name exists (`rename` = ` (1)` suffix, `skip` = 409, `overwrite` = same name).
- **Extraction**: `auto_extract` unpacks a finished archive next to it through
  `services/files_service.extract_archive` (`.zip`, `.tar.*`, and `.7z`/`.rar` when `bsdtar` is present —
  the optional `archive-tools` host requirement). Multi-part packages are only unpacked once every part
  is `completed` and always start from the first volume (`part01`/`.zip`/`.001`); `auto_extract_delete`
  removes the consumed volumes after success. Password-protected archives fail with the extractor
  message; manual Extract reuses the same code path. Extraction runs off the event loop
  (`asyncio.to_thread`) and one job per package key is tracked in `_EXTRACT_JOBS`.
- **RSS**: one background loop scans enabled feeds once a minute; each feed has its own
  interval, an include regex (required) and an optional exclude regex. Matched items are
  queued through the same `add_task()` path, and seen item ids live in a capped store.
- **Still open**: the remaining P2-01 extraction, Sonarr install and live provider verification.

## Status

- [x] Engine: HTTP download, Range resume (persisted offset), pause/resume/cancel/reset
- [x] Debrid: AllDebrid magnets + premium links; Real-Debrid magnets; TorBox magnets
- [x] Provider retry/backoff (429/5xx + Retry-After), per-host rate limit
- [x] Disk-space guard (start + every 30 s), terminal history cap, JSON store migration
- [x] Security: path scope, SSRF guard for non-admins, admin-only config/verify, qB key masking + rotation
- [x] qBittorrent shim: SID auth, add/info/properties/files/sync/categories, savePath confinement
- [x] Frontend: live polling, status-aware actions, draft-based file selection, progress rows
- [x] Tests: API/security (`test_downloads.py`), qB contract (`test_qb_compat.py`), UI statics
- [ ] Refactor the router (TODO.md P2-01) — bencode, RSS, debrid clients, config,
      transfer engine and task persistence are extracted; pump/worker scheduling and
      watch folders remain
- [ ] Part-file (`.part` + atomic rename) downloads
- [x] Queue priority + drag-and-drop reorder (up/down buttons as fallback), `topPrio` in the qB shim
- [x] Daily start window + global/per-task bandwidth limits (token bucket in the streamer)
- [x] Categories: config `categories` (name + save path), Add-download picker, row badge,
      settings editor; qB `createCategory`/`deleteCategory` persist the save path
- [x] Task detail dialog: ETA, timestamps, priority, provider state, destination, per-file
      `downloaded_to_nas`; search box and sort selector (persisted in localStorage)
- [x] Search/sort and the task detail dialog
- [x] Multi-part archive packages: grouped rows, aggregate status/progress, package Resume/Pause/Delete/Details
- [x] Auto-retry with exponential backoff, Retry failed bulk action, duplicate/skip/overwrite policy
- [x] Post-download extraction: `auto_extract` / `auto_extract_delete`, package-aware first-volume start,
      manual Extract for a task or package, extract state on the row
- [x] Mirror / hoster failover: `mirrors[]`, per-source retry budget, cross-host resume reset, row indicator
- [x] Per-part skip / unskip: excluded records stay in the package without blocking (except extraction)
- [x] RSS feeds + include/exclude regex rules (background loop, persisted seen ids, manual run)
- [x] Part-file downloads: transfers write `<dest>.part` and rename atomically on success
      (legacy partials are adopted once); cancel/reset remove the part file
- [ ] Refactor the remaining router sections; Sonarr install + live provider E2E
- [ ] Sonarr installed/configured and Radarr+Sonarr end-to-end verified with real provider tokens

## Known gaps / next steps

- Live provider verification requires user tokens; CI only exercises request/response contracts offline.
- `docs/sonarr-download-station.md` still has the "restart server", "Sonarr install" and "end-to-end"
  checkboxes open.
- Upload/queue actions do not yet show a per-item progress row for every file in a multi-file torrent.
