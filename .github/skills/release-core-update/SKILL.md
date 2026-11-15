---
name: release-core-update
description: "Release a core EthOS version (APP_VERSION → signed releases/index.json) or change the update system itself. Use for version bumps, publishing an update, or touching deploy/ethos-update, update_service, /api/system/update and the Settings update card."
argument-hint: "Is this a release (publish the index) or a change to the updater?"
---

# Release a core update

Spec: [`docs/updates.md`](../../docs/updates.md) · operator side: [`docs/open-source.md`](../../docs/open-source.md).

## Why strict

Installed servers move their **code** to whatever commit the **signed** index names, as the repo owner,
and roll back automatically if the service does not come back. So the index is production control
data: a wrong version, a dirty tree or an unverified signature all turn into a broken fleet or a
rejected update. The private key never leaves the maintainer's machine.

## Hard rules

- **Never hand-edit `releases/index.json` or `.sig`.** They are produced only by
  `scripts/ethos-release`.
- **Never commit the signing key.** It lives at `~/.ethos-pack/ethos-packages/repo.key` (0600); only the
  public half is pinned (`backend/config.py` → `RELEASE_PUBKEY_B64`, and the DER copy in
  `setup_scripts/setup_service.sh`). `tests/test_update_helper.py` fails if the two disagree.
- **Bump `APP_VERSION` in `backend/config.py`** for any user-visible change; it is shown in the UI and
  stored in config archives.
- **Publish after the code commit exists on `origin`** — the helper runs `git fetch` + `reset --hard`
  to the indexed sha, so an unpublished sha breaks every update.
- **The helper stays argument-free.** One sudoers rule, root-owned
  `/etc/ethos-nas/update.conf` + `update-pubkey.pem`, validation inside the helper. The web UI may only
  drop `{"action":"apply"}` into `update/request.json`.
- **Check paths never hit the network on read.** `GET /api/system/update` and `…/status` return cached
  state; only `POST …/check` and the 24 h background loop fetch the index (the loop sleeps first).
- **No new privileged command as `ethos-nas`** — git/pip run via `runuser -u <repo owner>` from the
  helper, `systemctl restart` from the helper only.

## Releasing

```bash
# 1. feature work is merged/pushed; working tree clean
git status --short                      # must be empty
grep '^APP_VERSION' backend/config.py    # bump it first if needed

# 2. publish + verify the index (signs with the offline key)
.venv/bin/python scripts/ethos-release publish --notes "What changed in one or two sentences"
.venv/bin/python scripts/ethos-release verify
#   OK: signature valid — vX.Y.Z · <sha> · build N · M release(s)

# 3. commit the index separately, then push both
git add releases && git commit -m "release: vX.Y.Z" && git push
```

`build` is `git rev-list --count HEAD`; the index commit itself is one build ahead of the indexed
release, so `update_service.is_newer()` reports "up to date" on the release commit. Do not "fix" that.

## Verifying the chain end to end

```bash
curl -s https://raw.githubusercontent.com/SyncHot/ethos-nas/master/releases/index.json | head -c 200
.venv/bin/python -c "from backend.services import update_service as u; print(u.fetch_latest()); print(u.current_release()); print(u.is_newer(u.fetch_latest(), u.current_release()))"
```

The second command proves fetch + signature + comparison with the pinned key. On a server, after the
index commit is pushed:

1. Settings → System → **Check for updates** → "Update available: vX.Y.Z · sha".
2. **Update now** (admin) → confirm → progress task → restart overlay.
3. After the reload the card shows the last result; the Notification Center reports
   "EthOS NAS updated" or "Update failed — previous version restored".

## Servers need the helper once

`sudo bash setup_scripts/setup_service.sh` installs `/usr/local/bin/ethos-update`,
`/etc/ethos-nas/update.conf`, the public key and the sudoers rule (idempotent; restarts the unit).
Without it `POST /api/system/update` returns 409 with a message telling you exactly that — the check
still works.

## Changing the updater itself

Touching `deploy/ethos-update`, `backend/services/update_service.py`,
`backend/routers/system_update.py`, the Settings card, or `setup_service.sh` requires:

1. `docs/updates.md` updated (API table, flow, limits).
2. `tests/test_update_service.py` and `tests/test_update_helper.py` green — the helper test asserts the
   static contract (root-only, no args, openssl verification, owner-run git/pip, rollback) and key
   consistency between installer and config.
3. Security invariants: `POST /api/system/update` and `…/check` must stay admin-only (403 for `user`).
4. If the flow or the conf format changes, re-run the installer on the server and say so — the old
   helper keeps running until then.

## Limits to keep documented

- Git checkouts only; no artifact/tarball transport yet (`TODO.md` P5-13).
- Rollback covers code + dependencies, **not** JSON stores; the backend takes a `pre-update` snapshot
  before applying, and a migrated store needs `POST /api/system/config-import` on rollback.
- One update at a time (`flock`); `git reset --hard` leaves untracked files alone and never touches
  `backend/data`.
