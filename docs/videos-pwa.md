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

## Offline downloads

1. `POST /api/videos/exports {path, q, a}` → `202` with a job
   (`queued → rendering → done`); the server renders **one finite MP4**
   (`-movflags +faststart`, standard AAC layout) under
   `<DATA_DIR>/video_exports/files/<job>.mp4`.
2. The page polls `GET /api/videos/exports/{id}` (1.5 s) and mirrors progress
   into the global Progress panel (`core/progress.js`).
3. When done it streams `GET /api/videos/exports/{id}/download` into **Cache
   Storage** (`ethos-media-v1`) under the synthetic URL `/__media/<job id>`,
   stores the poster, an `.srt → .vtt` subtitle copy and an IndexedDB row
   (`ethos-videos-db`), so the film list, artwork and playback all work with the
   network off.
4. Playback uses `localMediaUrl(id)` → `/__media/<id>`; the service worker
   answers it **with Range support** (synthesised `206`/`416` slices of the
   cached Blob) so seeking works offline.

Server-side rules (`services/video_render.py`):

| Limit | Value |
|---|---|
| active renders per user | 1 (second request → `429` + `Retry-After`) |
| stored downloads per user | 3 (`409`) |
| total area | 20 GiB, free-space preflight 2 GiB (`507`) |
| job TTL | 7 days, swept by the retention loop |
| worker timeout | 6 h |

Renders run on their **own** `TranscodeLimiter(1)` so a download never starves
live playback. The same file+quality is deduplicated by
`source_key = path|size|mtime|quality|audio`. `public()` never exposes server
paths — only `name`.

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

- `tests/test_video_render.py` — queue, dedupe, caps, busy, cancel, failure,
  ownership, expiry, restart recovery (fake ffmpeg, no GPU).
- `tests/test_video_api.py` — `202` + poll to `done`, `Content-Disposition`,
  `Range 206`, `no-store`, dedupe, cross-user `404`, scope `403`, delete.
- `tests/test_video_engine.py` — `build_render_cmd` (faststart, `-progress`,
  hw/software filters, `-an`, opus).
- `tests/test_pwa_assets.py` — manifest, icons, public paths, worker precache,
  media cache survival, `/__media` Range.
- `tests/test_retention.py` — `video_exports` is measured but never swept by
  the retention service (the manager owns it).

## Status

- [x] Public `/videos` page, manifest and icons
- [x] Library, search, detail sheet, My List, continue watching
- [x] HLS player with codec negotiation, fullscreen, wake lock, Media Session
- [x] Finite MP4 render queue with caps, dedupe and notifications
- [x] Cache Storage downloads with Range-capable offline playback
- [x] Service worker precache + `/__media` handler + shell fallback
- [x] Retention hook, shutdown handling, `docs` + `CHANGELOG`
- [ ] Settings row that points phones at `/videos` (APK stays for Android TV)
- [ ] Optional background sync of the resume queue while offline
