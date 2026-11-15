# Core updates — check (and later: apply)

Status: **check-only shipped** (2026-11-15). Applying an update from the UI is
planned; today the operator upgrades with `git pull` / the commands in
`docs/open-source.md`.

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
| POST | `/api/system/update/check` | admin | Fetch + verify the index now (60 s-1 h cache) |

Response shape: `{enabled, index_url, key_fingerprint, current{version,git_sha,build},
latest|null, update_available|null, checked_at, checked_at_iso, error, auto_check_hours}`.

## UI

`Settings → System → EthOS NAS` card: running version + short sha, status line
("Up to date" / "Update available: v2.1.0 · sha" / "Check failed — …"), release
notes when an update exists, and an admin-only **Check for updates** button.
The card reads the cached result on open; only the button triggers a fetch.

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
- [ ] Apply from the UI (root helper `ethos-update`: backup → git/pip → restart →
      health probe → auto-rollback) — see the plan in `TODO.md` P5-12
