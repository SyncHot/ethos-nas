# Feature: Per-user filesystem scope & app isolation

Status: **Phases 1–3 implemented** (v2.6.4–v2.6.6). Phase 4 is the remaining
work for "an installed app can never reach more than the signed-in user".

## Problem

- The service user (`ethos-nas`) has broad ACLs, so an unscoped absolute path is
  effectively root read/write for any authenticated account.
- Installed packages that ship a **native UI** (`runtime: "files"`) run
  same-origin in the EthOS page and call core `/api/*` with the **viewer's**
  JWT. The package has no backend, so the core API is the only enforcement
  point. This is why the Code Editor could browse `/`: the viewer was an admin
  and `path_scope.sanitize()` used to skip the allowlist for admins — nothing
  escalated, but nothing was contained either.
- Several core endpoints accepted paths without the role scope
  (`/api/videos/*`, `/api/gallery/meta`, `/api/share-links`,
  `/api/music/config`, qB `createCategory`), so even a plain `user` could
  reach unsafe paths, and a native package could call exactly those APIs.

## Decisions

| Decision | Value |
|---|---|
| Admin filesystem access | Unrestricted (Code Editor still starts at `/` for admins) |
| Non-admin roots | Own home (`pwd.getpwnam`) + `/mnt` + `/media` |
| EthOS state dir | Never user-facing, even when `DATA_DIR` lives under `/home` |
| Package containment | L1: a package never exceeds the signed-in user; no sandbox |
| Existing library config | Migrates to the admin; other accounts start empty |
| Downloads | Shared engine, per-user tasks/dirs/keys |

## Data model

- `backend/services/path_scope.py` — `CURRENT_ROLE` / `CURRENT_USER`
  contextvars, `scope_request`, `sanitize`, `sanitize_for`, `allowed_roots`,
  `home_dir`, `system_scope`/`identity_scope`.
- `backend/services/user_data.py` — `<DATA_DIR>/users/<username>/` per-user
  stores; adopts the pre-multi-user files into the admin's directory at startup.
- Per-user files: `gallery_config.json`, `gallery_index.db`, `albums.json`,
  `music_config.json`, `music_library.json`, `video_station_config.json`,
  `video_station_resume.json`, `editor.json`, `downloads.json` (all under the
  user directory).
- Downloads keep `downloads_config.json` at the DATA_DIR root as the **global
  engine** config (concurrency, schedule, speed limits, package limits, debrid
  credentials, legacy qB key). Each account's download dir, categories, watch
  folders, RSS feeds and extraction policy live in `users/<user>/downloads.json`.
  Tasks carry `owner`; every task endpoint and the qBittorrent API filter by it.
- Share links carry `owner` + `owner_role`; the public route re-validates every
  stored path on each request.
- qBittorrent SID sessions store the caller identity; `_require_auth` publishes
  it for `path_scope`, so a personal API key keeps its own scope.
- Config export/import (`config_archive.py`) includes `users/**/*.json` and each
  per-user `gallery_index.db`.

## API changes

| Endpoint | Change |
|---|---|
| All filesystem routers | Mount `scope_request`; paths through `sanitize()` |
| `/api/videos/*`, `/api/gallery/*`, `/api/music/config`, `/api/share-links` | Router-level scope + sanitized inputs |
| `/api/files/search-roots` | User roots (own home + mounts); `/` only for admins |
| `/api/downloads/qb/torrents/createCategory` | Absolute `savePath` sanitized as the qB caller |
| `/api/share-links/public/{token}` | Re-checks the creator's scope (410 when it no longer applies) |

## Account lifecycle

- `users.json` is authoritative for accounts it knows: the auth middleware
  re-reads it and rejects `status != active` with 401, and takes the role from
  the store — a deleted/disabled account's JWT and API key die immediately, and
  a demoted admin loses admin rights on the next request.
- Deleting an account keeps a **tombstone** (`status: deleted`) so revocation is
  immediate even when `userdel` has to be retried; re-creating the same name
  revives the record only after the Linux account is gone.
- New accounts get a real home (`/home/<user>`) plus an `ethos-nas` ACL, so the
  own-home path scope works from the first login.
- Deletion can purge the account's `DATA_DIR` slices (libraries, trash, agent
  dirs, wallpaper, favorites, desktop layout, download tasks, share links).

## Status

- [x] Fail-closed default identity (`user`, no home)
- [x] Own-home + `/mnt` + `/media` scope, `DATA_DIR` deny
- [x] `scope_request` on gallery, video station, music, share links
- [x] Removed local `ALLOWED_ROOTS` / lexical `_is_allowed` checks
- [x] Sanitized gallery `/meta`, video config, music config, share-link creation,
      qB category paths
- [x] Realpath containment for share-link `?file=`
- [x] Picker defaults to the caller's first allowed root
- [x] Background workers use `system_scope()` (downloads, gallery scan, extraction)
- [x] qBittorrent identity from SID/API key
- [x] `tests/test_path_scope.py` + extended `tests/test_security_invariants.py`
- [x] Phase 2 — per-user gallery/music/video/editor stores
      (`DATA_DIR/users/<user>/`), legacy stores adopted by the admin
- [x] Backup/export includes per-user stores and gallery indexes
- [x] Phase 3 — per-user download config, task owners, per-user qB tasks/categories
      and per-user watch/RSS scans
- [x] Phase 4 — compose package bind hardening: a package may bind only its own
      package/data roots; another package or the package store is rejected
- [x] Account lifecycle — registry status/role enforced per request, tombstones,
      per-user state purge (`docs/user-management.md`)
- [ ] Phase 5 — follow-up: per-package OS confinement for `venv` packages

## Verification

- `tests/test_path_scope.py` (unit + static guard that filesystem routers mount
  `scope_request`).
- `tests/test_security_invariants.py` live probes: user gets 403 on `/etc`,
  other homes, `/tmp`, video stream/info/transcode, gallery meta, share-link
  creation, music config; admin gets 200; user search roots never advertise `/`.
- `tests/test_qb_compat.py`, `tests/test_trash_multiuser_api.py`,
  `tests/test_video_thumb_pipeline.py`.
- `tests/test_user_isolation.py`: two accounts get separate music/gallery/video/
  editor state, and legacy root stores are adopted by the admin.
- `tests/test_config_export.py`: per-user stores and indexes travel with the
  config archive.
- `tests/test_downloads_owner.py`: task visibility by owner, engine settings vs.
  per-user slice, legacy config adoption.
- `tests/test_qb_compat.py`: categories/tasks are scoped to the qB caller.

## Further considerations

1. **`venv` packages run as `ethos-nas`** with the `docker`/`shadow` groups —
   root-equivalent and *not* covered by L1. Fix with a per-package systemd unit
   (`DynamicUser`, `ProtectSystem=strict`, `ReadWritePaths`, no docker group).
2. **Compose packages** keep rw binds to `/home`, `/mnt`, `/media`; the package
   store itself is self-only now. A manifest `permissions.mounts` model (L3)
   should still make rw explicit and default to `:ro`.
3. **Android apps** authenticate with per-user API keys and will inherit the
   per-user libraries from Phase 2/3; verify `/api/app/version` parity.
