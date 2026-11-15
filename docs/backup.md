# Backup Manager v2 — spec & handoff

Versioned backups for folders and Docker Compose projects. **The UI is the Packages app
`backup-manager`** (native window, AntiVirus-style rail); the engine lives in EthOS core so it can use
the `ethos-nas` service user, docker access and `path_scope`. Verified against the code on 2026-11-14.

## File map

| What | Where |
|---|---|
| API (`/api/backup`, admin-only) | `backend/routers/backup.py` |
| Schedules/history/settings stores | `backend/services/backup_store.py` |
| On-disk layout + legacy migration | `backend/services/backup_layout.py` |
| Engine (rsync/docker/verify/restore) | `backend/services/backup_engine.py` |
| Docker discovery + volume/image archives | `backend/services/backup_docker.py` |
| rclone offsite replication | `backend/services/backup_remote.py` |
| Process/cancellation plumbing | `backend/services/backup_proc.py` |
| Queue + notifications + history | `backend/services/backup_jobs.py` |
| Scheduler, catch-up, weekly digest | `backend/services/backup_scheduler.py` |
| Cron parser (shared, extracted) | `backend/services/cron_utils.py` |
| Package UI | `~/apps/compose/ethos-packages/backup-manager/frontend/app.js` |
| Tests | `tests/test_backup_engine.py`, `test_backup_api.py`, `test_backup_logic.py` |

## Data model

`backup_schedules.json` v2 — `{version, schedules: [...]}`. A schedule carries `id, name, type
(folder|docker-compose), enabled, cron, source, destination, options, retention, verify_after_run,
skip_if_missing, remote, last_run_at/last_status/last_generation/last_error, last_scheduled_at`.

- `retention`: `{keep_last, keep_daily, keep_weekly, keep_monthly}` (0–365, default 0/7/4/6).
- `options`: `exclude[]`, `exclude_defaults`, `preserve_acls`, `min_free_gb`; docker adds `projects`
  (`null` = every running project under the source), `include_volumes`, `include_images`, `quiesce`.
- `remote`: `{enabled, remote, path, keep_days, bwlimit}` — rclone mirror of the newest generation.

`backup_history.json` v2 — newest first, capped at 200. Entries add `op_id`, `generation`,
`bytes_copied`, `files_total`, `remote_status`, `verify`.
`backup_settings.json` v1 — new-schedule retention/verify defaults, weekly digest.
A v1 store is migrated by `json_store` (`migrate=`), which also keeps a `.v1-<ts>.json` backup.

## On-disk layout

```
<destination>/<basename(source)>/                    folder schedules
<destination>/docker/<basename(source)>/             docker schedules
  snapshots/<YYYY-MM-DDTHH-MM-SS>/
    data/…                                           folder payload
    projects/<project>/{config,volumes,images}/       docker payload
    manifest.json
  .tmp-<op_id>/                                      workspace (renamed on success)
  .ethos-backup.json                                 layout marker
```

- A generation is renamed into `snapshots/` only after `manifest.json` is written, so an interrupted
  run can never look complete.
- `ensure_layout()` migrates the pre-2.0 layout once: old payload is renamed into a `legacy`
  generation (no copy). Legacy generations are protected until two native generations exist.
- The manifest records status, counts, sizes, rsync stats, docker volumes/images (sha256), verify and
  remote results.

## Backup mechanics

- rsync arguments: `-aHA --numeric-ids -S --delete --info=progress2 --no-inc-recursive
  --partial-dir=.ethos-partial --link-dest=<previous>/data`. A retry without `-A` happens when the
  destination filesystem rejects ACLs.
- Default excludes: `.ethos-partial`, `lost+found`, `#recycle`, `@eaDir`, `.Trash-*`, `.snapshot`,
  `.snapshots`, `.gvfs`.
- Progress lines (`NN%` + bytes) update the operation handle; the package UI shows the shared
  `core/progress.js` panel via `ctx.progress.startTask`.
- Preflight checks the destination, free space (`min_free_gb`) and hardlink support. A missing
  destination is a clean `skipped` run when `skip_if_missing` is set (USB unplugged), not a failure.
- Docker projects are grouped by `com.docker.compose.project` labels and filtered by
  `...working_dir` inside the source. Config is an rsync snapshot; volumes are tarred through a
  throwaway `alpine` container (`--numeric-owner`, temp+rename, sha256); images are re-hardlinked
  when the image id is unchanged. `quiesce` stops/starts project containers around the volume step.
- Retention is GFS: newest + `keep_last` + newest per day/week/month. Pruning runs only after a
  successful run and never below two complete generations.
- Verify: quick (`rsync -rlptn --delete --itemize-changes`, size/mtime) after each run when enabled;
  deep (`-c`) on demand. Docker volumes/images are sha256/tar-integrity checked on deep verify.
- Restore: in-place first creates a `pre-restore` generation (abort if it fails), then merges by
  default; `delete_extra` makes an exact copy. Docker restore stops project containers, restores
  config/volumes/images, then starts them again. `to_folder` restores into a chosen folder without a
  pre-snapshot.
- Offsite: `rclone sync <generation payload> <remote>:<path>/latest/ --checksum
  --backup-dir <remote>:<path>/history/<generation>`, then prunes history older than `keep_days`.
  Failure is a warning (`remote_status=failed`); the local backup stays successful.

## API (v2)

| Method | Path |
|---|---|
| GET | `/api/backup/meta` (API version + capabilities) |
| GET | `/api/backup/overview` (summary, destination usage/free, recent runs) |
| GET | `/api/backup/settings`, PUT `/api/backup/settings` |
| GET/POST | `/api/backup/schedules` |
| PUT/DELETE | `/api/backup/schedules/{id}` (`?delete_data=true` removes snapshots) |
| POST | `/api/backup/schedules/{id}/run` → 202 `{op_id, queued}` |
| POST | `/api/backup/schedules/{id}/dry-run` → 202 |
| GET | `/api/backup/schedules/{id}/generations` |
| POST | `/api/backup/schedules/{id}/generations/{gen}/verify` |
| POST | `/api/backup/schedules/{id}/restore` |
| GET | `/api/backup/status` (running op + queue) |
| POST | `/api/backup/cancel` |
| GET | `/api/backup/history?limit&offset` |
| GET | `/api/backup/docker/projects?source=` |
| GET | `/api/backup/remotes`, POST `/api/backup/remotes/test` |
| GET | `/api/backup/destinations/check?path=` |

Admin router (`require_admin`) plus `scope_request`/`path_scope.sanitize`. Only one operation runs at
a time; further requests are queued (depth 20) and a duplicate schedule/kind returns 409.

## Scheduling

20 s tick. `last_scheduled_at` is persisted per schedule, so a restart cannot re-fire a minute.
A run missed while the service was down is queued once after startup (24 h window). The weekly digest
is emitted through `emit_event` on the configured weekday/hour.

## Package UI

`backup-manager` is a **files-only native package** (`compose: null`): no container, no port, no proxy.
It calls core `/api/backup/*` with `ctx.apiFetch`. Sections: Overview, Schedules (3-step wizard +
Advanced), Restore (generation list, in-place/other folder, merge/exact), Activity, Settings
(defaults, rclone remotes, package info). Both themes, 218 px rail collapsing below 780 px, polling
2 s while a job runs and 20 s idle, timers guarded by `container.isConnected`.

## Status

- [x] v2 stores + v1 migration + legacy layout migration
- [x] rsync hardlink generations, atomic publish, completion manifest
- [x] GFS retention + protected legacy generation
- [x] queue, cancel, missed-run catch-up
- [x] quick/deep verify (folder + docker archives)
- [x] restore in place (pre-restore generation) / to another folder / exact
- [x] Docker project auto-discovery, volumes and images
- [x] rclone mirror + 30-day remote history
- [x] package UI, package published (1.0.0), built-in app removed + layout migration
- [x] installer ships rclone; docs updated
- [ ] Live E2E against the real `Backup1`/`Backup2` schedules after the next production restart

## Deferred

Encryption at rest, restic/borg repositories, bandwidth windows, VM disk backups, btrfs/ZFS snapshot
integration (TODO F-05), per-file restore from the UI (browse the generation in Files), and
auto-pinning a newly installed package icon on the desktop.
