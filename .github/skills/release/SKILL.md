---
name: release
description: "Cut and publish a core EthOS release now: bump APP_VERSION, sign releases/index.json with scripts/ethos-release, verify the chain, commit the index. Use for 'release', 'ship vX.Y.Z' or 'publish an update'. For changes to the updater itself use release-core-update instead."
argument-hint: "What shipped? (one user-facing line for the release notes)"
---

# Release EthOS core

Fast path for a normal release. Code is already merged; nothing here changes the updater.
Touching `deploy/ethos-update`, `backend/services/update_service.py`,
`backend/routers/system_update.py`, the Settings update card or `setup_scripts/setup_service.sh`
additionally requires the checks in `release-core-update`.

Spec: [`docs/updates.md`](../../../docs/updates.md).

## Hard rules

- **Never hand-edit `releases/index.json` or `index.json.sig`** — only `scripts/ethos-release publish`
  produces them.
- **Never commit the signing key.** Offline Ed25519 key: `~/.ethos-pack/ethos-packages/repo.key`
  (0600). The public half is pinned in `backend/config.py` (`RELEASE_PUBKEY_B64`) and
  `setup_scripts/setup_service.sh`.
- **Publish only from a pushed commit.** The helper `git reset --hard`s to the indexed sha, so an
  unpushed sha breaks every server update.
- **Index commit is separate** (`release: vX.Y.Z`) and contains only `releases/`.
- `build` = `git rev-list --count HEAD`; the index commit is one build ahead of the indexed release by
  design — `update_service.is_newer()` still reports "up to date" there. Do not "fix" it.
- Never restart the service yourself; the update path restarts it.

## 1. Preconditions and version

```bash
git status --short                 # must be empty (commit or stash first)
git fetch origin && git status -sb # must not be behind origin
grep '^APP_VERSION' backend/config.py
```

- Bump `APP_VERSION` in `backend/config.py` for **any user-visible change**.
- The bump travels either inside the feature commit or as its own
  `chore(release): bump APP_VERSION to X.Y.Z` — either way it must be **committed and pushed**
  before the index is published.
- Smoke it: `.venv/bin/python -c "from backend.config import APP_VERSION; print(APP_VERSION)"`.

## 2. Publish the signed index

Release notes are one user-facing line, present tense, `Area: what changed` — match the existing
entries in `releases/index.json`:

```bash
.venv/bin/python scripts/ethos-release publish --notes "Packages: … what the user can now do …"
.venv/bin/python scripts/ethos-release verify
#   OK: signature valid — vX.Y.Z · <sha> · build N · M release(s)
```

`publish` refuses a dirty tree or a sha that is not on `origin`. If it reports a missing key, stop and
tell the user — the key must not be copied into the repo or this machine's chat.

## 3. Commit the index and push

```bash
git add releases
git commit -m "release: vX.Y.Z"
git push
```

Both the code commit and the index commit must be on `origin/master`.

## 4. Verify the chain

```bash
curl -s https://raw.githubusercontent.com/SyncHot/ethos-nas/master/releases/index.json | head -c 200
.venv/bin/python -c "from backend.services import update_service as u; print(u.fetch_latest()); print(u.current_release()); print(u.is_newer(u.fetch_latest(), u.current_release()))"
```

The second command proves fetch + signature verification + comparison against the pinned key.
On a server: Settings → System → **Check for updates** → "Update available: vX.Y.Z · sha" →
**Update now**. Servers need `sudo bash setup_scripts/setup_service.sh` once for the root helper.

## If a guard fails

| Failure | Cause / fix |
|---|---|
| `dirty working tree` | commit or stash; the index must describe a real commit |
| `sha is not on origin` | push the code commit first |
| `signature invalid` after `verify` | re-run `publish`; never patch the sig by hand |
| `tests/test_update_helper.py` red | pinned key differs between `config.py` and `setup_service.sh` |

## Done when

- `scripts/ethos-release verify` prints OK for the new version,
- `releases/index.json` + `.sig` are committed as `release: vX.Y.Z` and pushed,
- `APP_VERSION` matches the published version,
- the report lists the version, sha, build number and the exact notes line.

A release is not finished before the tag and the changelog exist: finish with skill
`release-tag-changelog` (annotated `vX.Y.Z` on the indexed sha + `CHANGELOG.md` section).
