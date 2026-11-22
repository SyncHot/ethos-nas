# Service User & Privilege Model — Plan (AI handoff)

Dedicated system user `ethos-nas` runs the server; all privileged operations go through
**one consolidated, narrow NOPASSWD sudoers file** installed by a single idempotent
installer. Goal: no per-feature setup scripts at runtime, and **no broad sudo**.

**This file is the resume point.** Read it first, check Status below, continue from the
first unchecked item. Update checkboxes + Verification Log as work lands.

## Decision (why this shape)

How comparable products do it:
- **NAS appliance vendors** — unprivileged web process; privileged ops via IPC to root daemons.
- **Proxmox VE** — `pveproxy` user; API → root helpers over permission-restricted Unix sockets.
- **Cockpit** — no standing privileges at all; polkit prompts per operation.
- **Portainer / CasaOS** — dedicated service identity + scoped privilege (docker group / sudoers).

Conclusion: a **dedicated service user = yes** (today the app runs as personal account
`marcin`, and sudoers rules are tied to that login — fragile, wrong identity).
**Broad `NOPASSWD: ALL` for it = no** (any RCE in FastAPI ≈ full root; ~20 routers spawn
subprocesses). The existing narrow-sudoers pattern is the right one — we just consolidate
three scripts into one installer and fix the gaps below.

### File access model (Files / code editor)

PAM login authenticates the *browser session*; it does **not** change who performs file
operations. Every Files / code-editor action runs as the **service user** (single
server process, one identity — the appliance/Portainer model). So "log in as the admin →
marcin's permissions" is achieved by **granting the service user access to the admin's
home**, not by running operations as the human:

- Installer step 2c: `setfacl -R -m u:<service>:rwx -dm u:<service>:rwx` on the admin
  home's **non-dot entries** (+ the home dir itself). Dot-dirs (`.ssh`, `.gnupg`,
  `.config`, `.local`, …) are excluded on purpose and stay private.
- Default ACLs make newly created files/dirs inherit the grant (owner umask ≤ 0002,
  the default). `UMask=0002` in the unit makes service-created files group-writable
  so the owner can also edit them.
- Admin home resolution: `ETHOS_ADMIN_USER` env → `$SUDO_USER` → first uid ≥ 1000.
- Permission failures return a 403 that names the service user, the path's
  owner/mode, and the fix (re-run the service installer, or an explicit
  `setfacl -R -m u:<service>:rwx -dm u:<service>:rwx <path>` grant) — see
  `files.py::_permission_denied`. Group `chmod` is never suggested: the service
  account is not in the owner's group and it would widen access for everyone.

Root-owned dirs (e.g. XDG dirs created by root processes) are fixed by re-running the
installer as root (root can setfacl them); docker-compose app data dirs under
`apps/` are intentionally left to the containers.

## Current state (verified)

- Server runs as `marcin` (`deploy/ethos-nas.service`, `start_server.sh`).
- Three separate sudoers files in `/etc/sudoers.d/`: `ethos-nas-shares`,
  `ethos-nas-firewall`, `ethos-nas-usb` (installed by three scripts).
- Gaps found:
  - `routers/system.py` power buttons call `sudo -n systemctl reboot|shutdown` — **no rule exists** for it.
  - Docker access falls back to `sudo -n docker` — no rule; should use **docker group membership** instead (standard, Portainer-style).
  - Terminal WS target `sudo` (`["sudo","/bin/bash","--login"]`) will not work for the service user — **accepted limitation**. Do NOT grant a shell. The UI disables the dead Sudo option for nologin service accounts and offers `user:<name>` targets instead (`su - <name>`, interactive PAM password in the PTY, no sudoers rule) — that is the sanctioned path to root from the UI terminal (log in as your own user, then `sudo -i` inside). The **default** terminal target is `host` (the service account — no password prompt); user targets are an explicit picker choice labelled `(password)`. The PTY is now a proper controlling terminal (`TIOCSCTTY`), so `su` sessions have working job control.

## Target rules inventory (single file `/etc/sudoers.d/ethos-nas`, user `ethos-nas`)

| Area | Rule (exact, no wildcards in args) |
|---|---|
| Shares | `tee /etc/samba/smb.conf`; `systemctl reload samba`; `systemctl reload smbd` |
| Linux accounts (new) | helper `/usr/local/bin/ethos-user-accounts` (argument-free rule). One JSON request on stdin (`create`, `set_password`, `lock`, `unlock`, `delete`); usernames validated, reserved/system accounts (`uid < 1000`) refused, passwords only on stdin. Creating an account also grants the service user an ACL on its new home (see [`user-management.md`](user-management.md)) |
| Firewall | `/usr/sbin/ufw` (any args); `journalctl` (any args, if installed); `cat` on `/var/log/auth.log`, `/var/log/kern.log`, `/var/log/syslog` + concrete rotated copies (including `.gz` — the app gunzips the bytes itself); `cat /etc/ufw/user.rules` and `user6.rules` |
| USB | `tee /etc/fstab`; helper `/usr/local/bin/ethos-usb-partition` (argument-free rule; validation lives inside the helper) |
| Power (new) | `systemctl reboot`; `systemctl shutdown` |
| Docker (new, group not sudo) | `usermod -aG docker ethos-nas` if `/var/run/docker.sock` exists |
| Docker provisioning (new) | helper `/usr/local/bin/ethos-docker-install` (argument-free rule; no parameters, fixed package list, fingerprint-pinned repo key — see [`docker-manager.md`](docker-manager.md)) |
| Host dependencies (new) | helper `/usr/local/bin/ethos-system-deps` — one exact rule per `(action, id)` pair generated from `SYSTEM_DEPS_IDS` (`install samba`, `remove samba`). The helper re-validates the id; no wildcard rule exists, so a manifest can never name an arbitrary apt package (see [`packages.md`](packages.md) → System requirements) |
| Log read (group not sudo) | `usermod -aG adm ethos-nas` — `kern.log` (UFW BLOCK events) and `auth.log` are `root:adm 0640`, so the Firewall app can read them directly; the `cat` rules above are the fallback |
| Host dependencies (new) | helper `/usr/local/bin/ethos-system-deps` — one exact rule per `(action, id)` pair generated from `SYSTEM_DEPS_IDS` (`install samba`, `remove samba`). The helper re-validates the id and no wildcard rule exists, so a manifest can never name an arbitrary apt package (see [`packages.md`](packages.md) → System requirements) |
| Core updates (new) | helper `/usr/local/bin/ethos-update` (argument-free rule). It fetches and verifies the signed release index with the pinned Ed25519 key (`/etc/ethos-nas/update-pubkey.pem`), then `git reset --hard` + `pip install` **as the repo owner**, restarts the unit and rolls back on a failed health probe. The web UI can only drop an `{"action":"apply"}` request file — it never supplies a URL, ref or command. See [`updates.md`](updates.md) |

## File map

| What | Where |
|---|---|
| New consolidated installer | `setup_scripts/setup_service.sh` (new; supersedes the 3 old scripts) |
| Old per-feature installers | `setup_scripts/setup_sudoers{,_firewall,_usb}.sh` — keep for reference, mark deprecated in header (or delete after E2E passes) |
| systemd unit | `deploy/ethos-nas.service` → `User=ethos-nas`, `Group=ethos-nas` |
| Root-start script | `start_server_root.sh` — **delete** (root run is strictly worse than narrow sudoers; git history keeps it) |
| Error hints in code | all strings referencing `setup_sudoers*.sh` → point to `setup_service.sh`: `services/shares_service.py`, `services/usb_mount_service.py`, `services/usb_partition_service.py`, `services/access_control_service.py` (`SUDO_HINT`), `routers/shares.py` (3×) |
| Docs | `AGENTS.md` — Commands table + Security invariants updated |

## Phases

### Phase 1 — Plan written (this doc)
- [x] Decision recorded, rules inventory complete

### Phase 2 — Consolidated installer `setup_scripts/setup_service.sh`
Single script, requires root (`sudo bash setup_scripts/setup_service.sh`), idempotent:
- [ ] Create system user: `useradd --system --shell /usr/sbin/nologin ethos-nas` (skip if exists)
- [ ] Ownership: **do not chown the whole repo** (git must keep working for `marcin`).
      Only chown runtime state: `chown -R ethos-nas:ethos-nas backend/data/`.
      Code/.venv stay world-readable. If E2E reveals more write locations, chown those too.
- [ ] NAS share ACLs (step 2b): named-user entries `u:<user>:rwx` + defaults on all
      dirs under `ETHOS_SHARES` (default `/mnt/SharedDrive`) — without these the
      service user cannot save into developer-owned shares (Downloads etc.).
      Directories only; non-fatal on ACL-unsupported filesystems.
- [x] Home ACLs (step 2c): `u:<service>:rwx` + defaults on the admin home's non-dot
      entries (Files / code editor access — see "File access model" above).
- [x] `UMask=0002` in the unit (service-created files group-writable).
- [ ] Install **one** sudoers file `/etc/sudoers.d/ethos-nas` with the full inventory above
      (reuse rule text from the 3 old scripts; resolve binary paths via `command -v`;
      visudo-validate a temp file before installing — never install unvalidated)
- [ ] Install USB helper wrapper `/usr/local/bin/ethos-usb-partition` (move logic out of `setup_sudoers_usb.sh`)
- [ ] Docker group: add user if socket exists
- [ ] Remove old per-feature files: `/etc/sudoers.d/ethos-nas-{shares,firewall,usb}`
- [ ] Install systemd unit from `deploy/ethos-nas.service` (User=ethos-nas), `systemctl daemon-reload && systemctl enable --now ethos-nas`
- [ ] Optional: `[user]` arg to target a different user (dev use); default `ethos-nas`
- [ ] Optional: `--uninstall` flag (remove unit, sudoers file, helper; keep user)

### Phase 3 — systemd unit + root script cleanup
- [ ] `deploy/ethos-nas.service`: `User=ethos-nas`, `Group=ethos-nas`; keep JWT-secret comment
- [ ] Delete `start_server_root.sh`
- [ ] `AGENTS.md`: update Commands (service mgmt: `systemctl restart ethos-nas`) + Security invariants

### Phase 4 — Code hint updates
- [ ] Replace all `setup_sudoers*.sh` references with `setup_service.sh` (file list above)
- [ ] No behavior changes — hints only

### Phase 5 — E2E verification (live instance :9000, running as ethos-nas)
- [x] Health check: `curl -sf http://localhost:9000/api/system/status` → 401 unauthenticated = up
- [x] Login via UI with PAM account; JWT works
      **E2E finding (live incident):** under the service user, PAM login failed for *every* user — `pam_unix` reads `/etc/shadow` (root:shadow 640) and `ethos-nas` was not in the shadow group (dev mode worked only because marcin is a shadow member). Fix: installer now adds `${TARGET_USER}` to the shadow group; after re-run + restart, real PAM login verified via API (`marcin` → 200, role admin, server-issued JWT works on /api/system/status). Bad creds still → clean 401.
- [x] Shares: create share → smb.conf written + samba reloaded — `sudo tee` write + `systemctl reload smbd` both logged in auth.log as user `ethos-nas`; journal shows "Reloaded smbd.service"; delete removes section.
- [x] Firewall: status, add rule, delete rule — verify in `/var/log/auth.log` sudo entries show user `ethos-nas`
      **Historical (2026-11-22):** the raw `/api/network/firewall` router, `services/firewall_service.py`
      and the `iptables`/`iptables-save` NOPASSWD rules were removed; UFW through the Firewall &
      Access Control app is the only firewall surface. The findings below describe that removed code.
      **E2E findings (all fixed this pass):**
      1. The /api/network/firewall router uses raw `iptables`, not ufw — no sudoers rule existed and there was no sudo fallback (pre-existing gap: broken for any non-root user). Fix: `services/firewall_service.py` prefixes `sudo -n` when euid≠0 (+ SUDO_HINT on auth failure); installer adds `/usr/sbin/iptables` + `/usr/sbin/iptables-save` NOPASSWD rules.
      2. `run_iptables_save()` ignored returncode → silently returned an empty ruleset as non-root (UI showed "no rules" while writes failed). Fix: checks rc, raises with hint.
      3. Rule ids were random per call (`uuid4` in both POST and parse_rule_line) → DELETE-by-id could never match (always 404; broken for every rule incl. pre-existing docker ones). Fix: id = md5(raw iptables-save line)[:8] — stable across calls; POST re-reads the ruleset and returns the new rule's real id.
      Verified live: add → list shows real ruleset (docker PREROUTING entries) → delete by returned id 200 → no leftovers. Attribution to ethos-nas proven: service runs as ethos-nas, iptables needs root (direct run = EPERM), API writes succeeded ⇒ NOPASSWD rule applied.
- [x] Docker app: container list works via group membership (no sudo fallback) — GET /api/containers → 200 with live containers; docker.py has no sudo path; `ethos-nas ∈ docker` group.
- [x] USB: mount/unmount a real device if available; else confirm graceful error text
      **E2E finding:** the wrapper's transport check failed for *partitions* (`lsblk -no TRAN /dev/sdc3` is empty — TRAN lives on the parent disk) → every partition mount/unmount was rejected with "not a USB device". Fixed in `setup_service.sh` heredoc: resolve PKNAME (with `/dev/` prefix) before checking TRAN; verified loop devices still rejected.
      Real-device cycle on /dev/sdc3 (235G ext4, data intact): unmount → clean umount + fstab entry removed; mount → remounted at /mnt/usb_sdf3 + `PARTUUID=… nofail` entry restored. auth.log shows all helper calls as user `ethos-nas`. sda/sdb (in-use NAS drives) untouched.
- [x] Power buttons: **do not actually reboot** — verified instead of triggering: rule `NOPASSWD: /usr/bin/systemctl reboot|shutdown` present in installed sudoers; code runs exactly `sudo -n systemctl <action>` (routers/system.py, confirm=true guard). Exact-match rule + exact command = accepted by sudo. No real reboot/shutdown executed.
- [x] Terminal WS `sudo` target: expect graceful failure message (accepted limitation) — PTY shows "sudo: I'm sorry ethos-nas. I'm afraid I can't do that"; no shell, clean refusal.
- [ ] Isolated test instance as `marcin` (`ETHOS_PORT=9001 ETHOS_DATA_DIR=/tmp/ethos-test-data ...`): privileged features degrade gracefully with clear hints; unprivileged features work
- [x] Git still works for `marcin` in the repo (no dubious-ownership errors) — backend/data owned by ethos-nas, repo top-level still marcin.

### Phase 6 — Docs, commit & push
- [ ] Update this doc's Verification Log
- [ ] Commit: `feat(deploy): dedicated ethos-nas service user + consolidated sudoers installer`
- [ ] Push

## Verification Log

| Date | Check | Result |
|---|---|---|
| — | plan written | ✅ |
| 2026-09-27 | installer run as root (user ethos-nas) | ✅ user created, backend/data + users.json chowned, sudoers installed (visudo-validated), USB helper installed, docker group added, legacy files removed, unit enabled+started |
| 2026-09-27 | service identity on :9000 | ✅ MainPID runs as ethos-nas under systemd; health 401 = up |
| 2026-09-27 | auth: no token / bad token / PAM login | ✅ 401 / 401 / 200 — after shadow-group fix, real PAM login (marcin→admin) with server-issued JWT |
| 2026-09-27 | **incident:** all PAM logins broken under service user | fixed: `usermod -aG shadow ethos-nas` in installer; root cause = pam_unix needs /etc/shadow (root:shadow 640) |
| 2026-09-27 | shares create→smb.conf→reload→delete | ✅ sudo tee + reload smbd as ethos-nas (auth.log), journal "Reloaded smbd.service" |
| 2026-09-27 | firewall status/add/list/delete as ethos-nas (historical — raw router removed 2026-11-22) | ✅ after fixes: sudo -n iptables fallback, rc check in iptables-save, stable md5 rule ids; add→delete-by-id cycle clean, real ruleset visible |
| 2026-09-27 | power rules effective for ethos-nas | ✅ `sudo -ln` as ethos-nas lists systemctl reboot/shutdown; the historical iptables NOPASSWD rules were removed 2026-11-22 |
| 2026-09-27 | docker container list via group | ✅ 200 with live containers, no sudo in code path |
| 2026-09-27 | USB real-device unmount/remount (sdc3) | ✅ after wrapper PKNAME fix; fstab managed block intact; auth.log shows ethos-nas |
| 2026-09-27 | power buttons (no actual reboot) | ✅ rule present + exact command match in code; not triggered by design |
| 2026-09-27 | terminal WS sudo target | ✅ graceful refusal message, no shell |
| 2026-09-27 | git as marcin | ✅ no dubious-ownership errors |
| 2026-09-27 | **incident:** legacy user-level unit (`~/.config/systemd/user/ethos-nas.service`, ran as marcin) won the :9000 port race at boot → system unit crash-looped (31+ restarts, EADDRINUSE, `Restart=always`/`RestartSec=3`) | fixed: `systemctl --user disable --now ethos-nas.service`; system unit bound :9000 on next cycle and ran stable as ethos-nas (health 401, zero bind errors since). Unit file left in place but disabled for rollback. Boot itself was ~40 s (firmware/POST 14.3 s dominant) — unrelated to the loop |
| 2026-10-20 | **incident:** file manager / code editor permission failures in user home directories (service user couldn't create in 751 homes, re-save 644 files, write into new subfolders) | fixed: home-ACL installer step 2c + `UMask=0002` + `_permission_denied()` 403 details (files.py) + frontend surfaces 403 detail (files.js). Live ACLs applied on the non-dot home tree (~238k entries, 20 s). E2E via API as the service user: mkdir in a user home 200 (was 403), nested mkdir/touch/editor-save/re-save all 200, root-owned dir still 403. Remaining for root: re-run installer to ACL the root-owned XDG dirs + pick up `UMask=0002` |

## rclone (offsite backups)

`setup_scripts/install.sh` installs `rclone`. Credentials live in rclone's own config and never pass
through the EthOS API or its JSON stores. Configure remotes as the service account:

```bash
sudo -u ethos-nas rclone config
```

The service account has `/usr/sbin/nologin`; if its home is not usable, point EthOS at an explicit
config with `ETHOS_RCLONE_CONFIG=/path/to/rclone.conf` (owned by `ethos-nas`, mode 0600). The Backup
Manager `GET /api/backup/remotes` lists remote names and `POST /api/backup/remotes/test` checks
connectivity only.
