# OS package updates — Ubuntu apt from the web UI

Status: **shipped** (2026-11-22). Settings → System checks the Ubuntu archive and
installs the pending package upgrades through a narrow root helper. Reboots stay
a deliberate, manual action.

## Scope

Three independent update layers exist; this document covers the middle one.

| Layer | Mechanism | Rollback | Owner |
|---|---|---|---|
| EthOS application | signed git release + `deploy/ethos-update` | yes (previous commit, 90 s probe) | `ethos-nas` |
| **OS packages** | `apt-get update` + `apt-get upgrade --with-new-pkgs` | no — apt is not transactional | this feature |
| OS image / kernel layout | reflash from a signed `ethos-os-release` image | v1: no | `ethos-os-release` |

In scope: package upgrades **inside the current Ubuntu release** (kernel included,
it arrives through the `linux-image-generic` metapackage).

Out of scope, deliberately:

- `do-release-upgrade` / changing LTS release,
- snap refreshes (EthOS OS purges snapd),
- `dist-upgrade`, `autoremove`, `purge` (nothing is removed),
- automatic reboots,
- per-package selection (one action installs everything apt reports),
- managing apt sources or keyrings.

## Data model

Everything lives under `<DATA_DIR>/os-update/`:

| File | Writer | Shape |
|---|---|---|
| `check.json` | helper (`check`) | `{checked_at, checked_at_epoch, count, security_count, kept_back, packages[]}` |
| `job.json` | helper (`apply`) | `{status, step, percent, detail, upgraded, reboot_required, pid, at}` |
| `result.json` | helper (`apply`) | `{status, step, detail, upgraded, reboot_required, at}` |
| `apt.log` | helper (`apply`) | raw apt output, truncated at 2 MiB |
| `state.json` | backend (`json_store`, v1) | last error, attempt/check timestamps, package fingerprint, last reported result |

`packages[]` entries are `{name, from, to, origin, security}`. The helper caps the
list at 500 entries and every string at 200 characters; the backend re-bounds
everything it reads (`_coerce_packages`), because the root helper's output is
never trusted blindly.

`security` is derived from the apt origin suite (any origin containing
`-security`), not from a package-name heuristic.

A successful `apply` re-runs the simulation and rewrites `check.json`, so the UI
immediately shows only the packages apt kept back.

## Root helper — `deploy/ethos-os-update`

Installed as `/usr/local/bin/ethos-os-update` by
`setup_scripts/setup_service.sh`, reachable through **two argument-exact sudoers
rules**:

```
ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-os-update check
ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-os-update apply
```

Contract:

- exactly one argument, `check` or `apply`; anything else exits 2,
- conf `/etc/ethos-nas/os-update.conf` (`DATA_DIR`, `SERVICE_USER`) is root-owned;
  the web UI cannot redirect anything,
- `check`: `apt-get update -qq` then `apt-get -s --with-new-pkgs upgrade`; parses
  `Inst <pkg> [old] (new origin)` lines and writes `check.json` atomically
  (`fsync` + `os.replace` + chown to the service user),
- `apply`:
  - re-execs once through `systemd-run --scope --collect` (a package upgrade may
    restart `ethos-nas`; the scoped helper survives),
  - `flock -n /run/ethos-os-update.lock` — one operation at a time,
  - refuses below 512 MiB free on `/`,
  - `apt-get -y --with-new-pkgs upgrade` with
    `DEBIAN_FRONTEND=noninteractive`,
    `Dpkg::Options::=--force-confdef --force-confold` and
    `DPkg::Lock::Timeout=600` (waits out `unattended-upgrades`/`apt-daily`),
  - streams the output to `apt.log`, counts `Setting up …` lines for progress and
    rewrites `job.json` at most once a second,
  - writes `result.json` (`ok` / `failed` / `up_to_date`) plus the live
    `/run/reboot-required` state,
- exit codes: `0` ok, `2` usage, `3` busy, `4` apt failure, `6` no disk space.

`--with-new-pkgs` is what makes kernel updates work without `dist-upgrade`: new
binary packages are installed, but nothing is removed.

## API

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/system/os-update` | any JWT | cached result + live `reboot_required`; **always 200** (a missing helper is data, not an error) |
| GET | `/api/system/os-update/status` | any JWT | `job`/`result`/`running`/`stale` for polling |
| POST | `/api/system/os-update/check` | admin | runs the helper `check`; 409 when the helper is missing or busy |
| POST | `/api/system/os-update` | admin | spawns the helper `apply` detached; 409 without a cached non-empty check, or while a job runs |

`GET /api/system/os-update` response:

```json
{
  "enabled": true,
  "helper": true,
  "helper_path": "/usr/local/bin/ethos-os-update",
  "os": {"id": "ubuntu", "version": "26.04", "pretty": "Ubuntu 26.04 LTS",
         "codename": "resolute", "kernel": "6.14.0-…", "ethos_os": "0.1.2"},
  "auto_upgrades": "enabled",
  "checked_at": 1763800000,
  "checked_at_iso": "2026-11-22T08:00:00+00:00",
  "attempted_at_iso": "2026-11-22T08:00:00+00:00",
  "update_count": 12,
  "security_count": 3,
  "kept_back": 0,
  "packages": [{"name": "libc6", "from": "2.39-…", "to": "2.39-…",
                "origin": "resolute-security [amd64]", "security": true}],
  "reboot_required": false,
  "reboot_packages": [],
  "error": "",
  "check_hours": 24
}
```

`apply_status()` treats a `running` job as **stale** when the recorded `pid` is
gone (helper crash, host reboot) or the job is older than two hours; it then
synthesizes a `failed` result instead of blocking every later attempt with 409.
The core updater learned the same lesson the hard way (see `docs/updates.md`).

## UI — Settings → System

A new eighth rail section:

- identity card: `PRETTY_NAME`, kernel, EthOS OS version when present, whether
  `unattended-upgrades` is enabled, pending/security counts, cached error, and the
  admin-only **Check for updates** / **Install updates** buttons,
- package table (`Package · Installed · Available · Origin`, security tag,
  first 40 rows + “and N more”),
- **Reboot required** card with a danger **Restart now** button → existing
  `POST /api/system/shutdown {action:"reboot", confirm:true}` and
  `showRestartOverlay({waitTimeout: 240})`.

Progress uses `core/progress.js` (`startTask`, poll every 2 s), reattaches to a
running helper when the section is reopened, and never polls `apt` directly.
Notifications come from the backend (`emit_event`) and from the finished task.
Non-admins see the state read-only with an “Admin required” tag.

## Relationship to unattended-upgrades

Plain Ubuntu server installs ship `unattended-upgrades`
(`/etc/apt/apt.conf.d/20auto-upgrades`) and keep installing security updates in
the background. **This feature does not disable it** — the card shows its state
and installs everything that is left. EthOS OS images do not install
`unattended-upgrades`, so there the card is the only place updates happen.

Fresh lists matter: both the check and the apply run `apt-get update` as root
first. On a host where the nightly timer already refreshed them, the result is
the same.

## Background check and notifications

`os_update_service.start_os_update_checker()` starts from the app lifespan and
**sleeps first** (`ETHOS_OS_UPDATE_CHECK_HOURS`, default 24 h), so dev/test
instances never run apt. A newly seen package set emits one Notification Center
event:

- `Ubuntu updates available` — `dedupe_key=os-update:<fingerprint>`, action opens
  Settings,
- `Ubuntu updated` / `Ubuntu update failed` after an apply,
- `Reboot required` — `dedupe_key=os-update-reboot:<boot_id>` (one per boot).

## Config

| Env | Default | Meaning |
|---|---|---|
| `ETHOS_OS_UPDATE_ENABLED` | `true` | kill switch for check/apply |
| `ETHOS_OS_UPDATE_HELPER` | `/usr/local/bin/ethos-os-update` | helper path (tests point it at a fixture and skip sudo) |
| `ETHOS_OS_UPDATE_TTL_SECONDS` | `3600` | cached check freshness |
| `ETHOS_OS_UPDATE_CHECK_HOURS` | `24` | background cadence (sleeps first) |
| `ETHOS_OS_UPDATE_CHECK_TIMEOUT` | `180` | timeout for the helper `check` |

## Installing on an existing server

The helper and its two sudoers rules are written by
`setup_scripts/setup_service.sh`. A server updated from an older release must run
it **once**:

```bash
sudo bash setup_scripts/setup_service.sh
```

Until then Settings → System and `GET /api/system/doctor` show the
“helper not installed” hint, and `POST` answers 409 with the same message.
After that one-time setup, `deploy/ethos-update` refreshes the helper file on
every core update (the sudoers rules never change at runtime).

Since v2.10.0 the same setup also installs `ethos-provision`, which applies
`deploy/provision.json` on every core update — helper files, the separate
sudoers file, the unit and allowlisted confs. After that bootstrap, adding a
helper like this one no longer requires a manual setup run
(see [`provisioning.md`](provisioning.md)).

## Status

- [x] `deploy/ethos-os-update` (check/apply, scoped, locked, bounded)
- [x] Installer: helper, root-owned conf, two argument-exact sudoers rules
- [x] Core updater refreshes the installed OS helper
- [x] `services/os_update_service.py` (cache, TTL, guards, stale-pid, events)
- [x] `/api/system/os-update` (+ `/check`, `/status`), admin on mutations
- [x] Settings → System section with table, progress, reboot card
- [x] `showRestartOverlay({title, waitTimeout})` for full reboots
- [x] Doctor check `os-update-helper`
- [x] Tests: `test_os_update_helper.py`, `test_os_update_service.py`,
      `test_os_update_api.py`, security invariants, app-UI/settings guards
- [ ] Release upgrades (`do-release-upgrade`) — separate, high-risk feature
- [ ] Old-kernel cleanup (`autoremove`) — separate, explicit action
