# Feature: Per-user filesystem scope & app isolation

Status: **Phase 1 implemented** (v2.6.3/v2.6.4). Phases 2–4 are the remaining
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
  `home_dir`, `system_scope`.
- Share links carry `owner` + `owner_role`; the public route re-validates every
  stored path on each request.
- qBittorrent SID sessions store the caller identity; `_require_auth` publishes
  it for `path_scope`, so a personal API key keeps its own scope.

## API changes

| Endpoint | Change |
|---|---|
| All filesystem routers | Mount `scope_request`; paths through `sanitize()` |
| `/api/videos/*`, `/api/gallery/*`, `/api/music/config`, `/api/share-links` | Router-level scope + sanitized inputs |
| `/api/files/search-roots` | User roots (own home + mounts); `/` only for admins |
| `/api/downloads/qb/torrents/createCategory` | Absolute `savePath` sanitized as the qB caller |
| `/api/share-links/public/{token}` | Re-checks the creator's scope (410 when it no longer applies) |

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
- [ ] Phase 2 — per-user gallery/music/video/editor stores (`DATA_DIR/users/<user>/`)
- [ ] Phase 3 — per-user download config, task owners, per-user qB tasks/categories
- [ ] Phase 4 — compose package bind hardening (`PACKAGES_ROOT` self-only)
- [ ] Phase 5 — follow-up: per-package OS confinement for `venv` packages

## Verification

- `tests/test_path_scope.py` (unit + static guard that filesystem routers mount
  `scope_request`).
- `tests/test_security_invariants.py` live probes: user gets 403 on `/etc`,
  other homes, `/tmp`, video stream/info/transcode, gallery meta, share-link
  creation, music config; admin gets 200; user search roots never advertise `/`.
- `tests/test_qb_compat.py`, `tests/test_trash_multiuser_api.py`,
  `tests/test_video_thumb_pipeline.py`.

## Further considerations

1. **`venv` packages run as `ethos-nas`** with the `docker`/`shadow` groups —
   root-equivalent and *not* covered by L1. Fix with a per-package systemd unit
   (`DynamicUser`, `ProtectSystem=strict`, `ReadWritePaths`, no docker group).
2. **Compose packages** keep rw binds to `/home`, `/mnt`, `/media`; a manifest
   `permissions.mounts` model (L3) should make rw explicit and default to `:ro`.
3. **Android apps** authenticate with per-user API keys and will inherit the
   per-user libraries from Phase 2/3; verify `/api/app/version` parity.
