# Changelog

All notable, user-visible changes to EthOS NAS are documented here.

- Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
- Versioning: [SemVer](https://semver.org/) — one version is one `APP_VERSION` in
  `backend/config.py`, one git tag `v<APP_VERSION>` and one entry in the signed
  `releases/index.json`.
- The release ritual (bump → changelog → publish → tag) is mandatory for every push
  of runtime changes: skill `release-tag-changelog`.

## [Unreleased]

## [2.10.0] - 2026-11-22

### Added

- **Privileged provisioning from the signed release.** A core update now also
  applies the new root helpers, the exact sudoers rules declared in
  `deploy/provision.json`, the systemd unit and allowlisted
  `/etc/ethos-nas/*.conf` files. After one final
  `sudo bash setup_scripts/setup_service.sh`, releases no longer need a manual
  setup run. Spec: `docs/provisioning.md`.
- `GET /api/system/doctor` reports `system-provisioning` (last applied result)
  and `checkout-writable` (the backend must not be able to modify the checkout).

### Security

- The provisioner runs as root only from the updater (and the installer), takes
  no arguments, is not reachable through sudo, writes a **separate**
  `/etc/sudoers.d/ethos-nas-provided` (the installer's consolidated file is
  never edited), validates it with `visudo -cf`, and restores every replaced
  file on failure.
- It refuses a manifest or helper source owned by, world-writable, or
  group-writable by the service account — a writable checkout would otherwise
  let the backend plant a root helper for the next update. The installer warns
  and the doctor shows `checkout-writable`.

### Changed

- `deploy/ethos-update` now runs `provision` before the restart; the rollback
  path re-provisions the previous release. Old installs without the provisioner
  keep a guarded helper-refresh fallback with the one-time setup hint.

### Upgrade

- Run `sudo bash setup_scripts/setup_service.sh` once to install
  `ethos-provision`. If the doctor reports `checkout-writable`, remove the
  service user from the repository owner's group first
  (`sudo gpasswd -d ethos-nas <owner-group>`) — provisioning fails closed on a
  writable checkout.

## [2.9.0] - 2026-11-22

### Added

- **Download Manager: provider slots and tb7.pl.** A torrent debrid and a
  hoster-link multihoster are now independent slots (Settings → Providers), so
  magnets keep working while premium file-host links go through tb7.pl; tb7
  accounts get Verify with premium/transfer status and its supported-hoster
  list, and `GET /api/downloads/providers` exposes the provider catalogue
  without secrets. Requires the download-manager package 1.1.0.

### Changed

- **Overview / Network / Firewall split.** Overview now paints from the existing
  `/ws/stats` socket (slow HTTP fallback) and shows only the live network rate —
  the duplicate 30-minute chart moved out. Network is a single-pane, read-only
  view (interfaces, connection, throughput); its raw iptables tab and the
  `/api/network/firewall*` endpoints were removed, so Firewall & Access Control
  (UFW) is the only firewall surface. That app is now hidden from non-admin
  accounts, `ufw.service` cannot be started/stopped from Services or System
  Services (403, shown as Protected), and the repeated UFW status markup inside
  Security is shared. Also fixes the Network Interfaces view crashing on a
  missing import.

## [2.8.0] - 2026-11-22

### Added

- **Ubuntu package updates from the UI (Settings → System).** A new rail section
  shows the Ubuntu release and kernel, whether `unattended-upgrades` is active,
  every pending package upgrade (with security tags and the apt origin) and
  installs them with one admin action. Only the current Ubuntu release is
  upgraded: `do-release-upgrade`, snap and package removals are out of scope.
- **Reboot notice.** Kernel/system-library upgrades set `/run/reboot-required`;
  the section shows what needs the restart and offers an explicit **Restart now**
  button (the page waits for the server to stop, then for it to come back).
- **Background update check** (every 24 h, sleeps first) with Notification Center
  events for available updates, a finished install and a pending reboot.
- `GET /api/system/os-update`, `GET /api/system/os-update/status`,
  `POST /api/system/os-update/check` and `POST /api/system/os-update` (admin on
  the two mutations).

### Security

- apt runs only through the new root-owned `ethos-os-update` helper, reachable by
  **two argument-exact sudoers rules** (`check`, `apply`). The web UI can never
  pass a package name, version, URL or command; the helper locks itself, waits
  for the apt/dpkg lock, refuses below 512 MiB free on `/` and uses
  `--force-confdef/--force-confold` so prompts cannot block.
- `--with-new-pkgs` installs kernel updates without `dist-upgrade`: nothing is
  removed, `autoremove` and `purge` are never run.

### Upgrade

- Run `sudo bash setup_scripts/setup_service.sh` once on existing servers to
  install the `ethos-os-update` helper and its two sudoers rules. Until then
  Settings → System and `GET /api/system/doctor` show the setup hint and the
  mutating endpoints answer 409.

## [2.7.0] - 2026-11-22

### Added

- **User management (Settings → Users).** Admins can create a Linux account with
  a home directory and password, set its Samba password in the same step, change
  roles, reset passwords, disable/enable and delete accounts. Deletion can keep
  or remove the home directory and always offers removing the account's EthOS
  data (libraries, trash, wallpaper, favorites, download tasks, share links).
- **Self-service password change** from the topbar user menu
  (`POST /api/auth/password`, current password re-verified by PAM, at most five
  failed attempts per ten minutes).

### Security

- Account status and role are read from `users.json` on every request: disabling
  or deleting an account revokes its existing JWT and API key immediately, and a
  demoted admin loses admin rights without waiting for the 24 h token to expire.
- Deleted accounts keep a tombstone, so revocation survives a failed `userdel`
  retry; system accounts (`uid < 1000`) and the service account are untouchable.
- New accounts get an `ethos-nas` ACL on their home, so per-user file scope works
  from the first login.

### Upgrade

- Run `sudo bash setup_scripts/setup_service.sh` once on existing servers to
  install the new root-owned `ethos-user-accounts` helper. Until then account
  mutations answer 503 with that hint; listing keeps working.

## [2.6.8] - 2026-11-21

### Changed

- **First-run setup no longer asks for a setup code.** The default claim policy
  is now `open`: the wizard shows one form — admin username, password, repeat —
  and nothing to read off a screen. Whoever flashed the image is holding the box,
  so the code was friction without security value on a home network. The image
  ships `ETHOS_SETUP_CLAIM=open` in `/etc/ethos-os/firstboot.env` and the
  first-boot banner only prints a code when the policy asks for one. Set the
  policy to `local` (code only for proxy/public callers) or `code` (always) when
  the box will be reachable from outside before it is claimed — see
  `docs/first-run-setup.md`.

## [2.6.7] - 2026-11-20

### Security

- **Installed packages can no longer bind-mount another package's files** or the
  package store (`installed.json`, `sources.json`). A Compose package may bind
  only `/home`, `/mnt`, `/media` and its own package/data directories — even
  when `DATA_DIR` itself lives under `/home`.

## [2.6.6] - 2026-11-20

### Added

- **Per-user Downloads.** Each account has its own download directory, categories,
  watch folders, RSS feeds and extraction policy. Task lists, bulk actions and the
  qBittorrent API only show or change the caller's tasks.

### Changed

- Engine settings (concurrency, schedule, speed/package limits, debrid
  credentials) stay global and admin-only; the existing downloads config is
  adopted by the admin on upgrade, other accounts start with `<home>/Downloads`.

### Security

- A personal API key can no longer see, pause, delete or re-categorise another
  account's downloads through the qBittorrent-compatible API.

## [2.6.5] - 2026-11-20

### Added

- **Per-user libraries.** Gallery, Music, Videos and Code Editor state is now
  stored per account under `<DATA_DIR>/users/<user>/`: folder lists, private
  PIN, albums, media index, liked tracks/playlists, video resume positions and
  recent projects no longer leak between accounts. On upgrade the existing
  stores are adopted by the admin; every other account starts empty.

### Changed

- **Config backup/export** now includes every per-user store and each account's
  gallery index, and import restores them.

### Security

- A per-user gallery scan runs with the owning account's path scope; a scan
  scheduled from one account can no longer walk another account's folders.
- The music library no longer keeps every account's state in one document.

## [2.6.4] - 2026-11-20

### Security

- **Per-user filesystem scope.** Admins keep full access; every other account is
  limited to its own home plus `/mnt` and `/media` instead of every account in
  `/home`. The EthOS state directory (`<DATA_DIR>`, which stores `users.json` and
  every API key) is never user-facing, even when it lives under `/home`.
- **Media, share links and music settings enforce that scope.** `/api/videos`
  (stream, transcode, info, subtitles, thumbnails), `/api/gallery/meta`,
  `POST /api/share-links` and `POST /api/music/config` accepted paths outside the
  caller's roots. Share links store their creator and re-check the stored path on
  every public request; legacy links keep the old shared roots.
- **qBittorrent API identity.** The SID / API-key caller is resolved to a real
  account, so a personal API key keeps its own path scope; an absolute category
  `savePath` is sanitized instead of only being confined to the download dir.
- **Fail-closed path scope.** A router that forgets `scope_request` now denies
  instead of silently running with admin rights; background workers use
  `path_scope.system_scope()`.

### Fixed

- **Code Editor / Files picker no longer opens at `/`** for non-admins; it starts
  at the caller's first allowed root (admins still start at `/`).

### Added

- `docs/user-isolation.md`, `tests/test_path_scope.py` and an expanded
  `tests/test_security_invariants.py` matrix (user 403 / admin 200).

## [2.6.3] - 2026-11-20

### Fixed

- **The update card advertised an update forever on appliances built from an OS
  image**, even right after updating ("Update available: v2.6.2 · 2d45dff" while
  running exactly that). The image ships a `--depth 1` clone, where
  `git rev-list --count HEAD` returns 1, so the build comparison always saw the
  signed index (build 67) as newer. A shallow checkout now reports no build
  number and the check falls back to the commit sha the index pins.

## [2.6.2] - 2026-11-20

### Fixed

- **Updates failed on appliances built from an OS image** with "signed commit …
  is not present after fetch". The image clones EthOS with `--depth 1`, and a
  released commit is normally an ancestor of the new tip, so a shallow fetch
  moved the tip but never fetched that commit. `deploy/ethos-update` now fetches
  the exact commit, then deepens the clone (`--unshallow`, falling back to
  `--depth=200`) before giving up.

## [2.6.1] - 2026-11-20

### Fixed

- First-run setup: the wizard said "choose the password for *ethosadmin*" while
  the username field right below was editable; it now asks for the admin
  username and password.

## [2.6.0] - 2026-11-20

### Added

- **Pick your own username at first boot.** The setup wizard now has a username
  field, pre-filled with the image's `ethosadmin`. The root helper renames the
  account in place (`usermod -l` + home move + `groupmod`), so the uid, sudo
  groups and home contents survive and the appliance keeps exactly one admin.
  Names are validated against useradd's portable alphabet and refused if
  reserved or already taken.

## [2.5.0] - 2026-11-20

### Changed

- **First-run setup no longer demands a code on your own network.** The claim
  code still exists — without it the first browser to reach a fresh appliance
  could set its admin password — but the image now records a policy: with
  `local` (the default) anyone on the appliance's LAN claims with just a
  password, while requests arriving through a reverse proxy or from a public
  address still have to type the code printed on the screen. `code` enforces it
  everywhere, `open` drops it entirely. The wizard hides the field when it is
  not needed, and `GET /api/setup/status` reports `code_required` per caller.

## [2.4.1] - 2026-11-20

### Fixed

- First-run setup: the "EthOS is set up" notification was silently dropped —
  the claim handler imported the notification helper from a module name that
  does not exist, and the surrounding `try` swallowed the ImportError.

## [2.4.0] - 2026-11-20

### Added

- **First-run setup.** A freshly flashed EthOS OS appliance can be claimed in
  the browser: open `http://ethos-nas.local:9000`, type the setup code shown on
  the appliance's screen and choose the admin password. Until now the only way in
  was an HDMI keyboard and a randomly generated console password. The wizard
  signs you in immediately and the code stops working afterwards. Image side: a
  new `deploy/ethos-set-password` root helper does the `chpasswd` — the API stays
  unprivileged. See `docs/first-run-setup.md`.

### Security

- `/api/setup/status` and `/api/setup/claim` are public by necessity (a fresh
  appliance has no account). They answer only while
  `/etc/ethos-nas/setup.json` exists without `claimed_at`, are rate-limited to
  five failures per address per ten minutes, and never return or log the setup
  code. The helper refuses every account except the one recorded in that file.

## [2.3.15] - 2026-11-20

### Fixed

- Packages: installing again after a failed start no longer stops with “Package
  directory already exists”. A leftover directory the service user cannot delete is
  moved into `.backup/` and the install continues; uninstall and update rollbacks use
  the same recovery.

## [2.3.14] - 2026-11-20

### Fixed

- Files: video thumbnails are generated reliably again without hammering the CPU.
- Files: only one video stream runs at a time — thumbnail work yields to active
  playback, so playback no longer stutters.

## [2.3.13] - 2026-11-18

### Added

- Packages: **Update all** — every pending update runs sequentially, each with its
  own rollback, instead of one failed package blocking the rest.

Earlier releases were not maintained as a changelog; the notes for each version
live in `releases/index.json` and the full history in `git log`.
