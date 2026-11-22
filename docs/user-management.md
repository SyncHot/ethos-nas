# User management (create, roles, passwords, delete)

Spec for Settings → Users and `/api/users`. Status: **implemented 2026-11-22**.

An EthOS account is two things that must stay in sync:

1. a **Linux account** — PAM authenticates it, `pwd.getpwnam()` supplies its home
   (`/home/<user>`), Samba reuses the same name;
2. a **registry record** in `<DATA_DIR>/users.json` — role, status, API key,
   timestamps.

The registry alone can never grant access (PAM decides), and a Linux account
without a record is auto-registered on its first successful login with the
`user` role.

## Why a privileged helper

`useradd` / `usermod` / `userdel` / `chpasswd` need root; the service runs as
`ethos-nas`. Following the Samba and first-run pattern, everything privileged
goes through one **root-owned, argument-free** helper:
[`deploy/ethos-user-accounts`](../deploy/ethos-user-accounts).

Request contract (one JSON object on stdin, max 16 KiB, no argv):

| Action | Keys | What it does |
|---|---|---|
| `create` | `action, username, password` | `useradd -m -U -s /bin/bash`, `chpasswd`, `chage -d today`, `setfacl` for the service user on the new home |
| `set_password` | `action, username, password` | `chpasswd` + `chage -d today` |
| `lock` / `unlock` | `action, username` | `usermod -L` / `usermod -U` |
| `delete` | `action, username, remove_home` (bool) | `userdel` (`-r` only when `remove_home` is true) |

Validation inside the helper: strict lowercase username pattern (max 32, no
trailing `-`), reserved/system names refused, `uid >= 1000` only, the same
password policy as the first-run wizard (10–200 chars, two character classes,
no `:`/line breaks/control characters), passwords only on stdin.

The home ACL mirrors `setup_service.sh`: without
`setfacl -m u:ethos-nas:rwx` Files/Photos/editor would return 403 for every file
the new account owns.

## Deployment note

`deploy/ethos-update` refreshes only itself; it does not install new helpers.
On a server updated from an older release, run once:

```bash
sudo bash setup_scripts/setup_service.sh
```

Until then `/api/users` mutations answer **503** with that hint, and the Users
section shows the same instruction. `GET /api/users` keeps working (it merges
`pwd` with the registry) with `samba_available: false` when Samba is missing.

## API (`/api/users`, admin only)

All routes use `require_admin`, so a `user` token gets
`403 {"detail": "Admin access required"}`.

| Method + path | Body / query | Result |
|---|---|---|
| `GET /api/users` | `include_deleted=false` | `{users, total, samba_available}`; each row: `id, username, role, status, created_at, last_login, source, uid, home, linux, samba` |
| `POST /api/users` | `{username, password, role, samba_password?}` | **201** `{user, samba_error}` |
| `PATCH /api/users/{username}` | `{role? , status?}` | `{user}` |
| `POST /api/users/{username}/password` | `{password}` | `{ok: true}` |
| `DELETE /api/users/{username}` | `remove_home=false&remove_ethos_state=true` | `{ok, username, status, home_removed, purged, samba_error}` |
| `POST /api/auth/password` | `{current_password, new_password}` | self-service change; PAM re-verifies the current password, 5 failures / 10 min → 429 |

`GET /api/auth/users` is kept as a compatibility alias and returns the old
shape (`id, username, role, status, created_at, last_login`).

Error mapping: `404` unknown account, `409` duplicate / last-admin guard /
self-target guard, `422` username-password-role policy, `503` missing helper.

## Guards

- You cannot demote, disable or delete **your own** account.
- The last **active admin** cannot be demoted, disabled or deleted.
- `root`, `ethos-nas`, `nobody`, system accounts (`uid < 1000`) and reserved OS
  names are never touchable.
- Creating a name whose Linux account already exists is a 409 — finish the
  deletion first. A tombstoned name may be re-created once `userdel` succeeded.
- Passwords are never logged, returned or stored in `users.json`.

## Immediate revocation

`users.json` is authoritative for accounts it knows:

- the middleware (`backend/main.py::auth_middleware`) re-reads it (2 s
  signature-aware cache) and rejects `status != active` with **401**, so a
  deleted/disabled account's 24 h JWT dies immediately;
- the role comes from the store, not from the JWT claim — a demoted admin loses
  admin rights on the next request;
- deletion removes the record's `api_key`, so API-key clients (Android apps) stop
  working too;
- login (`POST /api/auth/login`) rejects disabled/deleted records with **403**.

Deletion writes the tombstone **before** calling `userdel`: if the Linux account
still has running processes the request fails, the tombstone stays, and a retry
of `DELETE` finishes the job.

## Deleting data

`DELETE` always removes:

- Samba credentials (best effort; a warning is returned when absent/failing);
- the Linux account, and the home **only** when `remove_home=true`.

With `remove_ethos_state=true` (default) it also purges:

- `<DATA_DIR>/users/<user>/` (libraries, indexes, editor state, downloads config)
- `<DATA_DIR>/trash/<user>/`
- `<DATA_DIR>/agent-sessions/<user>/`, `<DATA_DIR>/agent-cache/<user>/`
- the user's keys in `wallpapers.json`, `desktop_layouts.json`, `favorites.json`
  and their `wallpapers/<user>.*` file
- queued download tasks owned by the account
- public share links created by the account (revoked)

## UI

`frontend/static/apps/settings.js` → Users (rail section, shared shell):

- table: User (with `you` tag), Role, Status, Home, Samba, Last login, Actions;
- toolbar: **Add user**, **Refresh**;
- actions per row: Role, Password, Disable/Enable, Samba, Delete;
- create/reset/Samba dialogs use `showFormModal`; delete uses a danger form with
  two checkboxes (home **off**, EthOS data **on**);
- destructive actions on your own account are hidden; the topbar user menu has
  **Change password** for the self-service endpoint;
- feedback through `notify()`; no local toasts/panels.

## Files

| What | Where |
|---|---|
| Root helper | `deploy/ethos-user-accounts` |
| Helper installation + sudoers | `setup_scripts/setup_service.sh` |
| Service (helper calls, registry, guards, purge) | `backend/services/user_accounts.py` |
| Registry store (+ status/UUID/cache) | `backend/services/users_store.py` |
| Router | `backend/routers/users.py` |
| Middleware status/role enforcement | `backend/main.py::auth_middleware` |
| Login refusal + self password change | `backend/routers/auth.py` |
| Settings UI | `frontend/static/apps/settings.js::renderUsers` |
| Topbar dialog | `frontend/index.html`, `frontend/static/core/window-manager.js::initTopbarMenu` |
| Tests | `tests/test_user_accounts_helper.py`, `tests/test_users_api.py`, `tests/test_app_ui.py` |

## Status

- [x] root helper with create / set_password / lock / unlock / delete
- [x] helper installed root-owned with an argument-free sudoers rule
- [x] `/api/users` CRUD + admin guards + actionable 503
- [x] status/role enforced from the registry on every request (tombstones)
- [x] login refuses disabled/deleted accounts; API keys revoked on delete
- [x] per-user state purge with `remove_ethos_state`
- [x] Settings → Users management panel and topbar password change
- [x] helper, API and UI guard tests
- [ ] Linux group management and per-share ACL editor (TODO.md F-03 remainder)
- [ ] per-user storage quotas (TODO.md F-14)
