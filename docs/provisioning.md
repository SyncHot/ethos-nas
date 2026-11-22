# Privileged provisioning — configuration from the signed release

Status: **shipped** (2026-11-22, v2.10.0). A core update now applies the new
root helpers, sudoers rules, systemd unit and allowlisted conf files from the
signed checkout itself. This is the part that makes an EthOS update behave like
an appliance firmware update instead of "code only, run the installer by hand".

## Why

Before this, `deploy/ethos-update` shipped **code** (git reset + pip + restart)
but privileged configuration stayed behind: new helpers such as
`ethos-user-accounts` (2.7.0) or `ethos-os-update` (2.8.0) required one manual
`sudo bash setup_scripts/setup_service.sh`. That is not how DSM/QTS behave —
their firmware update carries the privileged layer with it.

`deploy/provision.json` is that layer: a versioned manifest inside the same
signed commit the updater already verifies. `deploy/ethos-provision` validates
and applies it as root.

## Trust model

- The manifest and every source live in the **signed git commit**. A server only
  reaches that commit after `deploy/ethos-update` verified the Ed25519 index.
- `ethos-provision` takes **no arguments** and is **not reachable through sudo**.
  Only root callers use it: `deploy/ethos-update` and `setup_service.sh`.
- It **never edits** `/etc/sudoers.d/ethos-nas` (the installer's consolidated
  file). It owns a separate `/etc/sudoers.d/ethos-nas-provided`, written only
  after `visudo -cf` accepts it.
- **Ownership guard.** The manifest and every source must not be owned by,
  world-writable, or group-writable by the service account. A checkout writable
  by the backend would let the service plant a root helper for the next update —
  the helper fails closed instead. Fix with
  `sudo gpasswd -d ethos-nas <repo-owner-group>` (the doctor shows
  `checkout-writable` and the installer warns).
- Every replaced file is copied to `/var/lib/ethos-provision/<timestamp>/`
  first. Any validation or install error restores the backup and exits non-zero,
  which makes the updater roll the code back — and the rollback runs the
  provisioner again against the previous release.
- No shell is ever involved: values are validated in Python, files are written
  atomically, package installation and `apt` stay out of scope.

## Manifest — `deploy/provision.json`

```json
{
  "schema": 1,
  "helpers": [
    {"name": "ethos-os-update", "source": "deploy/ethos-os-update"}
  ],
  "unit": {"source": "deploy/ethos-nas.service"},
  "sudoers": [
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-os-update check"
  ],
  "confs": [
    {
      "destination": "/etc/ethos-nas/os-update.conf",
      "lines": ["DATA_DIR='{data_dir}'", "SERVICE_USER='{service_user}'"]
    }
  ]
}
```

Validation rules:

| Part | Rule |
|---|---|
| helper `name` | `^ethos-[a-z0-9-]{1,40}$`; `source` must be exactly `deploy/<name>` |
| source file | regular file under `deploy/`, no symlink, 1 B – 1 MiB, protection guard |
| destination | `/usr/local/bin/<name>` (helpers) |
| `sudoers` | each line must start with `<service-user> ALL=(root) NOPASSWD: /usr/local/bin/ethos-`; arguments are lowercase words; `* ? # , \` are rejected; max 200 rules |
| `unit` | only `deploy/ethos-nas.service`; `User=`/`Group=` templated to the service account, `__ETHOS_INSTALL_DIR__` to the checkout |
| `confs` | only `/etc/ethos-nas/<name>.conf`; `KEY='value'` lines; `$`, backticks, backslashes and quotes rejected; `{data_dir}`, `{service_user}`, `{install_dir}` are the only placeholders; `update.conf` is owned by setup and refused |
| manifest | `schema: 1`, unknown top-level keys rejected |

The manifest lists every helper the installer ships as a `deploy/` file
(`ethos-update`, `ethos-os-update`, `ethos-set-password`, `ethos-user-accounts`,
`ethos-samba-accounts`, `ethos-system-deps`, `ethos-provision`). Helpers that
`setup_service.sh` generates itself (`ethos-docker-install`,
`ethos-usb-partition`) stay installer-owned.

## Flow

Core update (`deploy/ethos-update`):

```
fetch → apply (git reset) → deps (pip) → provision → restart → verify
```

- `provision` installs changed helpers, confs, the unit (with `daemon-reload`)
  and the provided sudoers file.
- Any failure → `rollback`, which resets the code, re-provisions the previous
  release and restarts; the 90 s health probe still decides the final outcome.
- Old installations **without** `ethos-provision`: the updater falls back to its
  direct helper refresh, now guarded by the same ownership check, and logs the
  one-time setup hint.

Bootstrap: `sudo bash setup_scripts/setup_service.sh` installs
`/usr/local/bin/ethos-provision` and runs it once. From then on every core
update applies privileged configuration automatically — no manual step.

## State

| Path | Content |
|---|---|
| `/var/lib/ethos-provision/<timestamp>/` | backups of replaced files + `files.json` map (root-only) |
| `<DATA_DIR>/provision/result.json` | last result for the UI/doctor: `{status: ok\|noop\|failed, changed[], backup, at, detail}` |

`GET /api/system/doctor` reports `system-provisioning` and
`checkout-writable`. The helper's own log lines go to the updater job
(`journalctl -u ethos-nas` shows no secrets — there are none).

## Limits

- Not a full config-management system: only `/usr/local/bin/ethos-*`, one unit,
  and allowlisted confs under `/etc/ethos-nas/`.
- Sudoers rules from the manifest and the installer's consolidated file overlap
  on purpose: setup remains the bootstrap superset, the provided file is the
  update channel. Both are validated by `visudo`.
- The rollback path runs the **new** provision helper against the **old**
  manifest (schema 1 is stable). Keep the manifest schema backward-compatible.
- A release that needs an OS package, a new system user, or a system-wide config
  file outside `/etc/ethos-nas/` still needs the image/installer — that is the
  boundary to the A/B image work (roadmap Phase 3).

## Status

- [x] `deploy/provision.json` + strict validator/installer `deploy/ethos-provision`
- [x] Updater: provision before restart, re-provision on rollback, guarded fallback
- [x] Installer: install + run the provisioner, warn on a writable checkout, uninstall cleanup
- [x] Separate `/etc/sudoers.d/ethos-nas-provided` (installer file untouched)
- [x] Backup/restore, `visudo -cf`, atomic writes, idempotency
- [x] Ownership guard (service user cannot write the manifest/sources)
- [x] Doctor: `system-provisioning`, `checkout-writable`
- [x] Tests: `tests/test_provision_helper.py`, `tests/test_update_helper.py`
- [ ] True A/B whole-image updates (roadmap Phase 3 in `ethos-os-release`)
