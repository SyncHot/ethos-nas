# Apps reference

One short entry per app that did not have its own doc. Format: purpose, the API it uses, the state it
touches, limits, and how it is tested. Feature-specific docs live next to this file (e.g.
`docs/file-manager.md`, `docs/photos-ui-redesign.md`, `docs/kanban.md`).

Status: written 2026-11-11 (TODO.md P6-04).

> Some apps moved to Packages (`~/apps/compose/ethos-packages`, installed from the signed
> `ethos-packages` repository): the Code Editor, Git Diff, Photos, Music, Videos, Cameras,
> Docker Manager, Cleaner, Samba Shares, Kanban, Download Manager and Books. Their UI lives in the
> package; the routers and services described below stay in the core. The Editor stylesheet is still
> part of `frontend/index.html` until TODO.md P3-10 lands.

The core windows share one shell (`docs/app-ui.md`) — 218 px rail, panels, summary tiles, 11 px tables
and one status line — matching the native package windows.

| App | File | Router |
|---|---|---|
| Overview | `apps/overview.js` | `/api/system/status`, `/api/network/history`, `/api/docker/*` (read) |
| Docker Manager | `ethos-packages/ethos-docker/frontend/app.js` (package) | `/api/docker`, `/api/containers`, `/api/images`, `/api/volumes`, `/api/compose/projects` |
| Container logs | `apps/logs.js` (unified viewer) | `/api/logs`, `/ws/logs?source=…` |
| Cleaner | `ethos-packages/ethos-cleaner/frontend/app.js` (package) | `/api/disk-clean` |
| Cron | `apps/cron.js` | `/api/cron` |
| Process manager | `apps/processes.js` | `/api/processes` |
| Services | `apps/services.js` | `/api/systemd`, `/api/ethos-nas-service` |
| SMART | `apps/smart.js` | `/api/smart` |
| Samba shares | `ethos-packages/ethos-shares/frontend/app.js` (package) | `/api/shares` |
| Books | `ethos-packages/ethos-books/frontend/app.js` (package) | `/api/files`, `/api/files/raw` + the package's own venv service (`docs/books.md`) |
| Network | `apps/network.js` | `/api/network` |
| Logs | `apps/logs.js` | `/api/logs`, `/ws/logs/*` |
| Search | `apps/search.js` | `/api/files/universal-search` |
| Settings | `apps/settings.js` (rail: Overview · Updates · Maintenance · Users · Security · Appearance · About) | `/api/system`, `/api/auth`, `/api/health`, `/api/app/version` |

---

## Overview
Live gauges: CPU, RAM/swap, disk, network rate, temperature, uptime, container count.
Polls `/api/system/status` every 2 s and the network history every 60 s; charts are drawn from counter
deltas (no server-side history beyond `net_stats`). Limit: the 2 s poll is the heaviest frontend loop —
see TODO.md P3-09 (shared poller). Tested indirectly by `tests/test_network.py`.

## Docker Manager
Containers (start/stop/restart/remove), images (pull/prune/delete), volumes (prune/delete) and compose
projects (up/down/destroy, with location detection). Mutations are admin-only. Compose commands run with a
10-minute timeout; failures surface as HTTP errors, never as a 200 with an error body.

The app opens with `GET /api/docker/engine` and, when the daemon is missing, replaces the tabs with a
provisioning panel: install Docker Engine through the privileged helper (job progress), or — when the
service account was just added to the `docker` group — a **Restart EthOS** action. Admins get the
buttons; other users get the diagnosis only. See `docs/docker-manager.md`.
State: `backend/data/compose_projects.json` (via `json_store`). Tested: `tests/test_docker_manager_api.py`
(endpoints/auth), `tests/test_docker_host.py` (provisioning decisions).

## Container logs (Logs → container tab)
Streams `docker logs -f` over `/ws/logs/{name}` with a bounded ring buffer. Also exposes the host journal
and `/var/log` files under the same app. Admin-only (journal content can contain credentials).
Tested: no dedicated test (P4-05).

## Cleaner (Disk Cleaner)
Scans mount points for large/old files, then deletes a user-selected subset. Walks are capped
(`MAX_WALK_ENTRIES`), time-boxed, and run in worker threads; deletion refuses a hard-coded set of critical
roots. Admin-only. Tested: no dedicated test (P4-05).

## Cron
CRUD for crontab entries tagged `# ETHOS-CRON:{id}:{enabled}` in the service user's crontab. All
read-modify-writes are serialised by an `asyncio.Lock` and `crontab -l/-` runs off the event loop.
Admin-only (entries execute arbitrary commands as the service user). Tested: no dedicated test (P4-05).

## Process manager
Table of processes with CPU/memory, grouped by name, with cgroup→container attribution and SIGTERM.
Kill is admin-only. Reads are cheap (single `psutil.process_iter` pass in a thread). Tested: no dedicated
test (P4-05).

## Services
Two panes: the EthOS unit itself (`/api/ethos-nas-service` — status, start/stop/restart, enable toggle)
and any systemd unit (`/api/systemd` — list, action). Both admin-only; port state is read from
`/proc/net/tcp`. Tested via the security invariant suite (403 for non-admins).

## SMART
Per-disk SMART attributes and pass/fail, plus the trend rows the health monitor persists
(`smart_history.json`). Requires `smartctl`; the app hides itself when the binary is missing (see
`/api/system/doctor`). Tested: `tests/test_health_monitor.py` covers the trend logic.

## Samba shares
Create/update/delete shares, validation via `testparm`, `smb.conf` written through the narrow sudo rule,
service reload. Mutations admin-only and run off the event loop. Emits Notification Center events.
Tested: `tests/test_shares_service.py` (unit).

## Network
Interfaces, addresses, throughput and the RX/TX history sampler (`net_stats.json`, 60 s cadence).
Read-only. Tested: `tests/test_network.py`.

## Logs
One viewer for every diagnostic source: the unified event store (`core`, `browser`, `pkg:<id>`), host
files under `/var/log`, journald and Docker containers grouped by package. History via `GET /api/logs`,
live updates via `/ws/logs?source=…`; ingest is open to any authenticated user, everything else is
admin-only. Spec: `docs/logs.md`.
Tested: `tests/test_logs_api.py`, `tests/test_logs_ws.py`, `tests/test_logging_ui.py`.

## Search
Unified file search across the sensible roots (`/home`, `/mnt`, `/media`), name/type/size filters,
streamed results. Uses `/api/files/universal-search`; per-role path scope applies.
Tested: `tests/test_file_manager_fixes.py` (search cases).

## Settings
System (info, restart, health card, app versions, storage usage), Users (PAM-seeded accounts), Security
(API key, JWT warning), Appearance (wallpapers). Version display comes from `/api/system/version`.
Tested: `tests/test_appearance.py`, plus the security invariants.

## Backup (Packages)
The Backup UI moved to the Packages package `backup-manager` (native window, rail layout like
AntiVirus/VM Manager). The engine stays in core behind `/api/backup` v2: dated rsync hardlink snapshots,
GFS retention, completion manifests, quick/deep verify, a single-flight queue with cancel, Docker Compose
project discovery (config + named volumes + images), restore in place or to another folder, and optional
rclone offsite replication. Admin-only. State: `backup_schedules.json` (v2), `backup_history.json` (v2),
`backup_settings.json`. Spec: `docs/backup.md`. Tested: `tests/test_backup_engine.py`,
`tests/test_backup_api.py`, `tests/test_backup_logic.py`.

## Packages
Signed static repositories (Ed25519 index + sha256 artifacts), compose-only packages, catalog with
categories/search, detail modal, install/update/uninstall jobs with live progress/logs, source manager
with trust badges, and an Updates tab. Admin-only mutations; unverified sources require an explicit
opt-in. Spec and publishing guide: `docs/packages.md`. Tested: `tests/test_package_logic.py`,
`tests/test_package_repo.py`, `tests/test_pack_cli.py`, `tests/test_packages_api.py`.
