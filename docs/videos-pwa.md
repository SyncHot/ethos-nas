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
   (`queued → rendering → done`). `q` (`original`, `balanced`, `fast`, `tiny`) is
   optional; omitted means the account default from
   `GET/POST /api/videos/download-settings` (default `tiny` = 480p, so many films
   fit on a phone). Many jobs may be queued: the NAS renders them **one at a
   time** and `queue_position` drives the UI.
2. `original` is the fast path: an H.264 4:2:0 source is copied (MKV → MP4
   remux) or served as is when it already is a phone-playable MP4
   (`mode=passthrough`); HEVC/10-bit sources fall back to a 1080p encode with a
   visible `note`. `fast`/`balanced`/`tiny` always re-encode (720p/1080p/480p).
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
5. After storing, the page calls `POST /api/videos/exports/{id}/consumed`, so
   the NAS frees that file immediately (it also expires with the TTL).
   The list is repainted **in place** while a transfer runs (the poster node is
   never recreated, so it does not flicker), a no-op sync does not touch
   IndexedDB, and the finished global progress row is dismissed after 3 s (the
   Downloads sheet is the lasting record on a phone).
6. Playback uses `localMediaUrl(id)` → `/__media/<id>`; the service worker
   answers it **with Range support** (synthesised `206`/`416` slices of the
   cached Blob) so seeking works offline.

Server-side rules (`services/video_render.py`, `services/video_engine.py`):

| Limit | Value |
|---|---|
| queued + rendering jobs per user | 40 (over the cap → `409`) |
| allowed download versions | `original`, `balanced`, `fast`, `tiny` |
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
  `/api/videos/resume/all` when the connection returns.
- Errors render inline with a retry that restarts the stream at the same
  position (one automatic attempt first).

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
  queue, streaming into Cache Storage, no whole-file Blob, version settings.
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
- [x] Service worker precache + `/__media` handler + shell fallback
- [x] Retention hook, shutdown handling, `docs` + `CHANGELOG`
- [ ] Settings row that points phones at `/videos` (APK stays for Android TV)
- [ ] Optional background sync of the resume queue while offline
