# Architecture

How a request flows, who owns what, and where state lives. Status: 2026-11-11 (TODO.md P6-06).

> Always-on rules (security, logging, workflow) live in [`AGENTS.md`](../AGENTS.md); this file is the
> detailed map.

```
browser / Android ──HTTP──▶ FastAPI (backend/main.py)
                              │  auth middleware (JWT: header, cookie, ?token=)
                              ├─▶ routers/*.py        HTTP + WS surface
                              │      └─▶ services/*.py   logic, subprocess, stores
                              └─▶ static/ + index.html (SPA, no build step)
```

## Request lifecycle

1. `auth_middleware` normalises a malformed `?token` in the query string, then short-circuits for
   **exact** public paths plus two prefixes that authenticate internally (`/api/share-links/public/`,
   `/api/downloads/qb`). Everything else needs a valid JWT; the payload is attached to
   `request.state.current_user`.
2. Router dependencies run: `require_admin` on privileged routers, `path_scope.scope_request` on anything
   that touches the filesystem (it must be `async def` — see ADR 0003 note).
3. The handler calls a service in `asyncio.to_thread` for blocking work (docker-py, subprocess, big walks).
4. Failures return `{"detail": ...}` with a real status code; successes return an endpoint-specific body.

## Auth model

| Level | Who | Enforcement |
|---|---|---|
| Public | login, APK version, desktop icons, docs, share-link tokens | exact-path allowlist in `main.py` |
| User | files (scoped), photos, music, video, downloads, notes, editor, search | JWT only |
| Admin | system control, processes kill, terminal host/sudo, docker/systemd/packages/storage/shares/firewall/backup/cleaner/cron/logs | `require_admin` dependency |

Roles come from `<DATA_DIR>/users.json` (`admin` / `user`). Accounts are seeded from PAM on first login;
API keys (for the Android apps) live in the same store.

## Router → service map

| Router | Service layer | Stores |
|---|---|---|
| `files`, `file_ops` | `files_service`, `path_scope`, `trash_service` | `favorites.json`, `trash/<user>/`, `thumbnails/`, `temp_chunks/`, `deletion_log.json` |
| `gallery`, `moments` | — (index inside router) | `gallery_config.json`, `gallery_index.db`, `albums.json`, `trash/<user>/` |
| `downloads` | `qb_compat`, `debrid`, `download_config`, `download_tasks`, `download_transfer`, `bencode`, `rss_service` | `downloads_tasks.json`, `downloads_config.json`, `downloads_rss_seen.json` |
| `music` | — | `music_library.json`, `music_config.json`, `backend/radio_stations.json` (150 stacji, generator), `backend/podcast_catalog.json` (130 podcastów, generator), `radio/state` w `music_library.json` |
| `video_station` | `video_metadata`, `tmdb_service` | `video_station_*.json`, `tmdb_matches.json`, `video_info_cache.json` |
| `surveillance` | `camera_service`, `camera_discovery` | `surveillance.json`, `surveillance_events.json`, `surveillance/` |
| `backup` | — (runner inside router) | `backup_schedules.json`, `backup_history.json` |
| `docker` | `docker_host` (engine status/provisioning) | `compose_projects.json` |
| `auth`, `auth_pam` | `users_store` | `users.json` |
| `notifications` | `notifications_service` | `notifications.json` |
| `health` | `health_monitor_service` | `health_state.json`, `smart_history.json` |
| `network` | `net_stats_service` | `net_stats.json` |
| `system` | `retention_service`, `net_utils` | — |
| `packages` | `package_repo`, `package_installer`, `package_jobs`, `package_schema`, `compose_policy`, `packages_store` | `packages/apps/`, `packages/installed.json`, `packages/sources.json`, `packages/cache.json`, `desktop_layouts.json` |
| `storage` | `usb_mount_service`, `usb_partition_service` | `usb_mounts.json` |
| `shares` | `shares_service` | `/etc/samba/smb.conf` (via sudo) |
| `access_control`, `firewall` | `access_control_service`, `firewall_service` | — |
| `editor` | — | `editor.json` |
| `logs` | `log_service` | `logs/events.jsonl` (rotated) |
| `appearance`, `share_links`, `cron`, `disk_clean`, `smart`, `systemd`, `processes`, `ethos_service`, `updates`, `sync`, `docker_logs`, `file_events_ws` | — | own store or none |

## Store catalog

All JSON stores live under `settings.DATA_DIR` (default `backend/data`, override with `ETHOS_DATA_DIR`).
New code must use `services/json_store.py` (locked, atomic, quarantine, version guard) — see TODO.md P0-14
for the modules still on hand-rolled writers.

| Store | Version | Cap / retention |
|---|---|---|
| `users.json` | — | locked + atomic (`users_store`) |
| `notifications.json` | 1 | 200 events |
| `logs/events.jsonl` | — | ring 5000, 5 × 1 MiB, INFO+ only; legacy `app_events.jsonl` imported once |
| `kanban_boards.json` | 1 | legacy Kanban store — imported by the `ethos-kanban` package |
| `downloads_tasks.json` | — | 500 terminal records (in-flight always kept) |
| `backup_schedules.json` / `backup_history.json` | 1 | history capped by the backup runner |
| `packages/installed.json`, `packages/sources.json` | 1 | per-package records; cache TTL in `cache.json` |
| `compose_projects.json` | — | — |
| `gallery_config.json` | — | via `json_store` |
| `gallery_index.db` | ad-hoc ALTERs | SQLite |
| `health_state.json`, `smart_history.json` | 1 | alerts pruned; devices pruned after 14 d |
| `net_stats.json` | — | ring buffer |
| `share_links.json` | 1 | — |
| `surveillance*.json` | 1 | 200 events |
| `sticky_notes.json` (legacy, imported by the `ethos-sticky-notes` package), `appearance`, `editor.json` | — | per user |
| `tmdb_matches.json`, `video_info_cache.json` | — | unbounded (P0-14) |
| `albums.json`, `favorites.json`, `deletion_log.json` | — | deletion log keeps 50 |
| `desktop_layouts.json`, `usb_mounts.json` | — | — |

Directories with retention: `temp_chunks/` (24 h), `transcodes/` (24 h), `trash/` (30 d),
`thumbnails/` (20 000 files, LRU), `video_thumbs/` (5 000 files, LRU) — all enforced by
`services/retention_service.py` (`GET /api/system/storage-usage` shows the footprint).
`snapshots/` holds the daily state archives (`services/config_archive.py`, keep 7).

### Schema versions and migrations

A store that is an **object** (config-shaped) can declare a schema version:

```python
_CONFIG_STORE = json_store.get(CONFIG_FILE, dict, version=2,
                               migrate=lambda old, new, data: {**data, "renamed": data.pop("name")})
```

- `save()` stamps `version` into the payload; `load()` sets it for older files.
- When `on_disk < declared`, `load()` runs the migrator, writes a `<name>.v<old>-<ts>.json` backup,
  persists the upgraded payload and logs `Store X migrated v1→v2`.
- When `on_disk > declared` (rollback to an older build) the data is **never** used or overwritten:
  `load()` returns the default and logs an ERROR. Startup (`lifespan` → `json_store.verify_versions()`)
  reports every downgrade in one line; `GET /api/system/doctor` shows `store-versions`
  (registered / versioned counts).
- A migrator that raises, or returns a non-dict, aborts the upgrade and leaves the file as it was.
- **Do not version key-mapped stores** (`{username: …}`, `{track_id: …}`, `{project: …}`): the
  `version` field would look like an entry. `sticky_notes.json` (legacy; the
  `ethos-sticky-notes` package imports it), `wallpapers.json`,
  `music_library.json`, `desktop_layouts.json`, `compose_projects.json`, `video_info_cache.json`,
  `tmdb_matches.json`, `video_station_resume.json` are deliberately unversioned for this reason —
  migrate those by reshaping in the module that owns them, or wrap them as `{"version": n, "items": {…}}`
  in a single migration.

## Background loops

| Loop | Owner | Start/stop |
|---|---|---|
| stats WS, health monitor, net-stats sampler, retention sweep | services | `lifespan` start + stop |
| backup cron scheduler | `routers/backup.py` | `start_backup_scheduler` / `stop_backup_scheduler` |
| download watch folder + engine pump + RSS scanner | `routers/downloads.py`, `services/debrid.py` | thread + task registry |
| surveillance motion threads | `camera_service` | start/stop all |
| detached tasks (transcodes, thumbnails, broadcasts, backup runs) | `task_registry` | drained on shutdown |

## Installable app (PWA)

- `frontend/manifest.webmanifest` (served at `/manifest.webmanifest` as `application/manifest+json`)
  with 192/512 icons + a maskable variant generated by `scripts/make_icons.py`, and
  `apple-touch-icon` for iOS.
- `frontend/sw.js` (served at `/sw.js` with `Service-Worker-Allowed: /`) is **network-first**: the network
  answer always wins and the cache only answers when the NAS is unreachable, so a deploy can never be
  masked by a stale worker (the failure mode `frontend_cache_middleware` exists to prevent). It skips
  `/api/*`, websockets, `Range` requests and any `?token=` URL, and registers from `static/app.js` only in a
  secure context (https or localhost — a plain-http LAN install silently skips it).
- Both paths are in `public_paths` (`main.py`): the browser fetches them while the login overlay is up.
- Guard: `tests/test_pwa_assets.py` (manifest fields, icon sizes, worker strategy/scope, registration).

## Frontend structure

- `index.html` — shell, Tailwind CDN, inline CSS (extraction tracked in P3-10).
- `static/app.js` — bootstrap: imports every app, wires Taskbar/registry, dynamic package icons.
- `static/core/` — `window-manager` (windows/taskbar), `api` (fetch + auth), `ws` (reconnect),
  `registry` (apps + desktop icons), `upload-engine`, `progress` (shared task/progress
  center), `modal`, `unified-confirm`, `notification-center`,
  `utils`.
- `static/apps/` — 32 apps, one ES module each; apps must not re-implement notifications, uploads or
  window management.

## See also

- `docs/api-conventions.md` — response shapes and status codes.
- `docs/ops.md` — deployment, upgrade, backup, diagnostics.
- `docs/adr/` — decisions that are expensive to reverse.
- `TODO.md` — known debt, ordered.
