# Music PWA (`/music`) — the phone player

Installable web app that **replaces the EthOS Music Android APK**: search,
playlists, likes, history, podcasts, radio and the full player, with the same
account and library as the desktop Music package. Everything that belongs to a
playlist is **downloaded to the device** and plays without the NAS.

- URL: `/music` (and `/music/`)
- Manifest: `/manifest-music.webmanifest` (`id`/`scope`/`start_url` = `/music`)
- Service worker: `/sw.js` (scope `/`), shell cache `ethos-shell-v7`
- API: `/api/music/*` (JWT), the same endpoints the desktop app and the retired
  APK used

## Why a separate page

The desktop shell is a window manager; a phone needs a thumb-sized player. The
page is a standalone document like `/notes` and `/videos`: it loads before the
user has a token, pins as an app and starts offline. The desktop Music window
(the `ethos-music` package) is untouched and shares state through the NAS.

## Routes and auth

`/music`, `/music/` and `/manifest-music.webmanifest` are **exact** public paths
in `backend/main.py` (see the `public_paths` note in `AGENTS.md`). The page asks
for the NAS login and stores the normal desktop JWT in `localStorage`; every
`/api/music/*` call carries it. `?view=search|downloads|library|radio|podcasts|settings`
opens a view directly (used by the manifest shortcuts).

**Install over HTTPS.** Service workers and durable storage exist only in a
secure context: open the page on the NAS hostname with a TLS certificate (or
`localhost`) and use the browser's “Install app” / “Add to Home screen”. Plain
`http://192.168.x.x:9000/music` still streams, but downloads are refused by the
browser and no icon can be pinned.

## Device downloads

The rule matches the Android app's `OfflineStore`:

- **every track in any playlist is downloaded** to the device;
- removing the track from **all** playlists deletes its audio from the device;
- podcast episodes are downloaded on demand and never auto-deleted.

The list is refreshed whenever the app comes back to the foreground (throttled
to once per 15 s) and when the connection returns, so a playlist edited on the
desktop starts downloading on the phone without reopening the app.

Every playlist shows how much of it is already on the device: a badge on the
card (Home and Library) and a counter + meter in the playlist header —
`✓ 12/12` when complete, `↓ 4/12` while some tracks are missing or queued. It
updates in place while downloads run, and each row keeps its own `offline` tag.

**Parallel downloads.** Four tracks are fetched at once by default; Settings →
„Równoległe pobierania” offers 1/2/4/6. Each slot carries only the audio — the
cover is stored afterwards, outside the queue — and the UI repaints at most
four times a second per track instead of on every chunk. The NAS resolves and
proxies every stream independently (the engine is a threaded loopback server),
so a playlist is limited by the link, not by the client. Backgrounding the PWA
may pause downloads: mobile browsers throttle background pages, hence the hint
in the Downloads view.

### Liked is the playlist „Polubione”

There is no separate liked list any more: the heart adds the track to a real
playlist with the fixed id `liked` and the name **Polubione**, which is why it
downloads like any other playlist. The server keeps the legacy `liked` array
identical to the playlist so the desktop Music window and old clients still see
their hearts; `GET /api/music/state` materialises the playlist for accounts that
only had likes, and asking `POST /api/music/playlists` for a playlist called
„Polubione” returns that same object instead of creating a duplicate.

Storage is **IndexedDB**, not Cache Storage: `ethos-music-db` keeps metadata in
`rows`, audio/cover Blobs in `blobs` and small state (library snapshot, episode
positions) in `meta`. A downloaded track plays from an object URL, so `<audio>`
seeks natively and the service worker keeps its “no `/api/*`, no `Range`” rule.
Videos use Cache Storage + a synthesized `206` handler because a film is one big
file; a track is a few megabytes and IDB is the simpler, safer fit.

| Download source | Endpoint |
|---|---|
| track already in the NAS library | `/api/music/library-file` (finite file) |
| track streamed from YouTube | `/api/music/stream` (Range proxy) |
| podcast episode | `/api/music/episode-audio?url=…` (SSRF-guarded proxy) |

The episode proxy exists because podcast CDNs usually send no CORS headers, so a
browser cannot read the body into a Blob. It validates the host (public
addresses only), re-validates **every redirect hop**, forwards `Range`, refuses
non-audio content types, caps the payload at 400 MiB and writes nothing to disk.

| Limit | Value |
|---|---|
| concurrent downloads | 4 by default (Settings: 1/2/4/6) |
| progress repaint | at most 4×/s per track |
| pause threshold | usage > 92 % of `storage.estimate().quota` |
| persistence | `navigator.storage.persist()` on start |
| interrupted download | re-queued on the next start (no byte resume) |

Progress uses the global `core/progress.js` task panel; a full disk pauses the
queue and emits one Notification Center warning instead of failing every track.

## Player

- `<audio>` + **Media Session** (lock-screen metadata, hardware keys, position
  state) — the web equivalent of the Android foreground service.
- Queue, shuffle, repeat (off/one/all), sleep timer (15/30/60 min), like toggle.
- A downloaded copy is preferred; if the stream breaks mid-track the engine
  switches to the local Blob instead of stopping.
- The session (queue, index, position, modes) survives a reload in
  `ethos.music.session`; a restored session waits for the first play so opening
  the app never triggers a yt-dlp run.
- Podcast episodes remember their position (`meta.pos:<key>`) and resume.
- Radio is live: no seek, no history entry, `● NA ŻYWO` badge. HLS stations use
  `/static/vendor/hls.min.js` when the browser has no native HLS support.

## Feature parity with the retired APK

| Android app | `/music` PWA |
|---|---|
| Home (greeting, recents, playlists) | same layout |
| Unified search (YouTube + podcasts + radio + library + playlists, suggestions, history, filters) | `type=all`, 300 ms debounce, local history (12) |
| Podcasts (catalog, subscribe, RSS, feed refresh, resume, offline episodes) | same endpoints, episodes cached in IDB |
| Radio (150 stations, categories, favourites, recents, HLS) | same catalog + `/radio/state` |
| Library (liked, playlists, shared playlists) | playlists only — likes live in the „Polubione” playlist
| Offline playlists (auto-download, delete when removed) | same (IndexedDB) |
| “Download whole playlist to the phone” | same button in the playlist view |
| Full player (seek, shuffle, repeat, like, queue, sleep timer) | same |
| Media Session / lock screen | same |
| Cast dialog, Bluetooth output picker | **not available** in a PWA |
| ICY “now playing” for live radio | **not available** (browser cannot read the stream headers) |
| Auto Backup restore after uninstall | **not available** — NAS state returns, device downloads do not |
| Self-hosted APK updates | obsolete: the page updates with the NAS |

## Files

| File | Role |
|---|---|
| `frontend/music.html` | standalone shell: login, nav, mini player, full player, sheet, toast host |
| `frontend/manifest-music.webmanifest` | install metadata + Search/Downloads shortcuts |
| `frontend/static/css/music.css` | phone layout on the shared tokens, both themes |
| `frontend/static/music/music.js` | auth, router, views, sheets, player UI |
| `frontend/static/music/player.js` | `Engine`: queue, Media Session, sleep timer, local-first source, HLS |
| `frontend/static/music/offline.js` | IndexedDB store, download queue, playlist mirroring, quota |
| `frontend/static/music/api.js` | JSON helper, track keys, playback vs download URLs |
| `frontend/sw.js` | shell precache (page, CSS, module graph incl. core) + offline fallback |
| `backend/routers/music.py` | `GET /api/music/episode-audio` (podcast offline) |
| `backend/routers/updates.py` | `RETIRED_APPS = {"music"}` → 404 for the old APK channel |
| `frontend/static/apps/settings.js` | APK list without Music, “Open /music” card |
| `scripts/make_icons.py` | `render_music()` → `music-192/512`, maskable, apple-touch |

## Tests

```bash
.venv/bin/python tests/test_music_pwa.py            # Playwright: login, search, auto-download, offline playback
.venv/bin/python tests/test_music_liked_playlist.py # like ↔ „Polubione” playlist mirroring + legacy migration
.venv/bin/python tests/test_music_episode_proxy.py  # SSRF guard, redirects, Range, size cap, retired APK
.venv/bin/python tests/test_pwa_assets.py           # manifest, icons, public paths, worker precache
.venv/bin/python tests/test_app_ui.py               # Settings contract (music-pwa-link, no apk-link-music)
.venv/bin/python tests/test_frontend_modules.py     # import graph + worker references
```

## Status

- [x] Public `/music` page, manifest, icons, SW precache + offline fallback
- [x] Login gate reusing the desktop token; install prompt; offline pill
- [x] Search (tracks/podcasts/radio/library/playlists), suggestions, history
- [x] Podcasts: catalog, subscriptions, feed refresh, episode download + resume
- [x] Radio: catalog, categories, favourites/recents, HLS, live badge
- [x] Library: playlists (likes are the „Polubione” playlist), shared playlists (read-only), create/delete
- [x] Device downloads: playlist mirroring, quota guard, storage meter, retries
- [x] Full player: Media Session, queue, shuffle/repeat, sleep timer, like
- [x] `episode-audio` proxy with SSRF guard and size cap
- [x] APK channel retired (endpoint 404, Settings row removed)
- [x] Docs, guards and CHANGELOG updated
- [ ] Optional: user-set storage cap instead of the 92 % quota guard
- [ ] Optional: deduplicate the phone UI and the desktop package UI
