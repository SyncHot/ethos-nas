# Samba Shares — Spec & AI Handoff

Admin-only desktop app for managing Samba shares, account credentials for existing Linux users, and
per-share user/group access. Samba configuration and passdb remain host-owned; EthOS mediates the
privileged operations through narrow helpers. Verified against the implementation on 2026-11-15.

## File map

| What | Where |
|---|---|
| Router | `backend/routers/shares.py` |
| Router mount | `backend/main.py` — `/api/shares` |
| Samba config and passdb service | `backend/services/shares_service.py` |
| Root-owned passdb helper | `deploy/ethos-samba-accounts` |
| Helper/sudoers installer | `setup_scripts/setup_service.sh` |
| Native package | `../ethos-packages/ethos-shares/frontend/app-shell.js` |
| Package manifest | `../ethos-packages/ethos-shares/manifest.json` |
| Tests | `tests/test_shares_service.py`, `tests/test_shares_acl.py`, `tests/test_shares_accounts.py`, `tests/test_shares_accounts_api.py`, `tests/test_samba_accounts_helper.py`, `tests/test_shares_package_ui.py` |

## Data model

- Share definitions live in `/etc/samba/smb.conf`; no new JSON store is introduced.
- `valid users` remains supported for legacy Samba expressions. Structured ACLs use `read list`,
  `write list`, and the corresponding `valid users` values in `smb.conf`.
- Samba credentials live in Samba's passdb and are changed only through `smbpasswd` via the root
  helper. Passwords are never persisted by EthOS.
- User/group choices are read from the host account database. The feature never creates or deletes
  Linux users or groups.
- Existing hosts must rerun `sudo bash setup_scripts/setup_service.sh` once after updating EthOS so
  the root-owned account helper and its exact sudoers rule are installed.

## API

Every route requires a JWT. Mutations and account APIs require admin role.

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/shares` | User | List shares and effective access settings. |
| GET | `/api/shares/status` | User | Samba tools, service active/enabled, config path. |
| POST | `/api/shares` | Admin | Create share; supports legacy `valid_users`, masks, and structured ACL fields. |
| PUT | `/api/shares/{name}` | Admin | Update provided fields only. |
| DELETE | `/api/shares/{name}` | Admin | Remove a share; special sections are protected. |
| GET | `/api/shares/accounts` | Admin | Existing eligible Linux users/groups and passdb status; 503 when Samba/helper is unavailable. |
| PUT | `/api/shares/accounts/{username}/password` | Admin | Set or reset SMB password for an existing Linux user. |
| DELETE | `/api/shares/accounts/{username}` | Admin | Remove Samba credentials only; Linux account remains. |

The account helper accepts one JSON request on stdin and no command-line arguments. Only the fixed
`list`, `set_password`, and `delete` actions are accepted. Usernames are validated and checked
against host accounts; passwords are supplied to `smbpasswd` on stdin.

## Frontend wiring

Package Center loads the signed native entry `frontend/app-shell.js`. It uses `ctx.apiFetch`,
`ctx.pickFolder` restricted to `/home`, `/mnt`, `/media`, `ctx.modal`, `ctx.notifications.toast`,
and `ctx.log`. No package-local notification history or custom folder picker is used.

## Decisions & constraints

- Samba package installation/removal remains in `services/system_deps.py` and the existing
  `ethos-system-deps` helper. EthOS removes Samba only when EthOS installed it and no installed
  package still declares the dependency.
- `testparm` validates staged content. Failed reloads are errors and restore the prior config state.
- Structured read/write ACLs are constrained to existing non-system host users/groups; a principal
  cannot appear in both lists. Legacy `valid_users` expressions remain accepted when free of
  control characters and config-line injection.
- Guest access and explicit user/group ACLs cannot be combined because the guest principal would be
  excluded by `valid users`.
- No changes to Android clients or the per-file public share-link API.

## Status

- [x] Core account helper and narrow setup wiring
- [x] Admin-only account and share ACL API fields
- [x] Focused service and API regression tests
- [x] Native package shell and feature implementation
- [x] Full verification/security review
- [x] Package build and signature verification
- [x] UI workflow, theme, and narrow-layout checks

## Known gaps / next steps

- A real Samba host was not modified during tests; verify live passdb operations on a test NAS after
  rerunning `setup_scripts/setup_service.sh` to install the helper.