# Changelog

All notable, user-visible changes to EthOS NAS are documented here.

- Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
- Versioning: [SemVer](https://semver.org/) — one version is one `APP_VERSION` in
  `backend/config.py`, one git tag `v<APP_VERSION>` and one entry in the signed
  `releases/index.json`.
- The release ritual (bump → changelog → publish → tag) is mandatory for every push
  of runtime changes: skill `release-tag-changelog`.

## [Unreleased]

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
