# Core updates — check and apply

Status: **check + apply shipped** (2026-11-15). An admin can install the
published release from Settings → Updates; the privileged helper verifies the
signed index, applies the commit, restarts the service and rolls back on failure.

> Ubuntu **package** upgrades (apt) are a separate layer with their own helper
> and no rollback — see [`os-updates.md`](os-updates.md).

## Why

Installed servers had no way to learn that a newer release exists: `GET
/api/system/version` returned the identity, but no UI consumed it and there was
no published list of releases. This adds one signed index and one card.

## Data model

Published in the repository at `releases/index.json` + `releases/index.json.sig`:

```json
{
  "schema": 1,
  "generated_at": "2026-11-15T12:00:00+00:00",
  "latest": {
    "version": "2.1.0",
    "git_sha": "39d271e",
    "git_sha_full": "39d271e…",
    "build": 412,
    "released_at": "2026-11-15T12:00:00+00:00",
    "notes": "What changed, shown in the card"
  },
  "releases": [ /* the same shape, newest first, capped at 25 */ ]
}
```

`build` is `git rev-list --count HEAD` — monotonic, so the commit that ships the
index itself is *one ahead* of the indexed release and correctly reports
"up to date". Runtime cache: `<DATA_DIR>/update/state.json` (json_store,
schema 1), holding the last result + error.

## Trust chain

1. `scripts/ethos-release publish` signs the index with the offline Ed25519 key
   (`~/.ethos-pack/ethos-packages/repo.key`, never committed).
2. Servers verify it with the public half pinned in `backend/config.py`
   (`RELEASE_PUBKEY_B64`, shared with the package catalog); a mismatch is an
   error, never a silent success.
3. The index URL is HTTPS-only (`ETHOS_UPDATE_INDEX_URL`) and goes through
   `package_repo.get_bytes` (size cap, private-host guard, redirect re-check).

## API

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/system/update` | any user | Cached result; **never** a network request |
| GET | `/api/system/update/status` | any user | Helper job/result + `helper` availability |
| POST | `/api/system/update/check` | admin | Fetch + verify the index now (1 h cache) |
| POST | `/api/system/update` | admin | Write `update/request.json` and start the helper |

Response shape: `{enabled, index_url, key_fingerprint, current{version,git_sha,build},
latest|null, update_available|null, checked_at, checked_at_iso, error, auto_check_hours}`.

## Apply (root helper `ethos-update`)

`POST /api/system/update` never runs anything privileged itself. It:

1. refuses unless a check says an update is available and no job is running,
2. takes a **pre-update data snapshot** (`config_archive.take_snapshot("pre-update")`),
3. writes `<DATA_DIR>/update/request.json` (`{"action":"apply"}`, 0600),
4. spawns `sudo -n /usr/local/bin/ethos-update` detached and returns `{status:"updating"}`.

The helper is the only privileged piece and it takes **no arguments**. Installed
by `setup_scripts/setup_service.sh` under one argument-free sudoers rule; its
settings live in the root-owned `/etc/ethos-nas/update.conf` (checkout, repo
owner, service unit, data dir, index URL, public key).

Helper flow (git-checkout installs):

```
fetch signed index → verify Ed25519 (openssl) → take the signed commit
→ git fetch → git reset --hard <sha>   (as the repo owner, never root)
→ .venv/bin/pip install -r requirements.txt
→ deploy/ethos-provision applies helpers, sudoers, unit and confs
  from the same signed checkout (docs/provisioning.md)
→ systemctl restart ethos-nas
→ poll :9000/api/system/status for 90 s
```

Anything that fails — fetch, reset, dependencies, restart, or a service that
does not answer within 90 s — triggers **auto-rollback**: reset to the previous
commit, reinstall the previous requirements, restart, probe again. Progress and
the outcome are written to `<DATA_DIR>/update/{job,result}.json`; the backend
reports a successful/rolled-back result once after the restart through the
Notification Center.

The helper re-execs itself once into a transient `systemd-run --scope` unit
(`ETHOS_UPDATE_SCOPED=1`), so the `systemctl restart` it performs cannot kill it
mid-job (the unit runs with `KillMode=control-group`). An older helper lived in
the unit cgroup, was killed at its own `restart` step and left `job.json` at
`running` forever — every later update then answered 409 “already running”. The
backend now treats a `restart`/`verify` job older than the current process (or
any job idle for two hours) as **stale**, finalizes it from the checkout at
startup (`reconcile_result`) and reports the outcome once. A plain service
restart is enough to clear an already-stuck install; no state file surgery.

Limits (deliberate):

- Git checkouts only. Tarball/artifact installs are refused with a clear error.
- Code and dependencies roll back; **JSON stores do not**. A release that
  migrated a store needs the pre-update snapshot restored manually
  (`POST /api/system/config-import`).
- Privileged configuration (helpers, `/etc/sudoers.d/ethos-nas-provided`, the
  systemd unit, allowlisted `/etc/ethos-nas/*.conf`) is applied from
  `deploy/provision.json` in the same signed commit. It has its own
  backup/restore path, not git; a failed install aborts the update and restores
  the previous files. Details: [`provisioning.md`](provisioning.md).
- `git reset --hard` keeps untracked files; nothing in the data dir is touched.
- One update at a time (`flock`), and the helper never accepts a URL/ref from
  the web UI.
- Installs older than v2.10.0 still need one `sudo bash setup_scripts/setup_service.sh`
  to install `ethos-provision`. Afterwards privileged configuration follows the
  signed release automatically.

## UI

`Settings → System → EthOS NAS` card: running version + short sha, status line
("Up to date" / "Update available: v2.2.0 · sha" / "Check failed — …"), release
notes when an update exists, the last helper result, an admin-only **Check for
updates** button and — when an update is available — **Update now** (confirm →
progress task → restart overlay). The card reads cached data on open; only the
buttons trigger work.

## Background check

`services/update_service._loop` runs every `ETHOS_UPDATE_CHECK_HOURS` (default
24 h) and **sleeps first**, so short-lived dev/test instances never reach the
network. A newly seen `git_sha` emits one Notification Center event
(`dedupe_key=core-update:<sha>`, source `updates`, action opens Settings).

## Config

| Env | Default | Meaning |
|---|---|---|
| `ETHOS_UPDATE_ENABLED` | `true` | kill switch for the check |
| `ETHOS_UPDATE_INDEX_URL` | `…/SyncHot/ethos-nas/master/releases/index.json` | signed index |
| `ETHOS_UPDATE_PUBKEY` | pinned key | override for a fork/re-sign |
| `ETHOS_UPDATE_TTL_SECONDS` | `3600` | cache TTL |
| `ETHOS_UPDATE_CHECK_HOURS` | `24` | background cadence |

## Releasing

```bash
# after the feature commit is on master
.venv/bin/python scripts/ethos-release publish --notes "What changed"
.venv/bin/python scripts/ethos-release verify
git add releases && git commit -m "release: v2.1.0" && git push
```

## Status

- [x] Signed release index + `scripts/ethos-release` (publish/verify)
- [x] `update_service` (fetch, verify, compare, cache, notify, background loop)
- [x] `GET /api/system/update`, `POST /api/system/update/check` (admin)
- [x] Settings → System card with cached status and manual check
- [x] Tests: `tests/test_update_service.py`, security invariants
- [x] Apply from the UI: helper `deploy/ethos-update`, request file, progress,
      auto-rollback, `POST /api/system/update`, `GET /api/system/update/status`,
      `tests/test_update_helper.py`
- [ ] Artifact/tarball installs (no `.git`) — refused today
- [ ] Channels (stable/beta) and per-server fleet management
