# Ops runbook — EthOS NAS

Everything an operator needs to install, upgrade, restart, back up and diagnose the NAS.
Commands use `/opt/ethos-nas` for a fresh installation; adjust if your checkout differs.

Status: current as of 2026-11-13. Related: `docs/service-user.md`, `TODO.md` §6.

---

## 1. How it runs

| | |
|---|---|
| Service | systemd unit `ethos-nas` (installed by `setup_scripts/setup_service.sh`) |
| Unit file | `deploy/ethos-nas.service` (the only one that is installed) |
| User | dedicated `ethos-nas` system account, not your login user |
| Port | `9000` (`ETHOS_PORT`) |
| State | `backend/data/` (`ETHOS_DATA_DIR`) — 30 JSON stores, one SQLite index, caches, trash |
| Logs | journald (`journalctl -u ethos-nas`) + unified store `<DATA_DIR>/logs/events.jsonl` (Logs app; ring 5000, 5 × 1 MiB) |

The service user has **narrow NOPASSWD sudo rules** for `tee /etc/samba/smb.conf`, `systemctl
reload smbd`, `ufw`, USB partitioning, power commands and the Docker provisioning helper
(`/usr/local/bin/ethos-docker-install`, no arguments, fixed package list). That is why the unit must not use
`NoNewPrivileges=` or `ProtectSystem=strict` — see §8.

---

## 2. First installation

```bash
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/SyncHot/ethos-nas/master/setup_scripts/install.sh | sudo bash'
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:9000/api/system/status   # 401 = healthy
```

The quick installer is for Ubuntu Server 64-bit with Python 3.14 or newer. On
releases that ship an older interpreter (Ubuntu 24.04 has 3.12) it enables the
deadsnakes PPA and installs `python3.14`, `python3.14-dev` and `python3.14-venv`
before creating the venv. It installs the OS dependencies the backend shells out
to (archive tools, NTFS/XFS/Btrfs mkfs, nftables/ufw, cron), clones into
`/opt/ethos-nas`, creates a private JWT secret, and installs the dedicated
systemd service. It also installs **Docker
Engine + Compose v2** from Docker's official apt repository (fingerprint-pinned
key) and adds the service user to the `docker` group — set `ETHOS_SKIP_DOCKER=1`
to skip that and install Docker later from the Docker Manager app. It refuses to overwrite an
existing checkout, service unit, secret file, or unrelated listener on port
9000. It does not configure storage and never touches partitions. It does grant
the service user ACL access to the admin home's non-dot entries, because Files,
the code editor and the terminal run as that account; share ACLs are
left alone. Review the security warning and manual installation steps in the
repository [README](../README.md) before using `curl | sudo bash`.
The one-line form follows `master`; for a reproducible install clone a tagged
release and use `sudo bash /opt/ethos-nas/setup_scripts/install.sh --from-checkout`.

For an existing checkout, install the Python dependencies, create
`/etc/ethos-nas/ethos.env` with a strong `ETHOS_JWT_SECRET`, then run:

```bash
sudo env ETHOS_SHARES= bash setup_scripts/setup_service.sh
```

Re-running that command after an upgrade also refreshes the privileged helpers. Add
`ETHOS_INSTALL_DOCKER=1` to install Docker Engine + Compose v2 in the same pass (existing installs need
one re-run to get `/usr/local/bin/ethos-docker-install` before the Docker Manager can install Docker
from the UI). The script restarts `ethos-nas`, which is also what applies new `docker` group
membership. See [docker-manager.md](docker-manager.md).

Set `ETHOS_SHARES` to an existing share path only after deciding which storage
locations the service should access. Home ACLs are on by default; use
`ETHOS_GRANT_HOME_ACL=0` to keep the service account out of the admin home (File
Station will then return 403 for it).

`setup_service.sh --uninstall` removes the unit, the sudoers file and the USB helper (keeps the user).

---

## 3. Upgrade

**From the UI (recommended):** Settings → Updates → *Check for updates* → *Update now*. The backend takes
a data snapshot, then the root helper `ethos-update` verifies the signed release index, moves the
checkout to the signed commit, reinstalls dependencies, restarts the service and probes `:9000` for
90 s. A failure restores the previous commit automatically. Spec: [`updates.md`](updates.md).

**Ubuntu packages (apt):** Settings → System → *Check for updates* → *Install updates* installs the
pending package upgrades inside the current Ubuntu release through the root helper
`ethos-os-update` (`--with-new-pkgs`, no removals, no autoremove). A required reboot is shown as a
separate button — nothing restarts on its own. Spec: [`os-updates.md`](os-updates.md).

**Manually:**

```bash
cd /opt/ethos-nas
sudo tar czf /mnt/backups/ethos-data-$(date +%F).tgz backend/data   # see §5
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart ethos-nas
journalctl -u ethos-nas -n 50 --no-pager
```

The server migrates stores on first start (user store → `<DATA_DIR>/users.json`, `"version"` stamps).
Migration lines are logged at INFO.

### Restart without sudo

Settings → Restart issues `POST /api/system/restart` (admin only); the process re-execs in place and the
page reloads itself. Equivalent: `sudo systemctl restart ethos-nas`.

**Frontend files are served from disk**, so UI changes need only a browser reload (Ctrl+Shift+R if the
tab was open during the deploy). Backend changes always need a restart.

---

## 4. Rollback

```bash
cd /opt/ethos-nas
git log --oneline -10            # pick the last known-good commit
git checkout <sha>
.venv/bin/pip install -r requirements.txt
sudo systemctl restart ethos-nas
```

If a store was migrated forward, the downgrade guard refuses to use it and logs
`has version N but this build understands M` — restore the data snapshot from §5 instead of guessing.

---

## 5. Backup and restore of `backend/data`

This is the only irreplaceable state (photos index, albums, download tasks, kanban, notes, users/API keys,
private-photo vault, caches can be rebuilt). Recommended nightly:

```bash
# stop briefly so no JSON store is mid-write; SQLite is copied consistently
sudo systemctl stop ethos-nas
sudo tar czf /mnt/backups/ethos-data-$(date +%F).tgz -C /opt/ethos-nas backend/data
sudo systemctl start ethos-nas
```

Keep 7 daily + 4 weekly copies. Restore:

```bash
sudo systemctl stop ethos-nas
sudo tar xzf /mnt/backups/ethos-data-2026-11-10.tgz -C /opt/ethos-nas
sudo chown -R ethos-nas:ethos-nas backend/data
sudo systemctl start ethos-nas
```

The Backup app (`/api/backup`) protects *user folders*; it does not back up EthOS itself.

### 5.1 Online export/import (no stop needed)

For a snapshot you can take while the NAS is serving (also handy before an upgrade):

| Action | Call |
|---|---|
| Export | `GET /api/system/config-export` → `ethos-config-<UTC-stamp>.zip` |
| Import | `POST /api/system/config-import` (multipart field `file`) |

- The archive holds every `*-*-*.json` store under `ETHOS_DATA_DIR` plus a consistent copy of
  `gallery_index.db` (taken with SQLite's backup API, so WAL writers are safe) and a `manifest.json`
  (app version, timestamp, file sizes).
- **It contains secrets** (`users.json`: password hashes, API keys). Treat it like a password file —
  store it mode `0600` on encrypted media, never in git.
- Import validates the archive (flat `.json` names only, JSON must parse, the DB must be a real SQLite
  file) and **snapshots the current stores first** into
  `ETHOS_DATA_DIR/import-backup-<epoch>/`. A rejected archive changes nothing.
- JSON stores are replaced atomically through `json_store`; the SQLite index is restored through
  `backup()`, so no restart is required. Restart anyway if you imported while a long download or scan
  was running.

Both endpoints are admin-only (`require_admin`); every import is logged at WARNING level with the
store count and snapshot directory.

### 5.2 Automatic snapshots

The retention sweeper (`services/retention_service.py`, runs every
`ETHOS_RETENTION_INTERVAL`, default 6 h) also keeps a **daily state archive**: once the newest archive
is older than `ETHOS_CONFIG_SNAPSHOT_INTERVAL` (default 86400 s) it writes
`<DATA_DIR>/snapshots/ethos-config-<UTC-stamp>.zip` (mode `0600`) and prunes to the newest
`ETHOS_CONFIG_SNAPSHOT_KEEP` (default 7). Set `ETHOS_CONFIG_SNAPSHOT=0` to disable.

| Call | Purpose |
|---|---|
| `GET /api/system/config-snapshots` | list archives + `{enabled, keep, interval}` |
| `POST /api/system/config-snapshots` | take one now (503 if disabled or the write failed — see App Events) |

Restore from a snapshot with `config-import` (5.1) — it validates the archive and snapshots the live
stores before touching anything.

The offline `tar` recipe above is still the right choice for a *cold* backup: it also captures
`app_events.jsonl`, the recycle bin (`trash/`) and every cache directory, while the online archive
covers **configuration and catalog state only** (JSON stores + `gallery_index.db`). The daily zip is
the zero-effort safety net; the tar is the "everything" copy.

---

## 6. Logs and diagnostics

```bash
journalctl -u ethos-nas -f                 # live application log
journalctl -u ethos-nas --since "1 hour ago" -p warning
systemctl status ethos-nas
```

In the UI: **Logs** app → App Events (backend records at INFO+, 500-entry ring), plus System Files for
`/var/log`.

Journald is unbounded by default. Install the drop-in once:

```bash
sudo install -D -m 0644 deploy/journald-ethos.conf /etc/systemd/journald.conf.d/50-ethos.conf
sudo systemctl restart systemd-journald
```

that caps the journal at 500 MB / 90 days (`backend/data/app_events.jsonl` trims itself to the last
1000 lines). `GET /api/system/storage-usage` shows what the caches/trash consume;
`GET /api/health/status` shows disks, memory, temperatures, SMART and failed units.

`Settings → System` also shows the Health Monitor card (thresholds + Run check now).

---

## 7. Troubleshooting

| Symptom | First checks |
|---|---|
| UI loads, every API call 401 | Clock skew (tokens are 24 h), or `ETHOS_JWT_SECRET` changed → log in again |
| `502`/connection refused | `systemctl status ethos-nas`; port conflict: `ss -tlnp \| grep :9000` |
| "Permission denied" in File Manager | Service-user ACLs (`docs/service-user.md`); re-run `setup_scripts/setup_service.sh` |
| Samba changes fail with 403 | `sudo -n tee /etc/samba/smb.conf` must be allowed for `ethos-nas`; re-run setup script |
| Disk full alerts | `GET /api/system/storage-usage`; retention sweep logs at INFO when it frees space |
| Nothing after upgrade | `journalctl -u ethos-nas -n 100`; a store may be from a newer version (downgrade guard) |
| Port 9000 already in use | Only the unit may own :9000. `./start_server.sh` is a dev server on :9001 |

---

## 8. Why the unit is not hardened further

The service shells out to `sudo` for Samba, firewall, USB and power operations. `NoNewPrivileges=yes`
blocks the setuid escalation sudo relies on, and `ProtectSystem=strict`/`ProtectHome` would make
`sudo tee /etc/samba/smb.conf` and media writes fail. Any hardening change must therefore be tested on a
real box with a share edit and a media copy before it ships (TODO.md P5-01).

Safe additions that were considered: `PrivateTmp=yes` (transcodes already live in `DATA_DIR`),
`LimitNOFILE=65535`, `Restart=always` (already set).

### 8.1 Resource limits actually shipped

| Setting | Value | Why |
|---|---|---|
| `KillMode` / `TimeoutStopSec` | `mixed` / 30 s | SIGTERM reaches only the main process; children are stopped by the app's teardown, and a stuck connection cannot hold the unit past 30 s (see `docs/updates.md`) |
| `MemoryMin` | 512 M | the NAS stays resident while other units (VS Code, containers) reclaim |
| `MemoryHigh` | 1536 M | throttles reclaim; deliberately **no** `MemoryMax` (the kernel would kill this cgroup) |
| `ManagedOOMPreference` | `avoid` | `systemd-oomd` is enabled on Ubuntu and kills whole cgroups at 50 % pressure; `avoid` keeps the NAS out of the candidate set (the `ManagedOOM*` settings only accept `auto|kill`) |
| `OOMPolicy` | `stop` | after a real OOM kill the unit restarts deterministically (and `StartLimit*` stops a loop) |
| `StartLimitIntervalSec` / `StartLimitBurst` | 600 s / 10 | a broken release cannot restart-loop forever |
| `LimitNOFILE` | 65535 | WebSockets, streams, package builds |
| `MALLOC_ARENA_MAX` | 2 | 40+ threads × glibc arenas inflate RSS |

Inside the app: `services/pressure_guard.py` watches `/proc/pressure/memory`, MemAvailable and
swap; on **hard** pressure offline renders, video thumbnails, package installs, new downloads and
music batch jobs pause and the Notification Center says why. `services/child_limits.py` caps each
ffmpeg/ffprobe child (RLIMIT_AS: render 3 GiB, probe/camera 1 GiB, thumbnails 512 MiB) and nices
background work, so a runaway encode fails cleanly instead of becoming the kernel's OOM victim.

Checks:

```bash
systemctl show ethos-nas -p MemoryCurrent -p MemoryPeak -p ManagedOOMPreference -p Result
cat /proc/pressure/memory; grep -E 'MemAvailable|SwapFree' /proc/meminfo
journalctl -k -b | grep -iE 'oom|killed process'; journalctl -u systemd-oomd --since -1d
```

An unclean stop (killed or power loss) is reported once in the Notification Center at the next
start (`services/startup_guard.py`); a clean restart removes the marker and stays quiet.

### 8.2 Host memory tuning (zram + oomd) — optional, administrator-run

The app protects itself; the host needs two decisions of its own. On this class of box the disk
swap file (`/swap.img`, 4 GiB) was **100 % full** while `systemd-oomd` was active with Ubuntu's
default *kill at 50 % pressure* for `user@.service` — so a browser/editor session (and anything in
its cgroup) could be killed while RAM was still reclaimable.

```bash
sudo bash setup_scripts/setup_host_tuning.sh --dry-run   # preview
sudo bash setup_scripts/setup_host_tuning.sh             # install / update
sudo bash setup_scripts/setup_host_tuning.sh --revert    # undo
```

| Installed | What it does |
|---|---|
| `/etc/default/zramswap` (`deploy/host-tuning/zramswap`) | zram swap: 50 % of RAM, `zstd`, priority 100 — used before `/swap.img`, ~2–4× larger than the RAM it occupies, RAM-speed page-outs instead of disk stalls |
| `/etc/sysctl.d/99-zz-ethos-memory.conf` | `swappiness=150`, `page-cluster=0`, `watermark_boost_factor=0`, `min_free_kbytes≈1 %` — tuned for zram, not for a slow disk swap. The `zz-` prefix is deliberate: an older `99-swappiness.conf` (written for the disk swap file) would otherwise win, because systemd-sysctl applies files in filename order |
| `/etc/systemd/system/user@.service.d/10-ethos-oomd.conf` | raises oomd's user-session pressure limit 50 % → 80 %: still kills on genuine thrashing, not on a busy browser |
| package `zram-tools` | ships `zramswap.service`; no extra daemon |

This is a **per-host, manual** step like `setup_service.sh` — it is not part of EthOS provisioning
and adds no sudoers rule.

The script stops and resets `/dev/zram0` before starting the unit: `zram-tools`' postinst already
starts `zramswap.service` with its own defaults, and a plain `systemctl restart` then fails with
`Device or resource busy` while writing `comp_algorithm` — leaving the device on `lz4`. After the
run, `zramctl` must show `zstd`.

**Containers: limit swap too.** A container that only sets `mem_limit` may still fill the host swap
(docker defaults to `2 × mem_limit`); set `memswap_limit` equal to `mem_limit` so it restarts
instead of evicting the box. Applied example (`~/apps/compose/prowlarr/docker-compose.yaml`):

```yaml
    mem_limit: 3g
    memswap_limit: 3g
```

Verify:

```bash
swapon --show                 # /dev/zram0 PRIO 100 + /swap.img
zramctl                       # compression ratio / used
sysctl vm.swappiness vm.page-cluster
systemctl show user@1000.service -p ManagedOOMMemoryPressureLimit
docker inspect <name> --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}'
```

---

## 9. Secrets

- `ETHOS_JWT_SECRET` — **must** be stable across restarts. The installer generates one into
  `/etc/ethos-nas/ethos.env` (`openssl rand -hex 32`). If none is configured, the app generates a
  random secret once and persists it in `<DATA_DIR>/jwt_secret` (0600) — it never signs tokens with
  the public placeholder. `ETHOS_ALLOW_INSECURE=1` restores the placeholder (throwaway boxes only).
- Set it with `sudo systemctl edit ethos-nas`:

```ini
[Service]
Environment=ETHOS_JWT_SECRET=<long random string>
```

- Rotating it invalidates every issued token and API key instantly (expected; phones log in again).
- Test credentials live in `ETHOS_TEST_PASSWORD` only — nothing is committed.
