# Videos PWA (`/videos`)

Installable web app for films and shows: the whole Video Station library in a
phone-shaped layout, with **offline downloads**, real **fullscreen** and a
**wake lock** so the screen never sleeps mid-film. It replaces the Android APK
for phones; the APK stays only for Android TV / legacy devices.

Spec of the player itself: `docs/video-station.md`. Shared PWA plumbing (one
root-scoped service worker, `/notes` is the other surface): `docs/notes-pwa.md`.

## Why a separate page

The desktop shell is a window manager — unusable on a phone. `/videos` is a
standalone document with its own manifest, so Android pins it as an app:

| Property | Value |
|---|---|
| id / start_url / scope | `/videos` |
| display | `standalone` (`fullscreen` on launch) |
| orientation | any (the player locks landscape itself) |
| theme colour | `#0b0d12` |
| icons | `videos-192`, `videos-512`, `videos-maskable-512`, apple-touch 180 |
| shortcut | `?view=downloads` |

## Routes and auth

`/videos`, `/videos/` and `/manifest-videos.webmanifest` are **exact** public
paths in `backend/main.py` (see the `public_paths` note in `AGENTS.md`). The
page itself asks for the login form; the token is the normal desktop JWT
(`localStorage`, `Authorization: Bearer`). Every `/api/videos/*` call still
needs it, including `/exports/*/download` and the `?token=` forms used by
`<video>`.

**Install over HTTPS.** Service workers, Cache Storage and the install prompt
only exist in a secure context, so open the page on the NAS hostname that has a
TLS certificate (e.g. `https://nas.example.com/videos`) or on `localhost`.
A plain-http LAN address (`http://192.168.x.x:9000/videos`) still plays and
streams, but downloads refuse with an explicit “needs HTTPS” message and no
icon can be pinned.

## Offline downloads

1. `POST /api/videos/exports {path, q?, a}` → `202` with a job
   (`queued → rendering → done`). `q` (`auto`, `original`, `balanced`, `fast`,
   `tiny`) is optional; omitted means the account default from
   `GET/POST /api/videos/download-settings` (default `auto` = copy when the
   phone can play the file, otherwise a fast small encode). Many jobs may be
   queued: the NAS renders them **one at a time** and `queue_position` drives
   the UI.
2. `auto` (the default) is one tap and the fastest safe path: a playable
   H.264 4:2:0 source is copied (MKV → MP4 remux, or served as is when it
   already is a phone-playable MP4 — `mode=passthrough`), a HEVC source is
   copied too when the phone declares HEVC support (iPhone/Safari), and
   anything else gets the cheapest fast encode — 480p for small sources, 720p
   otherwise, never the slow 1080p re-encode. `fast`/`balanced`/`tiny` always
   re-encode (720p/1080p/480p); `original` forces a copy and honestly reports a
   1080p fallback for 10-bit/4:2:2 H.264.
3. The page polls `GET /api/videos/exports` (2 s while the sheet is open or a
   job is active) and **reconciles on every start**: a render that continued
   while the app was closed reappears with its stage and position instead of
   being marked lost. Only the transfer needs the page — the NAS queue does not.
   **Select** turns the grid into a picker (`Select all` + the default version
   on the Download button), so a whole holiday batch is one tap; every selected
   film is queued through the same endpoint.
4. When a job is `done` the page streams `GET /api/videos/exports/{id}/download`
   through a counting `TransformStream` straight into **Cache Storage**
   (`ethos-media-v1`, key `/__media/<job id>`) — a multi-GB film never sits in
   RAM as one Blob. Poster + `.srt → .vtt` subtitle and an IndexedDB row
   (`ethos-videos-db`) follow.
4a. The transfer is **resumable**: each attempt streams into its own cache entry
   (`/__media/<id>?part=<n>`) and the row records the part sizes, so a reload, a
   locked screen or a dropped proxy connection continues with
   `Range: bytes=<done>-` instead of starting a 4 GB film from zero. The
   playable `/__media/<id>` key is written once, by streaming the parts into one
   response, only after the last byte lands; `200` (Range stripped) and `416`
   (NAS copy changed) restart the transfer cleanly. Partial entries are dropped
   on cancel/delete/retry and when the server job vanishes; two attempts with no
   new bytes pause the row until Retry (or the next app launch) so a dead
   transfer cannot spin forever.
5. After storing, the page calls `POST /api/videos/exports/{id}/consumed`, so
   the NAS frees that file immediately (it also expires with the TTL).
   The list is repainted **in place** while a transfer runs (the poster node is
   never recreated, so it does not flicker), a no-op sync does not touch
   IndexedDB, and the finished global progress row is dismissed after 3 s (the
   Downloads sheet is the lasting record on a phone).
6. Playback uses `localMediaUrl(id)` → `/__media/<id>`; the service worker
   answers it **with Range support** (synthesised `206`/`416` slices of the
   cached Blob) so seeking works offline.

**Encoding profile and sizes.** QSV/VAAPI run in constant-quality mode
(`-global_quality`/`-qp`) with a max-rate cap instead of a fixed bitrate:
480p ≈ 1.5 Mbps, 720p ≈ 2.8 Mbps, 1080p ≈ 5 Mbps (plus 128 kbps AAC). Sources
above 30 fps are capped to 30, and HDR10/HLG is tone-mapped to 8-bit H.264
(`zscale`/`tonemap`) so the download is not washed out. The phone's preflight
size estimate reads those same caps from `/api/videos/download-settings`.

**HEVC copies.** iPhone/Safari reports HEVC support; the PWA sends `hevc: true`
with the export and the NAS then copies a HEVC stream (MKV → MP4 remux with the
`hvc1` tag) instead of re-encoding it. Devices without HEVC still get H.264.

**ETA and speed.** `ffmpeg -progress` writes `speed=`, the render job exposes
`speed`/`eta_s`, and the downloads list shows `1.8× · ~4 min` next to the
percentage.

**Ready on the NAS.** A stored copy is no longer deleted by `consumed`; it stays
for 7 days (rolling window of 8 files per account, 50 GiB area cap). The
downloads sheet lists those exports under **Ready on the NAS** with one-tap
**save to this device** or a NAS-side delete — a second phone or a cleared cache
never triggers a re-encode.

**Background Fetch.** When Chromium exposes
`ServiceWorkerRegistration.backgroundFetch`, the transfer is handed to the
browser (`core/background-download.js`) instead of running in the page: it
continues after the app is switched away or closed, shows a system progress
notification, and `sw.js` copies the finished response into `ethos-media-v1`
and writes a result marker (`ethos-bg-downloads`) that `initOffline()`
reconciles on the next start. While the page is open it follows the live
`downloaded`/`downloadTotal` counters and reattaches by tag after a reload. A
browser that accepts the job but never starts it, iOS Safari and Firefox (no
API) fall back to the resumable in-page transfer above; that attempt also
skips the browser queue. Automation (`navigator.webdriver`) always uses the
in-page path so the Playwright request mocks stay authoritative.

Server-side rules (`services/video_render.py`, `services/video_engine.py`):

| Limit | Value |
|---|---|
| queued + rendering jobs per user | 40 (over the cap → `409`) |
| allowed download versions | `auto`, `original`, `balanced`, `fast`, `tiny` |
| prepared files kept per user | rolling window of 5 (oldest reaped) |
| total area | 50 GiB, free-space preflight 5 GiB (`507`) |
| job TTL | 7 days; “saved on device” rows drop after 1 day |
| worker timeout | 6 h |

Renders run on their **own** `TranscodeLimiter(1)` so a download never starves
live playback. The same file+version is deduplicated by
`source_key = path|size|mtime|quality|audio|mode` — a queued or finished job is
returned instead of a duplicate. `public()` exposes the owner's own `path` (the
same value `/library` returns) so both UIs can match a job to a library item
after a reload; nothing outside the caller's scope is ever returned.

## Player

- **Transport**: HLS (`/api/videos/hls` + hls.js from
  `/static/vendor/hls.min.js`) with `/transcode` fallback; AAC/Opus/`none`
  negotiation from `docs/video-station.md`.
- **Fullscreen**: `requestFullscreen()` on the player element plus
  `screen.orientation.lock('landscape')` when available.
- **Wake lock**: `navigator.wakeLock.request('screen')`, re-acquired on
  `visibilitychange` and after the `release` event; released on pause/close.
- **Media Session**: lock-screen metadata and `setPositionState` seeking.
- **Resume**: `/api/videos/resume` every 5 s, flushed via
  `/api/videos/resume/all` when the connection returns. Offline playback keeps
  its own row in the IndexedDB `resume` store (see below) and merges with the
  server value on the next online start (newest `updated_at` wins).
- **Subtitles**: the library `.srt`/`.vtt` tracks are offered in a picker; the
  text is fetched with the token and attached as a `blob:` `<track>` (`srtToVtt`
  converts SRT), so burning in or a second request per cue is never needed. An
  offline download keeps its converted `.vtt` next to the cached media and is
  used when the NAS is unreachable.
- **Up next**: for an episode, a 10 s countdown overlay offers the next episode
  in the same season (`#vd-upnext`, `#vd-upnext-play` / `-cancel`); autoplay is
  skipped when the player was opened from a download that is not on the device.
- **Speed / cast**: `0.5×–2×` playback rate kept in `localStorage` (`vd-speed`),
  plus the native Remote Playback picker (`x-webkit-airplay` on the element) when
  the browser exposes `remote`.
- Errors render inline with a retry that restarts the stream at the same
  position (one automatic attempt first).

## Library on a phone

- Tabs **All / Movies / Series / My List** with live counts, sort (recent,
  title, size, duration) and a genre filter; the choice is per session.
- **Series** groups episodes by show: `groupShows()` builds one card per show,
  opening it lists the seasons with a per-season **Download season** action and
  the episodes in order (resume badges included).
- **Search** filters client-side with a 200 ms debounce; the last 8 queries are
  kept in `localStorage` (`vd-recent-searches`) and offered as chips.
- **Batch select** (`Select`) shows the default version and the estimated total
  on the Download button; failures are collected and reported once at the end.

## Storage and offline sessions

- `estimateSize(item, quality)` scales the source size by the target bitrate
  (`QUALITY_BPS`) and shows `≈ 700 MB` before queueing; `quotaAllows()`
  preflights `navigator.storage.estimate()` so a film that cannot fit warns
  instead of failing mid-transfer (95 % of the free space is the ceiling).
- `ethos-videos-db` is **v2**: `downloads` (cached media rows) and `resume`
  (`{path, time, duration, updated_at}`), so progress survives a reload, a
  closed tab and a dead NAS.
- **Offline unlock**: when the page has no token, no network **and** downloaded
  rows on the device, `boot()` opens the Downloads view instead of the login
  form (`Offline · <version>`), so a plane-mode phone still plays the trip
  library. A valid session restores the normal flow.
- **NAS restart mid-transfer**: a network error or 5xx moves the row to
  `waiting` and retries automatically with a 5 s → 2 min backoff (partial
  `?part=` entries resume with `Range`); `online` and returning to the tab retry
  immediately. Only a permanent 4xx asks for **Retry**.
- **Wznów wszystkie**: the Downloads view has one button that re-queues every
  `failed`/`waiting` row. It calls `retryDownload()` per title, which asks the
  NAS for the same export (deduplicated) or a fresh one when the old job
  expired — useful for rows that failed before the auto-retry release or after
  the export TTL.

## Files

| File | Role |
|---|---|
| `frontend/videos.html` | login card, library, sheets, player |
| `frontend/manifest-videos.webmanifest` | install metadata |
| `frontend/static/css/videos.css` | mobile-first styles on the shared tokens |
| `frontend/static/videos/videos.js` | library, detail sheet, player, downloads UI |
| `frontend/static/videos/offline.js` | IndexedDB rows, Cache Storage, progress |
| `frontend/static/vendor/hls.min.js` | hls.js 1.7.4 (Apache-2.0, `THIRD-PARTY.md`) |
| `frontend/sw.js` | shell precache, `/__media` Range handler, push |
| `backend/services/video_render.py` | render queue, caps, sweep, notifications |
| `backend/routers/video_station.py` | `/exports` endpoints |
| `scripts/make_icons.py` | `render_video()` icons |

## Tests

- `tests/test_video_render.py` — queue (many jobs, one render at a time),
  dedupe, caps, rolling window, consume, passthrough, cancel, failure,
  ownership, expiry, restart recovery (fake ffmpeg, no GPU).
- `tests/test_video_api.py` — `202` + poll to `done`, default version from
  `/download-settings`, queued second job, `queue_position`, `/consumed`,
  `Content-Disposition`, `Range 206`, `no-store`, dedupe, cross-user `404`,
  scope `403`, delete.
- `tests/test_video_engine.py` — `build_render_cmd` copy mode (remux),
  `download_mode` passthrough/fallback, plus faststart/`-progress`/hw/`-an`.
- `tests/test_videos_pwa.py` — static guards: reload reconciles with the NAS
  queue, streaming into Cache Storage, no whole-file Blob, resumable parts
  (`?part=<n>` + `Range`), stalled-transfer pause, version settings,
  series/seasons, tabs, subtitles, up-next, offline unlock and the `resume`
  store.
- `tests/test_videos_pwa_ui.py` — Playwright E2E on an isolated instance with
  ffmpeg fixtures (show with two subtitled episodes + one movie): tabs, series →
  season, episode sheet, subtitle attachment, up-next, batch download in the
  default version, offline unlock and offline resume.
- `tests/test_pwa_assets.py` — manifest, icons, public paths, worker precache,
  media cache survival, `/__media` Range.
- `tests/test_retention.py` — `video_exports` is measured but never swept by
  the retention service (the manager owns it).

## Status

- [x] Public `/videos` page, manifest and icons
- [x] Library, search, detail sheet, My List, continue watching
- [x] HLS player with codec negotiation, fullscreen, wake lock, Media Session
- [x] Finite MP4 render queue with caps, dedupe and notifications
- [x] Server-backed multi-film queue, rolling window and `original` copy mode
  (core 2.16.0)
- [x] Multi-select batch queueing in the default version (core 2.18.0)
- [x] Cache Storage downloads with Range-capable offline playback
- [x] Resumable transfers — a reload or dropped connection continues with
  `Range: bytes=<done>-` instead of re-downloading from zero
- [x] Service worker precache + `/__media` handler + shell fallback
- [x] Series grouping with seasons and “Download season” (core 2.20.0)
- [x] Subtitles, up-next autoplay, playback speed and cast
- [x] Library tabs, sort/genre, recent searches, size estimate + quota check
- [x] On-device resume (`resume` store, DB v2) and offline unlock without a
  session
- [x] Retention hook, shutdown handling, `docs` + `CHANGELOG`
- [ ] Settings row that points phones at `/videos` (APK stays for Android TV)
- [ ] Optional background sync of the resume queue while offline
