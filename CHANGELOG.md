# Changelog

All notable, user-visible changes to EthOS NAS are documented here.

- Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
- Versioning: [SemVer](https://semver.org/) — one version is one `APP_VERSION` in
  `backend/config.py`, one git tag `v<APP_VERSION>` and one entry in the signed
  `releases/index.json`.
- The release ritual (bump → changelog → publish → tag) is mandatory for every push
  of runtime changes: skill `release-tag-changelog`.

## [Unreleased]

## [2.38.1] - 2026-11-24

### Fixed

- **Download Station extraction defaults**: the one-time switch to
  extract-and-remove can no longer overwrite an account that saves its extraction
  switches at the same moment the migration runs.

## [2.38.0] - 2026-11-24

### Changed

- **Download Station: archives unpack themselves by default**: a finished
  `.zip`, `.tar.*`, `.7z` or `.rar` download is extracted into a folder next to
  it and the source volume(s) are removed after a successful extraction. Both
  switches stay in Download Manager → Settings; accounts whose stored config had
  the old off defaults are switched on once, and a later manual opt-out sticks.
  Multi-part packages wait for every part, start at the first volume and keep
  their sources when a part is skipped, the archive is broken or
  password-protected, or the extractor produced no files. Split `.rar`/`.7z`
  volumes use the `archive-tools` host requirement, now installed as
  `libarchive-tools` + `7zip` (Ubuntu 26.04 no longer ships `p7zip-full`).

## [2.37.0] - 2026-11-24

### Added

- **Phone PWAs: downloads keep running in the background**: `/videos`,
  `/books` and `/music` hand a transfer to the browser (Chrome Background
  Fetch on Android), so it continues after switching to another app — even
  with the PWA closed — with a system progress notification. `sw.js` writes
  the finished file into the right on-device store and the app reconciles its
  download rows on the next start. iOS Safari, Firefox and a refused or
  stalled browser job fall back to the existing in-page resumable transfer.

## [2.36.1] - 2026-11-24

### Changed

- **Folder pickers for package data**: Settings → Packages and a package's
  Installed → Settings now have a **Choose…** button next to the path field — the
  shared file picker opens limited to `/home`, `/mnt` and `/media`, so the folder
  can be picked instead of typed. The path stays editable for power users.

## [2.36.0] - 2026-11-24

### Added

- **Settings → Packages → default data folder**: admins can choose the root folder
  for the data of *new* Docker-based packages (for example a folder on a data
  disk under `/mnt`). Every new package gets `<root>/<package>`; installed
  packages keep the folder recorded at install, so changing the default never
  moves existing data. Leave the field empty to keep the built-in
  package-named folder in the EthOS data store.

## [2.35.1] - 2026-11-24

### Fixed

- **Packages → data folder**: an image that runs as a fixed non-root user
  (Grafana 13 runs as uid 472) could not write to the folder EthOS creates, so
  the container restarted in a loop. A manifest can now declare
  `data.uid`/`data.gid`; EthOS grants exactly that user access with a POSIX ACL
  instead of making the folder world-writable, and re-applies it on install,
  update, a settings save, package start/restart and port repair. Grafana 1.3.0
  uses it.

## [2.35.0] - 2026-11-24

### Added

- **Packages → data folder**: a package can declare where its data lives and
  EthOS mounts a host folder there instead of a hidden Docker volume. The
  install dialog and **Installed → Settings** let you pick any folder under
  `/home`, `/mnt` or `/media` (default: a package-named folder in the EthOS data
  store), so app data can live on a data disk and be handled by Files. Changing
  the folder recreates the service and does not move files; uninstall's “delete
  package data” honours the chosen folder. Host paths and passwords stay out of
  the general package list. Grafana 1.2.0 keeps its database and dashboards
  there.

## [2.34.1] - 2026-11-24

### Fixed

- **Packages → install dialog**: the manifest is nested in the catalog detail
  response, so the new install-time settings fields (and the system-requirement
  hints) were not rendered; the dialog now flattens the manifest first.

## [2.34.0] - 2026-11-24

### Added

- **Packages → install settings**: a package can declare install-time fields
  (an administrator login, a password, a hostname). Package Center shows them in
  the install dialog and under **Installed → Settings**; EthOS keeps the values
  with the package record and passes them to the container as environment
  variables through the signed compose file. Passwords are never returned by the
  API, and saving a value recreates the service so it applies immediately.
  Grafana 1.1.0 uses this for its administrator account instead of the built-in
  `admin/admin`, and InfluxDB-style apps can now offer a real first-run setup.

## [2.33.0] - 2026-11-23

### Changed

- **Files → Trash**: deleting from a data disk no longer copies the file onto
  the system drive. An item on another filesystem now moves into a hidden
  `<mount>/.@Recycle/<user>` bin with a plain rename — instant and no SSD
  space — while the Trash view, restore, retention, usage and the desktop badge
  keep working across every volume. `.@Recycle` is hidden from the file list,
  search and backups; `ETHOS_TRASH_VOLUME_LOCAL=0` restores the old behaviour.

### Fixed

- **Files**: “Move to Trash” on `/mnt` or `/media` could take minutes (and
  double the space) for a multi-GB file, because the item was copied byte by
  byte into `DATA_DIR/trash` on the system SSD. The trash operation also skips
  its size pre-walk when every path renames into a volume bin.

## [2.32.0] - 2026-11-23

### Changed

- **Files**: video thumbnails are always the plain 320 px frame. The grid asks
  the backend for `face=0`, so File Manager no longer runs (or waits on) the
  media-enhance face-detection pass; the plain frame also has its own cache
  slot, so it never competes with Photos' face-aware crop.

### Removed

- **Files**: the "cinematic / compact video preview" toggle and its enlarged
  (up to 507×285, 640 px source) grid mode were removed — video tiles keep the
  standard 16:9 frame. Photos still uses face-aware thumbnails.

## [2.31.4] - 2026-11-23

### Added

- **Settings → Updates, Browser extension**: a card for the EthOS NAS Downloader
  (Chrome, Edge, Brave, Vivaldi) with a Download button. It prefers the zip
  deployed on the NAS (`apks/extension/`), showing its exact version and serving
  it through the tokenized `/api/app/download?app=extension`; without a deployed
  package it falls back to the GitHub release asset. `GET
  /api/app/version?app=extension` advertises the package (version from the
  filename, sha256, size); `ETHOS_EXTENSION_ZIP_PATH` overrides the location.

## [2.31.3] - 2026-11-23

### Added

- **Settings → Updates**: a **Browser extension** card downloads the latest
  EthOS NAS Downloader zip (Chrome, Edge, Brave, Vivaldi) with the load-unpacked
  steps. The stable `ethos-nas-downloader.zip` comes from the extension's GitHub
  releases (override with `ETHOS_EXTENSION_DOWNLOAD_URL`); every account can
  download it.

## [2.31.2] - 2026-11-23

### Added

- **Settings → Updates**: the EthOS NAS card has a **Download** button for the
  published release (falling back to the running version) — the tagged source
  archive from GitHub. Every account sees the download; *Check for updates* and
  *Update now* stay admin-only.

## [2.31.1] - 2026-11-23

### Fixed

- **Download Manager categories**: a category whose save path is left blank now
  downloads into `<download-dir>/<category>` as the settings editor documents,
  and `.torrent` uploads keep the chosen category. The browser extension's
  right-click *Send link to EthOS NAS* now uses the category picked in its popup
  (it is saved as the send default), so VR/TV/Movies selections land in the right
  folder.
- **Video Station on Intel N100/N150**: QSV transcoding uses the low-power VDEnc
  path, scales with `nv12` and no longer sets a pixel format that oneVPL rejects,
  so hardware encoding works on VDEnc-only iGPUs.

## [2.31.0] - 2026-11-22

### Added

- **Disk Cleaner 2.0 backend**: scans run as background jobs with live entry counts, a
  cooperative Cancel and a partial-result flag instead of a 120 s timeout; `/suggestions`
  computes each junk root in one pass and caches the sweep for two minutes; new
  `GET /api/disk-clean/history`, `POST /api/disk-clean/caches/clean` and an optional
  `{areas:[…]}` body on `POST /api/system/retention/sweep`.
- **Recoverable deletes**: `POST /api/disk-clean/delete` accepts `mode:"trash"` (per-user
  recycle bin, restore from Files) and `async` for byte-level progress through
  `/api/files/ops`; the legacy synchronous response is unchanged for 1.0.x clients.
- **Protected-tree policy**: the cleaner now also refuses `<DATA_DIR>`, the EthOS checkout,
  `/var/lib/docker`, `/etc/ethos-nas` and any ancestor of a protected path, and mounts
  `path_scope.scope_request` on every route.

### Changed

- **Shared trash/permanent deletion workers**: the cleaner hands deletes to
  `files_service.start_trash_operation` / `start_hard_delete_operation`, so chunked progress,
  per-path failure reporting and the deletion audit are the File Manager's.

### Tests

- New `tests/test_disk_clean.py` (41 checks: scan jobs, cancel, suggestion cache/summary,
  protected paths, trash + restore, permanent delete, cache purge, history, area sweep).

## [2.30.3] - 2026-11-22

### Added

- **`GET /api/music/state` is now a modelled response** (`MusicLibraryState`): the five
  top-level sections of the per-user library document (`liked`, `playlists`, `history`,
  `podcasts`, `radio`) are documented in `/docs`, track dicts stay free-form and unknown
  extra sections are preserved. Same JSON on the wire.

## [2.30.2] - 2026-11-22

### Changed

- **Download Station RSS scanner moved into `services/download_rss.py`**: the bounded
  http(s) fetch, the persisted 5000-entry seen set, the per-feed interval gate and the
  whole `scan()` (rule matching, `.torrent` → magnet hop, per-user identity scope) left
  `routers/downloads.py`. The router supplies the config loader, the `AddTaskRequest`
  enqueue path and the bencode parser, and keeps the one-minute loop; same
  `/api/downloads/*` behaviour.

### Tests

- New `tests/test_download_rss_service.py` (16 checks: scheme/cap guards, bounded seen
  set, interval gating, forced rescan dedupe, feed-error isolation, feed filter).

## [2.30.1] - 2026-11-22

### Changed

- **Private vault moved out of the Gallery router**: PIN derivation (PBKDF2-SHA256,
  600k iterations), the AES-256-GCM at-rest format, the 30-minute `ps` session registry,
  plaintext temp files and the decrypt-in-memory private thumbnails now live in
  `services/gallery_private.py`. `routers/gallery.py` keeps only the request-bound glue
  (JWT decode, PIN config lookup, index lookup) plus the endpoints — same paths, same
  responses, `gallery.py` 2310 → 2176 lines.

### Tests

- New `tests/test_gallery_private.py` (37 checks: PIN derivation, header/wrong-key
  behaviour, file round-trip, PIN re-key, session TTL and user binding, 0600 temp files,
  in-memory private thumbnails). PIN set → verify → change verified live on an isolated
  instance (200/200/200, old PIN 403).

## [2.30.0] - 2026-11-22

### Changed

- **Media pipeline moved out of the Files router**: the thumbnail cache rule (source
  mtime + size sidecar), video master-frame/slot pipeline, custom covers, the Haar face
  crop, image thumbnails and the H.264 transcode worker now live in
  `services/media_cache.py`, `services/thumbnails.py` and `services/transcode.py`.
  `routers/files.py` keeps the HTTP endpoints (same paths, same responses) and
  `gallery.py` / `share_links.py` import the thumbnail service directly instead of
  reaching into the router. `files.py` 2133 → 1362 lines. No behaviour, cache-path or
  API change.

### Tests

- New `tests/test_media_cache_transcode.py` (27 checks: freshness, orphan sweep,
  transcode job lifecycle); `tests/test_video_thumb_pipeline.py` now targets the
  service.

## [2.29.9] - 2026-11-22

### Changed

- **Download URL/path guards moved into `services/download_urls.py`**: premium-hoster
  detection, the default file name, scheme validation, the private-IP block list (SSRF guard),
  `safe_join` (torrent-metadata and symlink escapes) and `unique_dest` are now pure,
  unit-tested helpers (`tests/test_download_urls.py`). No behaviour or API change.

### Fixed

- **Updates: *Update now* no longer does nothing.** The 2.29.4 helper rewrite
  dropped its state paths, so the helper exited before writing any result and the
  card kept offering the same version; the paths are restored and a guard now
  fails the build when a helper variable is undefined (4d450a2).

## [2.29.7] - 2026-11-22

### Added

- **Downloads: a second link provider now works as a fallback.** The slot in
  Settings → Providers is a preference, not the only option: when tb7.pl cannot unlock a
  RapidGator/hoster link, the engine immediately retries with the AllDebrid account already
  stored (which unlocks any host). A host tb7.pl does not list is routed straight to
  AllDebrid, and resetting a failed link re-tries the whole chain — the task then shows
  which provider actually fetched it.
- **Downloads: a premium hoster page is never saved as HTML.** Pasting a RapidGator (or
  other file-host) link without a provider that can unlock it now answers 400 with the
  configured providers instead of downloading the landing page.

### Fixed

- **Progress panel: × stays dismissed.** A progress tick re-opened the panel every
  250 ms and an empty queue showed a blank panel — both are fixed (b350d62).
- **Videos: offline downloads resume** after a reload, a locked screen or a dropped
  connection instead of restarting a multi-GB file from zero (72c0efb).
- **Music: offline downloads resume** from the last stored byte, with parts assembled only
  after the final part lands (6ef6425).

## [2.29.6] - 2026-11-22

### Changed

- **Queue order and retry timing moved into `services/download_engine.py`**: which waiting
  task starts next (priority, then FIFO) and when the retry timer should fire are now pure,
  unit-tested helpers (`tests/test_download_engine_queue.py`). No behaviour or API change.

## [2.29.5] - 2026-11-22

### Changed

- **The watch-folder scan pass moved into `services/watch_folders.py`**: torrent iteration,
  bounded infohash/filename dedupe and the move into `.processed/` (unique names on collision)
  are now unit-tested (`tests/test_watch_folders.py`); the router still owns the scanner
  thread, the per-user path scope and task persistence. No behaviour or API change.

## [2.29.4] - 2026-11-22

### Changed

- **Download engine policy moved into services**: the daily transfer window (`in_window`,
  `seconds_until_window`, overnight windows included) and the retry decision/backoff
  (`is_retryable`, `retry_delay`) now live in `services/download_schedule.py` and
  `services/download_retry.py`, covered by `tests/test_download_schedule_retry.py`.
  Behaviour is unchanged — this is the next slice of the `downloads.py` split.

## [2.29.3] - 2026-11-22

### Fixed

- **Uninstalling a window-only package can now delete its configuration.** The Cameras
  package (`ethos-surveillance`) is only the UI: cameras, settings, motion events and
  the live window live in the core, so removing the package left them behind and the
  recording engine kept restarting ffmpeg for cameras nobody could edit any more
  (`Camera … live failed: Connection refused` in the log, forever). Uninstall now offers
  "Delete package data and configuration" (pre-ticked when the package has no data root
  of its own), stops the pipelines and deletes the camera state; recordings are kept.
- **Leftover core state appears in Packages → Unused package data**, so a package that
  was already removed can still be cleaned up (`DELETE /api/packages/orphans/{id}` now
  purges the core state as well).

## [2.29.2] - 2026-11-22

### Fixed

- **Invalid request bodies are rejected with a 422 that names the field**, instead of a
  silently ignored key. Covered: Music (download, batch download, config, state import,
  radio state, liked/playlist/history tracks, podcast subscribe), Video Station
  (config, resume, metadata refresh), Gallery folders/options and the download settings
  (partial config stays partial). Every payload the bundled Music, Photos and Video apps
  send keeps working unchanged.

### Changed

- **File search moved out of the router** into `services/search_service.py` (name walk,
  skip list, type/size filters, real-mount discovery, default scopes, result merge), with
  `tests/test_search_service.py`. First slice of the `files.py` split; no API change.

## [2.29.1] - 2026-11-22

### Fixed

- **Settings → Updates no longer hangs at “fetch” with no progress.** On a
  freshly imaged appliance the root helper aborted before writing any result:
  `/etc/ethos-nas/update.conf` named the build account as the repo owner, which
  does not exist once the image is claimed by the admin created at first boot
  (`runuser: user ethosadmin does not exist`). The helper now resolves the
  owner from `INSTALL_DIR`, reports every exit path through an `EXIT` trap, and
  records its PID so the backend can tell a dead helper from a slow one; the
  backend finalizes a dead job as a failure (with the step and a journalctl
  hint) instead of showing a spinner for two hours. Installs that already ship
  a single-tag clone refspec also fetch branches and tags explicitly, and no
  git command can block on a credential prompt.

## [2.29.0] - 2026-11-22

### Added

- **Ctrl+K command palette** covering every app and every Settings section — 18 windows
  and nine settings panes are too many for a desktop grid.
- **Service Manager absorbs System Services**: four tabs (All · Containers · Systemd ·
  EthOS NAS), a **Logs** button on every row that opens the one Logs viewer on that unit or
  container, and the EthOS unit card with its restart/stopping/boot controls. Old
  `openApp('systemd')` links and pinned icons migrate to the merged window.
- **SMART disk details**: the full attribute table with changes since the previous sample,
  a 24 h temperature chart, drive self-tests (short / long / abort) and the drive's own
  self-test log. History is recorded by the health monitor (`smart_history.json` v2) and
  read through `GET /api/smart/history` — the request path never probes a disk.
- **Cron schedules are validated and previewed**: real 5-field/`@macro` parsing, a human
  description and the next run per job, and a live “next three runs” preview while typing
  (`GET /api/cron/preview`). A typo can no longer be saved as a job that never fires.
- **Process Manager details**: full command line, PPID, container and start time, an
  explicit SIGKILL next to SIGTERM, and a jump to a container's logs.
- **Network per-interface rates**: live RX/TX every 5 s, error/drop totals, copy buttons for
  IP and MAC, and a Firewall link for admins.
- **Universal Search** keeps recent queries, sorts by newest/name/size and opens the
  containing folder in Files.
- **Overview health alerts**: failed systemd units, near-full disks, SMART failures and high
  temperatures appear on the dashboard with a jump to the app that fixes them; memory now
  shows swap, and every card drills into its app.

### Changed

- Core apps hand paths and sections to each other through `core/app-links.js` instead of
  parking them on `window.*`; package apps keep their documented `__fm*Ctx` contract.

### Fixed

- SMART no longer replaces a healthy disk list with “Could not read SMART data” (a
  block-scoped variable was read outside its scope).
- Storage and Shared Links stop printing “GB GB” on sizes; both use the shared formatter.

## [2.28.8] - 2026-11-22

### Fixed

- **"Camera storage almost full — 0.0 GiB of 0.0 GiB used" no longer appears
  every five minutes.** The retention janitor compared the recordings against a
  budget that already *includes* them, so on a full volume the check was always
  true — and it ran even with no cameras left, which is why the warning kept
  coming after the cameras were removed. The janitor now stops when no camera is
  configured, needs at least 256 MiB of recordings, re-alerts at most every six
  hours and says what actually happened (“X GiB recorded · volume 98 % full — no
  room left for new recordings”).
- **An unreachable camera no longer spawns ffmpeg every two seconds.** A dead
  live/archive process was replaced before the failure was charged, so the
  5/15/60 s backoff never applied and every tick left a new recording directory
  behind (the real reason that volume filled up).

## [2.28.7] - 2026-11-22

### Added

- **Sharing → Shared by me can start a new share** without leaving the app: “Share
  folder…” and “Share file…” open the same picker and recipient dialog the Files
  context menu uses, so a view-only grant no longer requires finding the item in
  Files, Photos or Music first.
- **Storage has a Refresh button on every tab** (Disks, Partitions, USB), and the
  **SMART Disk Health** window refreshes itself every 60 s with a “checked · updated
  HH:MM” line, so temperatures and power-on hours stay current.
- **Shared Links shows the public URL on each row**; Copy now confirms (“Copied”) and
  the URL is visible for copying by hand when the clipboard API is unavailable.

### Fixed

- Windows that failed to load (Storage tabs, SMART, System Services) now offer a
  working **Retry** instead of a dead error card.
- Overview, Storage and SMART use the shared byte / rate / uptime formatters, and
  Shared Links lost the “GB GB” suffix on device sizes.
- Shared Links has its own desktop icon glyph and gradient instead of the generic
  fallback, and the icon set no longer carries a duplicate key.

### Changed

- The Terminal status dots and error text use theme tokens, so they switch with
  light/dark instead of staying on dark-only hex colours.

## [2.28.6] - 2026-11-22

### Added

- **Package Center shows when a package runs on a re-mapped port** ("port 3000
  was already in use, so this package listens on 3124") and offers a **Repair
  ports** action (`POST /api/packages/<id>/repair-ports`). It matters after a
  boot in which another compose project grabbed the port first: the package
  container could no longer bind and sat dead. Repair re-plans the mapping,
  rewrites the generated compose override and recreates the service, and the
  catalog now reports the effective `ui.port` instead of the manifest's
  original one.

## [2.28.5] - 2026-11-22

### Fixed

- **A package whose port is already taken now installs anyway.** Many hosts run
  another compose project (or a dev server) on the port a package asks for, and a
  signed compose file cannot be edited. EthOS now publishes the package on the next
  free port: it writes a compose override with `ports: !override`, stores the
  effective port in the package record and everything — the window, the package
  proxy, health probes — follows. The remapped port stays the same across updates,
  so a bookmarked URL keeps working. Venv/native packages cannot be remapped and
  still report “Port 3000 is already used by <container>”, because EthOS starts
  those processes itself.

## [2.28.4] - 2026-11-22
 - 2026-11-22

### Fixed

- **`install.sh` can install inside an image build chroot.** The port-9000 guard
  refused to continue when a NAS was already listening on the build host — the
  chroot shares its network namespace, so the check was always a false positive
  (the arm64/rpi image build died on it). `ETHOS_SKIP_PORT_CHECK=1` skips the
  guard; `ethos-os-release` sets it for the build chroot.

## [2.28.3] - 2026-11-22

### Fixed

- **A stopped package service no longer shows a Python error inside the window.**
  When the loopback service behind a native package was down, the authenticated proxy
  answered with the raw exception (`<urlopen error [Errno 111] Connection refused>`) and
  the app printed it verbatim. It now answers “The <package> service is not responding.
  Start it in Package Center and try again.”, while the real cause still goes to the Logs
  app (`tests/test_package_proxy_error.py`).

### Added

- **A cross-package QA sweep.** `scripts/ethos-qa` audits the whole catalog read-only —
  icons, port ownership (including a live `docker ps` collision check), `min_ethos_version`,
  native-module self-containment, the notification/logging/window-title rules, palette
  scheme, test hooks and catalog copy — and `tests/test_packages_qa_ui.py` opens every
  native package window on a throwaway instance in light and dark, failing on page errors.
  Findings and the repeatable loop: `docs/packages-qa.md`.

## [2.28.2] - 2026-11-22

### Fixed

- **A package install that cannot get its port now says so up front.** Package
  Center binds the package's `ui.port`/`healthcheck.port` before downloading
  anything, so a host that already runs something on that port (another compose
  project, a dev server) no longer ends with the raw
  “Bind for 127.0.0.1:3000 failed: port is already allocated” after the files
  were deployed — it reports `Port 3000 is already used by <container>`.
  Updates are unaffected: the running package owns its own port.

### Changed

- Forgejo (`ethos-git` 1.0.1) publishes its web UI on host port **3005** instead
  of 3000, which collides with common compose projects.

## [2.28.1] - 2026-11-22

### Fixed

- **Package policy: no more false share-write warnings.** A relative volume source (a named volume
  such as `clamav-db:/var/lib/clamav`) was resolved against the core's working directory, which
  normally lives under `/home`, and reported as a read-write share bind without `PUID`/`PGID`. Only
  absolute host paths can be a share bind, so the warning now fires just for those — and the whole
  25-package catalog validates without a single warning.

## [2.28.0] - 2026-11-22

### Added

- **Package Center: container health.** `GET /api/packages/{id}/status` now returns the containers and
  an aggregate `health` from `docker compose ps`, the Installed table shows it next to the project
  status, and a background monitor publishes one warning when a container turns unhealthy or enters
  a restart loop (and one info when it recovers) — a crash loop no longer looks “running”.
- **Package Center: Stable/Beta channel filter.** The catalog carries a `channel`; the window now has
  a channel selector, so the `beta` channel is reachable instead of being a dead API parameter.
- **Explicit consent for unauthenticated ports.** A package whose manifest declares
  `ui.expose: "lan"` with `ui.auth: "none"` cannot be installed or updated until the operator ticks
  “this port has no login” in the confirmation dialog.
- **Catalog: Uptime Monitor (`ethos-monitor`, Uptime Kuma 2.5.12) and FreshRSS (`ethos-rss` 1.30.4).**

### Changed

- **Compose packages run as the EthOS account when they write to shares.** Every `docker compose`
  call exports `ETHOS_SERVICE_UID`/`ETHOS_SERVICE_GID`, manifests can use
  `PUID: "${ETHOS_SERVICE_UID:-1000}"`, and the compose policy warns when a read-write `/mnt` or
  `/media` bind has neither `PUID`/`PGID` nor `user:`. Syncthing uses it, so synced files stay
  editable by the NAS and its users.
- **The install/update disk preflight checks Docker's root too.** Images land in
  `Docker Root Dir`, not in the package store, so a full `/` is now caught before a pull instead of
  after (`ETHOS_PACKAGES_DOCKER_ROOT` overrides the detected path).

### Fixed

- **Domains:** the reverse proxy and its control plane now report container health instead of
  looking healthy while Caddy is dead.
- **Catalog:** every service has a healthcheck where the image supports one; the remaining lint
  warnings for `domains` are gone.

## [2.27.0] - 2026-11-22

### Added

- **Package Center: update notifications.** A background checker refreshes the signed catalog every
  six hours (`ETHOS_PACKAGES_UPDATE_CHECK`, `ETHOS_PACKAGES_UPDATE_CHECK_HOURS`) and publishes one
  Notification Center event per installed package with a newer version; its action opens Package
  Center. Repository failures stay in the log and never affect startup or shutdown.
- **Package Center: app-data lifecycle.** The uninstall dialog has a third, off-by-default checkbox
  that deletes `<DATA_DIR>/packages/data/<id>` (job step `data`) and shows its size in the preview.
  `GET /api/packages/orphans` lists data left behind by packages that are already uninstalled and
  `DELETE /api/packages/orphans/{id}` purges one; the Installed tab shows them under “Unused package
  data”.
- **Package Center: `ui.expose` / `ui.auth`.** A window UI served from the package's own port must
  state whether the LAN can reach it and whether the app brings a login. The detail dialog shows
  it, a loopback-only UI gets an “Open in a new tab” card instead of a dead frame, and an HTTPS
  desktop no longer renders a blank mixed-content iframe.
- **`ethos-pack lint` / `ethos-pack readme`.** Lint fails on unpinned images (`:latest`, `:stable`,
  version-less tags), a compose service without a `restart:` policy, a missing
  `min_ethos_version`, an invalid `ui.expose`/`ui.auth` pair, an index that lags its manifest and a
  stale README catalog table; `readme` regenerates that table from `index.json`.
- **Catalog: two new packages.** Syncthing (`ethos-sync`) for device and cloud-style folder sync,
  and Forgejo (`ethos-git`) for self-hosted Git (web on 3000, SSH on 2222).

### Changed

- **Compose packages declare Docker automatically.** `validate_manifest` (and `ethos-pack build`)
  add `"docker"` to `system_requires`, so Package Center preflights — and can provision — the
  engine before the first `compose up` instead of failing inside the start step.
- **Install and update preflight free disk space** (`ETHOS_PACKAGES_MIN_FREE_MB`, default 2048 MB)
  and answer 507 with the available and required size instead of failing mid-download.
- `ethos-pack build` recomputes the compose hash from the committed file, so editing a compose file
  can no longer leave a stale hash in a signed index.
- Job steps show their real labels (`requires`, `wheels`, `venv`, `sysdeps`, `data`).
- Catalog images are pinned: ClamAV 1.5.5 and nginx 1.27 instead of `:stable` / `:alpine`, and the
  antivirus, welcome and Forgejo/Syncthing containers ship healthchecks.

### Fixed

- Package Center could not reclaim the state every package keeps across reinstalls; it is now
  visible, sized and removable, and a package removed earlier can have its leftovers purged.

## [2.26.1] - 2026-11-22

### Fixed

- **Cameras: a camera's own retention setting is now enforced.** The janitor used
  only the global default, so `record.retention_days` set on one camera (for
  example 3 days on a camera whose RTSP is intermittent) was ignored. Each camera
  is now purged with its own value; the newest run of every camera is always kept.

## [2.26.0] - 2026-11-22

### Added

- **Cameras: continuous recording on single-stream cameras.** A camera without a
  sub-stream URL (Eufy NAS/RTSP and similar) now writes live HLS, the archive and
  the motion frames from one ffmpeg, using a single camera session instead of
  two. Cameras that allow only one RTSP client can record continuously.

### Fixed

- **Cameras: the recording quota is now a free-space budget.** The janitor lets
  recordings grow only into space that keeps `100 − quota_pct` of the volume
  free, so a system disk that already holds other data is never filled by
  camera recordings (previously `total × quota_pct` could exceed free space).

## [2.25.1] - 2026-11-22

### Security

- **Camera stream passwords no longer reach disk logs.** ffmpeg echoes the input
  URL (including `user:password@`) on connection errors; stderr is now drained
  through a redacting writer and the per-run log starts empty. A camera named
  after its URL is masked in the UI, events and notifications.

## [2.25.0] - 2026-11-22

### Added

- **Cameras 2.0 (Surveillance Station).** Continuous or scheduled recording with
  retention and a disk quota, timeline playback with MP4 range export, motion
  zones with per-camera schedules and event snapshots, per-camera viewer access,
  and a `/ws/surveillance` status/event socket. One long-lived ffmpeg per stream
  (`-c:v copy` for H.264) replaces the old "start a process every few seconds"
  motion loop; live view is HLS with an MJPEG fallback.
- Camera stream credentials are now stored AES-256-GCM encrypted and never
  returned by the API; management endpoints are admin-only.

### Changed

- The Cameras window was rebuilt on the shared package shell (rail, summary
  tiles, tables, light/dark tokens) with the `ethos-surveillance` 2.0.0 package.

## [2.24.1] - 2026-11-22

### Fixed

- **`setup_host_tuning.sh` now really switches zram to `zstd`.** The `zram-tools`
  postinst starts `zramswap.service` with its own defaults, so a plain
  `systemctl restart` hit `Device or resource busy` while writing
  `comp_algorithm` and silently left the device on `lz4`. The script stops the
  unit, swaps off and resets `/dev/zram0` first, then verifies the algorithm.
- **`vm.swappiness` is no longer overridden.** The sysctl file is installed as
  `99-zz-ethos-memory.conf`: an older `99-swappiness.conf` (written for the disk
  swap file) sorted after the previous name and won, leaving swappiness at 10.
  Re-running the script removes the legacy file.

## [2.24.0] - 2026-11-22

### Added

- **Optional host memory tuning: `setup_scripts/setup_host_tuning.sh`.** On boxes
  where the 4 GiB swap file fills up completely, this installs zram swap (50 %
  of RAM, zstd), sysctl values tuned for compressed swap, and raises
  `systemd-oomd`'s user-session pressure limit from 50 % to 80 % so a busy
  browser/editor is no longer killed while RAM is still reclaimable. Supports
  `--dry-run` and `--revert`; it is a manual, administrator-run step (no
  sudoers rule, not part of provisioning).
- **Swap-aware container limits.** Documented and applied for `flaresolverr`:
  `memswap_limit` must equal `mem_limit`, otherwise a container's memory limit
  still allows it to fill the host swap (docker defaults to 2× RAM).

## [2.23.1] - 2026-11-22

### Fixed

- **Radarr/Sonarr use the download folder you set.** The qBittorrent API key is
  one NAS-wide credential, not an account, so the integration was reading its own
  empty per-user settings and reporting a folder that does not exist
  (`/home/qb-admin/Downloads`). Grabs are now created in the Download Station
  folder of the admin account — and a `movies`/`tv` category resolves to
  `<download folder>/movies` as an absolute path — so imports land where the
  library expects them. Categories created by Radarr/Sonarr are stored in that
  same settings view instead of a throwaway one.

## [2.23.0] - 2026-11-22

### Added

- **EthOS gets out of the way of the OOM killer.** A memory-pressure guard reads
  `/proc/pressure/memory`, available RAM and swap; on hard pressure offline video
  downloads, video thumbnails, package installs, new downloads and music batch
  jobs pause and the Notification Center says why (recovery resumes them). Every
  ffmpeg/ffprobe child now runs with an address-space cap (3 GiB render, 1 GiB
  probe/camera, 512 MiB thumbnails) and a lower priority, so a runaway encode
  fails with a clear error instead of being the kernel's victim.
- **Memory guards in the service unit**: `MemoryMin=512M` keeps the NAS resident,
  `MemoryHigh=1536M` throttles reclaim, `ManagedOOMPreference=avoid` keeps
  `systemd-oomd` (active on Ubuntu, kills cgroups at 50 % pressure) away from us,
  `OOMPolicy=stop` plus `StartLimitBurst=10` turn a crash into one deterministic
  restart instead of a loop, and `MALLOC_ARENA_MAX=2` trims the RSS of a
  40-thread process.
- **Why did it die?** A marker file now records every start and is removed only
  by a clean stop, so a killed run (OOM, SIGKILL, power loss) is reported once in
  the Notification Center at the next boot.
- **System → Status** shows swap and the pressure level; the health monitor
  alerts on swap exhaustion and on sustained memory stalls (PSI); `/ws/stats`
  feeds every open dashboard from one shared sampler instead of one psutil
  snapshot per tab every two seconds.

### Changed

- Swap, sharing and music stream caches now drop expired entries instead of
  growing with every distinct path/track (guarded by a test).

## [2.22.0] - 2026-11-22

### Added

- **Books on the phone, round two.** The lock-screen progress bar now moves with playback, the
  player shows how much time is left, and a new **Anuluj** / **Ponów** pair manages downloads
  (a failed or evicted file can be retried without deleting the whole title). A finished batch
  reports through the Notification Center.
- **Pobrane titles work without a valid session.** After the 24 h token expires on a phone with no
  network, `/books` opens in an offline-only mode from the saved library; positions queue and sync
  on the next login.
- **Library sorting and a “Skończone” filter**, and the grid renders 60 cards at a time with
  “Pokaż więcej” and lazy, concurrent cover loading — a 20 000-title library no longer stalls the
  phone.
- **Reading gestures**: horizontal swipe inside EPUB chapters, cached locations (reopening a long
  novel is instant) and pinch-to-zoom in PDFs.
- **Books package 1.2.0** (separate signed store): the scan now reads each new audio track's
  duration and extracts the cover embedded in M4B/M4A/MP3 files, so audiobooks show artwork in the
  grid and on the lock screen. The library answers `ETag`/`304`, so an unchanged catalogue is
  revalidated instead of downloaded again.

### Fixed

- **Android Back closes the book sheet, reader and player** instead of leaving the app.
- **Phone and desktop agree on progress** — the phone used a size-weighted percentage while the
  desktop used an even per-track one; both now use the same formula.
- **An evicted download is no longer trusted**: finished files are re-checked against the cache on
  every start, marked missing and offered for retry instead of silently streaming from the NAS.
- **A late progress flush can no longer rewind the position** another device reached; the package
  keeps the newest `updated_at`.
- **Reader settings apply to an open book** (theme, font, size) without closing it.

## [2.21.1] - 2026-11-22

### Fixed

- **Tapping a search result plays the whole list instead of that one track.**
  The player queue now follows whatever list you tapped in — search results,
  a playlist, downloads or radio recents — so next/previous stay in context.
- **A deleted download stays deleted.** Removing a track while it was still
  queueing or streaming could let the worker write the row back a moment later;
  the deletion now wins the race.
- Deleting or cancelling a download no longer blocks the parallel slots, and a
  manual (pinned) download is no longer dropped by the next playlist sync.

### Added

- The player queue can be edited on the phone (remove a track, clear the queue)
  and survives a reload; hardware/back navigation returns to the previous view
  and deep links (`?view=playlist&id=…`, `?view=podcast&id=…`) open it directly.
- Search keeps paging (append-only) and its filters while you scroll, and the
  radio tab lists recently played stations.
- Download progress uses the shared task panel (`progress.css`) instead of a
  music-only copy.

### Changed

- Downloads run four at a time by default (1/2/4/6 selectable), covers are
  fetched only after the audio slot frees up, and progress repaints are
  throttled — a playlist sync is visibly faster on a phone.

## [2.21.0] - 2026-11-22

### Added

- **Videos on the phone now works like the desktop window.** The `/videos` app
  groups episodes into shows with seasons and a *Download season* action, has
  All / Movies / Series / My List tabs with sort and genre, recent-search chips
  and batch select in the default 480p version. The player offers subtitles,
  playback speed (0.5×–2×), cast, and an *up next* card that plays the following
  episode.
- **A downloaded film remembers where you stopped**, even offline: the player
  keeps its position on the device and, with no session and no network, the app
  opens straight into the downloaded library instead of the login screen.
- Download buttons estimate the size and warn before a film that cannot fit on
  the phone is queued.

## [2.20.2] - 2026-11-22

### Fixed

- **A core update no longer ends in a hard kill.** The service now stops within a
  few seconds: systemd signals only the main process, the app itself stops
  package services and video renders, and uvicorn waits at most 10 s for open
  streams (music playback, camera live view, transcoding) instead of hanging
  until the 90 s deadline. Previously the unit reported `Failed with result
  'timeout'`, a queued video export was restarted only to be killed again, and
  the update card was left showing a running job (`stale update job`).

## [2.20.1] - 2026-11-22

### Changed

- **Music downloads are about twice as fast.** Four tracks download at once
  instead of two (Settings → „Równoległe pobierania”: 1/2/4/6), each slot now
  fetches only the audio — the cover is stored afterwards, outside the queue —
  and the download badge repaints at most four times a second instead of on
  every chunk. The NAS proxy also reads 256 KB chunks instead of 64 KB.

## [2.20.0] - 2026-11-22

### Added

- **Books on your phone (`/books` PWA).** The Books package now has an installable phone client with
  the same library, progress and reader settings: audiobooks keep playing with the screen off
  (lock-screen controls, speed, ±skip, sleep timer), EPUB titles open in a paginated reader with
  themes and a table of contents, and PDFs render page by page with zoom. The continuations list,
  search and shared titles come from the package, so desktop and phone stay in sync.
- **Offline downloads of whole titles.** A title downloads into the phone's cache (audiobook tracks,
  EPUB or PDF) with per-file progress, a quota guard and retry; playback and reading then work in
  airplane mode, including seeking inside a chapter. Downloads need HTTPS.
- **Settings → Books on your phone** card that links `/books` next to the Music one.

### Changed

- The EPUB/PDF readers used by the phone app are vendored in the core (`frontend/static/vendor/`,
  same pinned versions as the package), so an installed app can open a downloaded book with no
  network. `THIRD-PARTY.md` lists hls.js, epub.js, JSZip and pdf.js.

## [2.19.1] - 2026-11-22

### Added

- **Music: an offline badge on every playlist.** Playlist cards (Home and Library)
  and the playlist header show how many tracks are already on the phone
  (`✓ 12/12`, `↓ 4/12`) and how many are still queued. It updates while downloads
  run, so you can tell at a glance what will play without the NAS.

## [2.19.0] - 2026-11-22

### Added

- **Videos: 480p · tiny download version, now the default.** A fourth version
  next to Original / 1080p / 720p (`ultrafast`, CRF 30, max 480p — roughly
  0.5 GB/h), so a whole holiday batch fits on the phone. Pick it in
  **Download settings**; the one-tap and multi-select Download use it.

### Fixed

- **Deleting a download that is still preparing/saving now works.** The trash
  cancelled only the local row, so the next 2-second sync brought the film back;
  it now cancels the NAS job too.
- **The downloading film's poster no longer flickers.** Progress repaints the
  existing list rows instead of rebuilding them (the poster is fetched once),
  and an unchanged sync no longer rewrites IndexedDB every 2 s.
- **No more lingering “Tasks 1”** on the Videos home page: the finished global
  progress row is dismissed after a few seconds (the Downloads sheet keeps the
  record).

## [2.18.1] - 2026-11-22

### Changed

- **Music: „Polubione” is a normal playlist.** The heart now writes to a real
  playlist with that name, so liked tracks download to the phone like any other
  playlist; the desktop app still shows the same hearts. Existing likes are
  migrated into the playlist on the next visit and the two views stay in sync.

### Fixed

- Music downloads on the phone resync when the app returns to the foreground or
  the connection comes back, instead of waiting for a full reload.
- A download that was queued when the app was closed resumes on the next start
  instead of being reported as interrupted.

## [2.18.0] - 2026-11-22

### Added

- **Videos: pick many films at once and download them in one go.** Tap
  **Select** in the phone app (`/videos`) or the desktop Video Station window,
  tap the posters (or **Select all**), then **Download · <default version>**.
  Every selected film is queued in the version from Settings — the NAS still
  prepares them one at a time, so a whole holiday batch is one action.

### Changed

- The Videos phone app now loads the saved download default on start, so the
  one-tap Download and the batch Download always show the same version as
  Settings (previously the first download after opening the app used 1080p
  until Settings was opened).

## [2.17.0] - 2026-11-22

### Added

- **Music on your phone (`/music`).** The EthOS Music app is now an installable web app:
  one search for tracks, podcasts, radio, the NAS library and playlists; the full
  player with queue, shuffle, repeat, sleep timer and lock-screen controls; radio with
  HLS; podcasts with resume; likes, playlists and shared playlists. Pin it from the
  browser and it opens like a native app.
- **Offline music.** Every track in a playlist is downloaded to the phone automatically
  and deleted from it when you remove the track from all playlists; podcast episodes
  download on demand. The Downloads view shows storage use and can pause, retry or
  delete a download.
- **Podcast audio proxy** (`/api/music/episode-audio`) so episodes hosted outside the
  NAS can be stored on the phone. Only public hosts pass, every redirect is re-checked,
  the payload is capped and nothing is written to disk.

### Changed

- The EthOS Music Android APK is **retired**. Settings → Updates now links to `/music`
  instead of offering the APK, and the phone app signs in with the normal NAS account
  (no API key).

### Removed

- The Music APK download row and its self-update channel; `/api/app/version?app=music`
  answers 404 with a pointer at the web app. Photos and Video APKs are unchanged.

## [2.16.0] - 2026-11-22

### Added

- **Videos: a real download queue for your holiday.** Films can now be queued
  many at a time; the NAS prepares them one by one and the Downloads list shows
  each stage (**Queued #3 → Preparing 42% → Ready → On this device**) with
  progress, version and cancel/retry. A reload or a closed app no longer loses
  the queue — it reconciles with the NAS instead of saying “interrupted”.
- **Download versions: Original / 1080p / 720p**, with the default chosen once
  in Settings and shared by the desktop Video Station and the `/videos` phone
  app. “Original” copies an H.264 video stream instead of re-encoding it, so a
  film that used to take an hour can be ready in seconds.
- **Video Station (package 2.2.0):** a Downloads page showing the same queue,
  “Save to this computer”, a version picker on the movie page and the download
  default in Settings.

### Changed

- Video downloads stream straight into the phone’s Cache Storage instead of
  buffering the whole film in memory, and the NAS frees each prepared file as
  soon as a device stored it (rolling window of five copies, 40 queued jobs).

### Fixed

- Re-opening Videos showed no download at all even though the NAS was still
  rendering it; the app now restores the real state.

## [2.15.1] - 2026-11-22

### Fixed

- **Videos download on a plain-http address now explains itself.** Offline
  downloads need HTTPS (or `localhost`) because browsers only expose Cache
  Storage in a secure context; the app says so instead of failing silently.
  Streaming and playback still work over plain HTTP.

## [2.15.0] - 2026-11-22

### Added

- **Videos on your phone (`/videos`).** A standalone, installable web app for
  films and shows: the whole library in a phone layout, a fullscreen player with
  screen wake lock, lock-screen controls and resume where you left off. Pin it
  from the browser and it opens like a native app on Android, iPhone and
  desktop.
- **Offline downloads.** Pick 720p, 1080p or the original quality and the NAS
  renders one compact MP4 (H.264 + AAC) that stays on the device. Progress shows
  in the global progress panel, playback and seeking work with no network at
  all, and each account can keep three films for seven days.
- **Videos app icons** generated by `scripts/make_icons.py`, so the installed
  app matches the desktop.

### Changed

- The Android Videos APK now targets **Android TV**; phones install `/videos`
  instead and keep downloads, fullscreen and wake lock.
- Every standalone app (`/notes`, `/videos`, desktop) registers the shared
  service worker itself, and downloaded films are served from Cache Storage with
  HTTP Range support.

## [2.14.1] - 2026-11-22

### Added

- **Sticky Notes 2.0: checklist tasks and reminders.** Notes can become
  checklists with togglable items, due dates and reminders. Search across notes
  and items, pin important notes, archive the rest, and pick one of five colors.
  The package now ships a private venv with Web Push support (VAPID), so a due
  reminder reaches the device even when the tab is closed.
- **A right-edge Sticky Notes ribbon.** Pin the package and enable “Show
  ribbon” in its Settings to get a collapsible tab with quick note/task capture,
  checklist toggles and a jump to the full app. The ribbon reserves a 44 px
  desktop gutter, so icons shift instead of hiding underneath it; it is hidden
  on narrow screens where the mobile app takes over.
- **Installable `/notes` app for phones.** A standalone quick-capture page with
  its own manifest, icons, New note / New task shortcuts and a share target.
  Writes work offline through a bounded queue and replay automatically; the page
  reuses the desktop session and offers the same reminders through Web Push.
- **`ui.ribbon` for pinned native packages.** The package schema validates a
  `ui.ribbon` entry, the core mounts it in a single `#ethos-ribbon-host`, and
  `ctx.setDesktopGutter()` lets a package reserve desktop space. Sticky Notes is
  the reference implementation. Requires Sticky Notes 2.0.0 (EthOS 2.14.1+).

## [2.14.0] - 2026-11-22

### Added

- **Share with other users (view-only).** A new **Sharing** app, a Settings →
  Sharing section and a reusable “Share with users…” dialog let you grant files,
  folders, Photos albums, Video Station folders and Music playlists to named
  accounts or everyone. Recipients can view and stream but never edit, delete or
  re-share; writes stay own-scope, `<DATA_DIR>` and Photos Private items can never
  be shared. Recipients are notified with a targeted “Shared with you” event.
  Photos/Video/Music/Books show a “Shared with me” section. Spec
  `docs/sharing.md`; API `/api/sharing`.

### Fixed

- **Dead Favorites disappear.** Files drops starred folders that no longer exist
  from the sidebar instead of keeping phantom links forever; a favorite on an
  unplugged USB/network volume is kept (dimmed, “not mounted”) and returns when
  the volume comes back. Deleting a folder in Files refreshes the list at once.

## [2.13.3] - 2026-11-22

### Fixed

- **Playback in every browser.** The player now negotiates the audio codec with
  the server: AAC normally, **Opus** when the browser has no AAC decoder in MSE
  (some Chromium/Electron builds) and **video only** — with a short notice — when
  neither is available, instead of dying with “The stream could not be played”.
  The web player also prefers hls.js whenever MSE exists, so Safari and Android
  Chrome no longer fall back to their native player, which treats the bounded
  HLS window as a live stream and fails on any seek outside it.

## [2.13.2] - 2026-11-22

### Fixed

- **Movies that showed a black screen now play.** Dolby Digital Plus tracks
  stored as `5.1(side)` — the usual layout in WEB-DL rips — made ffmpeg write a
  PCE-based AAC track that Chrome refuses to demux
  (`CHUNK_DEMUXER_ERROR_APPEND_FAILED`), so the player showed “The stream could
  not be played” while the server reported a healthy HLS session. Audio is now
  normalised to a standard 5.1/7.1 layout before encoding (HLS and the legacy
  pipe), and an AAC track is only copied in remux when its layout is standard.
  The player logs the real error, retries once and shows the reason with a
  “Try again” button; an encoder that dies mid-session is logged with its ffmpeg
  stderr and its slot is freed immediately.

## [2.13.1] - 2026-11-22

### Fixed

- **10-bit HEVC movies play in the web player again.** VA-API hardware decode
  fed 10-bit frames straight into `h264_vaapi`, which only accepts 8-bit nv12;
  ffmpeg exited with “No usable encoding profile found”, HLS returned 502 and the
  legacy fallback failed with the same error (“The stream could not be played”).
  The hardware path now converts with `scale_vaapi=…:format=nv12`, and HLS
  retries once with libx264 if a hardware encoder still rejects a specific file.

## [2.13.0] - 2026-11-22

### Added

- **Keyless video metadata and posters.** Video Station no longer needs a TMDB
  key: the core now runs a provider chain with Cinemeta (movies and series,
  posters/backdrops/IMDb rating/cast) and TVmaze (official series fallback,
  CC BY-SA), while TMDB remains optional and is preferred when a key is set.
  New `/api/videos/metadata/{status,test,refresh,image}` endpoints, a provider
  selector, per-source health checks, source attribution and a bounded
  automatic metadata fetch after opening the app.
- A versioned metadata cache (`video_metadata_matches.json`, migration from the
  old TMDB cache), a 14-day negative cache, generic artwork caching under
  `metadata_images/` (old `tmdb_images/` still served) and source fields on every
  match (`source`, `source_id`, `source_label`, `source_url`, `imdb_id`).
- **File Manager: extract any archive to a chosen folder.** Extract Here /
  Extract to… in the context menu, every format the backend recognises, split
  archives, encrypted ZIP/7z/RAR through a stdin-only password channel, and a
  one-click install when the archive tool is missing.

### Changed

- TMDB is now an optional quality/localization upgrade rather than a hard
  requirement. Existing `/api/videos/tmdb/*` routes and the `tmdb` response
  envelope remain as compatibility aliases for the Android app and package
  2.0.x; keyless refresh works even from the older package.
- Video Station package 2.1.0 exposes the provider choice, source health,
  “Fetch missing” / “Re-fetch all”, attribution and the optional TMDB key.
  Requires this release (`min_ethos_version 2.13.0`).

### Fixed

- Metadata refresh progress/notifications now report the real
  `titles_matched`/`files_matched` counts instead of always showing zero.

## [2.12.5] - 2026-11-22

### Fixed

- **Desktop context menus stay on screen.** Right-clicking the Trash shortcut in
  the bottom-right corner opened its menu past the window edge, cutting off
  “Refresh count” and “Remove from Desktop”. Menus now flip above/left of the
  pointer and clamp inside the viewport; on narrow screens they cap their size
  and wrap long labels.

## [2.12.4] - 2026-11-22

### Fixed

- **Tasks: one row per OS update job.** Reopening Settings → System (or any
  section re-render) while Ubuntu updates or an EthOS OS image job is running no
  longer stacks another identical task row. The existing row keeps updating in
  the background and reaches 100% even if you leave the System section.

## [2.12.3] - 2026-11-22

### Fixed

- **Enter confirms dialogs again.** Pressing Enter in New Folder, New File, prompt
  and confirmation dialogs now submits the typed value or the action instead of
  closing the dialog and silently doing nothing. Escape still cancels.

## [2.12.2] - 2026-11-22

### Fixed

- **Packages: uninstall finishes cleanly.** The uninstall job no longer fails at
  the cleanup step, so the package disappears from Installed right after the
  progress row reaches 100%.
- **Dialogs: primary buttons are styled again.** Confirm and form dialogs render
  the OK/Restart/Uninstall action with the same radius, padding and hover as every
  other EthOS button; long messages keep their paragraph breaks and scroll inside
  the dialog instead of stretching it off-screen.
- **Restart and Reboot confirmations name the action** (`Restart`, `Reboot`) and
  spell out what happens in two short paragraphs.

### Changed

- **Packages window polish.** The catalog search gets a search icon, category
  filters are pills, cards clamp long descriptions and align their actions, and
  the Sources tab no longer shows the catalog search/filter row. Start, Restart and
  Stop use the shared SVG icon set with accessible labels instead of text glyphs.

## [2.12.1] - 2026-11-22

### Fixed

- **Download Manager: Resume works after the download folder changes.** Tasks
  queued under an old path (for example `/home/qb-admin/Downloads`) are moved
  into the configured download directory when nothing has been downloaded yet,
  instead of failing with `[Errno 13] Permission denied`.

## [2.12.0] - 2026-11-22

### Added

- **Video Station 2.0 playback engine.** New bounded fMP4 HLS sessions
  (`POST /api/videos/hls`, owner-checked playlist/segments, stop endpoint) for
  HEVC/AC3/DTS/MKV on desktop, phones and the Android app, with a 40 s sliding
  window and per-user/global session caps. The legacy `/transcode` pipe stays as
  a fallback.
- **VA-API/QuickSync hardware decode + encode.** The media probe validates both
  tiers with real micro-encodes, captures ffmpeg stderr, pins the render node,
  picks the bitrate from the downscaled target height and caps concurrent
  transcodes. `/api/videos/status` now returns `encoder`, `vaapi`, active
  streams and per-candidate reasons; `/api/videos/info` reports the engine;
  transcode/HLS responses expose `X-Ethos-Mode`/`Encoder`/`Decode`.
- `GET /api/videos/bootstrap` (one walk for config, engine, home, roots, resume
  and My List), a 10 s library walk cache with `truncated`, `DELETE
  /api/videos/resume` (remove from Continue Watching), and a per-user My List
  (`GET/POST/DELETE /api/videos/list`).

### Changed

- Video Station package 2.0.0 uses a full-bleed cinema layout (hero, poster
  shelves, bottom tab bar on phones) and a custom HLS player with hardware
  engine diagnostics; the package needs this release (`min_ethos_version
  2.12.0`).
- Android Video 1.4.0 plays remux/transcode and multi-audio files through HLS,
  restores resume positions everywhere and reports the NAS encoder in stats.

### Fixed

- Video HLS sessions stop cleanly on shutdown/app end and expire abandoned
  sessions (no orphaned ffmpeg processes or disk growth).
- The `/videos` library cache no longer walks the whole tree for every Home and
  Library request.

## [2.11.0] - 2026-11-22

### Added

- **EthOS OS image updates (A/B).** Settings → System checks the signed
  `ethos-os-release` index, downloads the slot-agnostic rootfs artifact with
  SHA-256 verification, and writes it into the standby slot through the new
  `ethos-os-image` root helper. Reboot is a separate, explicit action; the
  image's boot counter and health gate roll a bad slot back automatically. Data
  on the shared `ETHOS-DATA` partition is never touched.
- APIs: `GET /api/system/os-image`, `GET …/status`, `POST …/check`,
  `POST …/download`, `POST …/install`, `DELETE …/download` (admin on mutations).
- UI: **EthOS OS** card with slot state, download/install progress and a
  reboot prompt; single-slot installs show the reflash hint read-only.

### Security

- `ethos-os-image apply` takes no arguments, is allowlisted by provisioning,
  verifies the signed index and image hash itself, resolves the target by
  partition label and **refuses to write the active slot**. It never reboots and
  writes metadata only after `e2fsck` plus a read-only verification pass.

### Upgrade

- `sudo bash setup_scripts/setup_service.sh` once installs/refreshes the helper
  through `deploy/provision.json` (v2.10.0 provisioning). A/B images only; a
  single-slot installation keeps working and shows the reflash guidance.

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
