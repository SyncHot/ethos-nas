# Feature: Videos — Jellyfin-style library experience

Turn the flat folder browser into a proper media-library UI: parsed titles,
home rows (Continue Watching / Recently Added), Movies and Series views with
episode grouping + next-episode autoplay, and a subtitle track picker.

## Rationale (from codebase scan)
- `backend/routers/video_station.py` already has the hard parts done well:
  Range streaming (`/stream`), live QSV/libx264 transcode pipe (`/transcode`),
  resume positions (`/resume`, `/resume/all`), ffmpeg thumbnails, ffprobe info
  with disk cache, sibling-subtitle discovery. What it lacks is the *library*
  layer Jellyfin has: no titles (raw filenames only), no home screen, no
  series/episode grouping, subtitles auto-picked with no choice.
- The real library (`/media/marcin/SharedDrive/torrents`) uses messy release
  names (`…_4096p_8K_LR_180_1784103493.mp4`), so a conservative filename parser
  that degrades to "cleaned filename" is the right call — no external metadata
  APIs (TMDB needs keys + network; out of scope).

## Design

### Metadata service — `backend/services/video_metadata.py`
Pure, dependency-free filename parsing. One function:

```python
parse_video_name(filename) -> {
    "title":   str,          # cleaned display title (underscores→spaces, junk stripped)
    "year":    int | None,   # 1900–2035 standalone token or .YYYY. pattern
    "season":  int | None,   # from SxxEyy
    "episode": int | None,
    "show":    str | None,   # series name (prefix before SxxEyy, else parent folder)
    "kind":    "movie" | "episode" | "other",
    "quality": str | None,   # 360p/720p/1080p/4K/8K/… incl. 7680x3840→8K etc.
}
```

Rules (conservative — worst case is a tidied filename):
- `S(\d{1,2})[ ._-]?E(\d{1,3})` → episode; show = text before the token (or the
  parent folder name); title = text after it ("Episode N" fallback).
- Quality: p-suffix tags, 4K/8K/UHD words, `WxH` resolution pairs.
- Junk stripped from titles: codec/quality/release tags (h265, hevc, x264,
  hdr10, dolby, bluray, remux, proper, lr, fisheyeNNN…), 9+ digit unix
  timestamps, bare numeric IDs.

### Backend API changes (`routers/video_station.py`)
- Every video item from `GET /api/videos/library` (both folder and All-Videos
  walk) gains the parsed fields above — single source of truth shared by the
  web UI **and** the Android app.
- New `GET /api/videos/home?limit=12`: one bounded walk over all roots, then:
  - `continue_watching` — items with a resume entry (5 s < t < 97 %), newest
    activity first; each item carries `resume_time`/`resume_duration`.
  - `recently_added` — top N by mtime.

### Frontend (`frontend/static/apps/video-station.js`)
- Sidebar gains **🏠 Home**, **🎬 Movies**, **📺 Series** above the folder list.
- **Home**: horizontal card rows for Continue Watching (progress bar + "resume
  at mm:ss") and Recently Added, then a Browse hint with top-level folders.
- **Movies**: grid of `kind == movie` items — clean title, year, quality badge.
- **Series**: cards per show (episode count, latest S/E) → episode list grouped
  by season → play; on `ended`, auto-advance to the next episode in the same
  show (toast announces it).
- Cards everywhere use parsed titles instead of raw filenames.
- Player: subtitle track **picker** (dropdown) when >1 sibling sub exists,
  default = preferred/first as today.
- Existing behavior preserved: folder browsing, picker, search/sort/view,
  transcode fallback + seek-reload, resume save cadence.

### Android app — `~/apps/compose/ethos-video` (separate project)
Jetpack Compose app mirroring `ethos-music`:
- Auth = API key (`ethos_…`, issued per user in NAS Settings → Security),
  passed as `?token=` on every request — identical to ethos-music.
- Screens: Settings (server URL + API key, connection test), Home rows,
  Movies grid, Series/episodes, Player (Media3 ExoPlayer; `/stream` or
  `/transcode?t=` chosen by `needs_transcode`; resume save/restore).

## Security notes
- No new public endpoints — everything stays behind the JWT/API-key middleware.
- Parser is pure string processing on filenames; no path handling, no shell.
- Android app talks only to the user's own NAS with their own key.

## Phase 5 — flawless playback engine + settings (2026-09-27)

Goal: play *any* movie with sound on this box, plus a Settings panel to manage
the library and playback.

### Research findings (web + hands-on verification on this machine)
- **Browsers cannot decode AC-3/E-AC-3/DTS on Linux.** Chromium only gets AC3/
  EAC3 via Windows Media Foundation; Firefox has no support at all. So AAC is
  the universal web audio target — exactly what Plex/Jellyfin do.
- **Intel QSV (h264_qsv) does not work here**: ffmpeg 8 links libvpl, but the
  oneVPL *implementation* backend (`libintel-media`) is not installed on Ubuntu
  26.04 — `Error creating a MFX session: -9`. VA-API **decode** works
  (iHD driver + i915), but VA-API **encode** fails at device init on kernel
  7.0 (`Impossible to convert … auto_scale_0`). The existing `_qsv_available()`
  micro-encode probe detects this and falls back to software libx264 — no code
  change needed; if Intel's oneVPL runtime is ever installed, QSV is picked up
  automatically.
- **CPU reality (Intel N150, 4 cores)**: libx264 veryfast ≈ 8 fps @1440p
  (~0.13× realtime). Full re-encode of 4K/8K HEVC can't keep up live — but the
  user's movie library is H.264 MKV with Dolby Digital (AC3/EAC3) audio, which
  needs **no video encoding at all** → see remux path below.

### Playback modes (`_playback_mode()` in `routers/video_station.py`)
The server picks the lightest path that works; `/info` reports it as
`playback_mode`, and the player just asks for `/stream` or `/transcode`:
- **direct** — h264/vp8/vp9 + aac/mp3/opus in mp4/webm/mov → raw Range stream.
- **remux** — video codec OK but audio/container isn't (the common case: H.264
  MKV with AC3/EAC3/DTS): `-c:v copy` into fragmented MP4, audio re-encoded to
  AAC only when needed (AAC is copied). Lossless video, **real-time on any CPU**
  (verified: 5 s of 1080p h264+eac3 MKV → valid fMP4 in 1.6 s).
- **transcode** — video codec not browser-playable (HEVC/AV1/MPEG-2…): full
  re-encode, QSV when available else libx264 with quality presets:
  `fast` = ultrafast/crf28/≤720p, `balanced` = veryfast/crf23/≤1080p (default),
  `high` = veryfast/crf20/source res. Downscaling is what keeps live playback
  watchable on a Celeron-class CPU. `-g 30` on the libx264 path: with the
  default 250-frame GOP a slow CPU took ~a minute to emit the first fragment.
- `needs_transcode` (bool) is kept for Android-app compatibility.

### Audio tracks
- `/info` now returns `audio_tracks[]` = every audio stream in file order with
  `{index, codec, language, channels}`; index maps to ffmpeg `0:a:<idx>`.
- `/transcode` accepts `a=<track>` (clamped server-side) and `q=<preset>`;
  AAC tracks are copied, everything else is encoded to AAC at the configured
  bitrate. Info cache bumped to schema 2 so old entries re-probe once.
- Player: 🔊 dropdown when >1 track; default = Settings → preferred language
  (ISO prefix match) else first track. Switching tracks re-requests the pipe
  from the current time; switching away from direct play flips to processed
  mode automatically.

### Settings panel (`⚙️ Settings` in the Videos sidebar)
Full-screen overlay, same pattern as the folder picker:
- **Library** — list of root folders with per-root remove (✕) + Add Folder
  (reuses the existing picker). Previously roots could only be added, never removed.
- **Playback** — transcode quality preset, AAC bitrate (96k–256k), preferred
  audio language, auto-load subtitles toggle, autoplay-next-episode toggle.
- **Media Engine** — live status from new `GET /api/videos/status`
  (`{ffmpeg, ffprobe, qsv}`): shows QuickSync vs software libx264 and explains
  why when QSV is unavailable.
All options are whitelist-validated server-side in `_sanitize_options()`
(unknown keys dropped, bad values fall back to defaults).

### Test plan (Phase 5)
- [x] Isolated instance (`:9008`, `ETHOS_DATA_DIR=/tmp/ethos-test-data`):
      `/status` reports qsv=false; config POST sanitizes options.
- [x] `/info` on real EAC3 MKV → `playback_mode=remux`, audio_tracks correct.
- [x] Remux pipe: 12 s capture of the Spa_Weekend MKV = valid fMP4
      (h264 copy + AAC), real-time.
- [x] Transcode pipe on HEVC: downscale to ≤720p verified; `-g 30` fragment
      arrival verified; multi-track file → `a=1` remux encodes AC3→AAC and
      keeps the language tag.
- [x] Playwright smoke (`/tmp/vs_smoke.py`, throwaway): settings panel renders,
      root listed, status chips render, option toggle persists via API, player
      on EAC3 MKV uses /transcode with REMUX chip + audio label, video buffers,
      no page JS errors.
- [ ] ethos-video Android app: unaffected (still reads `needs_transcode`);
      can adopt `playback_mode`/`audio_tracks` later.

## Phase 6 — TMDB metadata + local posters (2026-09-29)

Goal: enrich the library with The Movie Database data — real titles, years,
ratings and plot blurbs — plus locally cached poster/backdrop artwork. **Files
are never renamed or moved**; on-disk names stay exactly as they are, TMDB is
display-only.

### Design decisions
- **Key storage**: the API key lives in `video_station_config.json` under a new
  top-level `tmdb` object (`{"api_key": "…", "auto_fetch": true}`) — same file
  as roots/options, gitignored. `GET /config` never returns the raw key (only
  `api_key_set` + last-4 mask); `POST /config` replaces it only when a non-empty
  value is sent.
- **No new dependencies**: `services/tmdb_service.py` uses stdlib `urllib`
  (sync, called via `asyncio.to_thread`). TMDB v3 REST: `/search/movie?query=&year=`,
  `/search/tv?query=`. Images come from `image.tmdb.org` and are **downloaded
  to disk** (`DATA_DIR/tmdb_images/`) — the UI never hotlinks TMDB's CDN, so
  artwork survives offline and costs no external bandwidth at browse time.
- **Matching**: movies match on parsed `title` + `year`; series on the show
  name (SxxEyy prefix or parent folder). A wrong guess only affects display
  (poster/title), never the file. Results are cached in
  `DATA_DIR/tmdb_matches.json` keyed by absolute path, so TMDB is queried once
  per title — library listings read the cache only and make zero network calls.
- **Query sanitization** (match-rate driver): parsed titles leak release-name
  noise (`Interstellar WEB DL GTBA`, `Dune Part Two NF GROUP`). Before
  searching, `_clean_query()` strips a stoplist of resolution/source/codec tags,
  streaming sources and common release groups anywhere in the title, plus a
  trailing bare year (the year is sent as its own parameter) and stray SxxExx
  tokens for shows. If that still misses, up to two prefix fallbacks drop
  trailing words (`The Matrix GROUP` → `The Matrix`).
- **Candidate ranking**: TMDB's `year` search param is a soft preference, not a
  hard filter — so `_pick_movie()` ranks explicitly: exact title in the
  requested year > anything from that year (so `John Wick` + 2023 picks
  *Chapter 4*) > exact title any year > TMDB's top hit. TV search prefers an
  exact show-name match.
- **Throttle**: ≥150 ms between API calls + one in-flight refresh guard; a
  batch refresh of a few hundred titles takes ~a minute, which is fine for a
  NAS-scale library. `auto_fetch` (default on) fires one background batch after
  the first library load per app session; matched items are never re-queried.
- **Artwork**: backdrop w780 (16:9, used in hero + browse list view) + poster
  w342 (used in all 2:3 card views — grids and horizontal rows). Served by
  `GET /tmdb/image?file=`
  with a strict filename allowlist (`[A-Za-z0-9._-]+`, no separators).

### Backend API additions (`routers/video_station.py`)
- Every library item gains an optional `tmdb` field from the match cache:
  `{kind, tmdb_id, title, year, rating, overview, poster_file, backdrop_file}`.
- `GET  /api/videos/tmdb/status` — `{api_key_set, api_key_masked, auto_fetch,
  matched_count}` for the settings UI (raw key never leaves the server).
- `GET  /api/videos/tmdb/test` — validates the stored key via `/configuration`.
- `POST /api/videos/tmdb/refresh` — body `{path?, force?}`: single item when
  `path` given (`force` re-queries a cached title), otherwise a bounded batch
  over all roots (unmatched items only, grouped by query so one season costs
  one API call). Downloads artwork per match. Returns
  `{titles_checked, titles_matched, files_matched, errors}` (+ `match` for
  single-item calls).
- `GET  /api/videos/tmdb/image?file=…` — locally cached TMDB artwork.

### Frontend (`frontend/static/apps/video-station.js`)
- Settings gains a **🎭 Metadata (TMDB)** section: API key input (masked,
  placeholder shows the stored last-4), auto-fetch toggle, “Test connection”
  and “Fetch metadata now” buttons with a live status line.
- Grid views (movies, series, browse, search) are poster walls: portrait 2:3
  cards with the local **TMDB poster** first (backdrop, then ffmpeg frame as
  fallbacks), title + ★ rating/year always visible below, hover = play
  overlay. Cards in the browse and search grids also carry the 🔄 (re-match
  this title) button in the hover, same as movie cards.
- Horizontal rows (Home: Continue Watching / Recently Added; show detail:
  season rows) use the same portrait 2:3 TMDB posters as the movies grid
  (2026-10-05): CW posters keep the red resume bar + ✕ remove; the hover panel
  shows play/info/🔄. The hero stays 16:9 with the backdrop.
- Cards use the local backdrop/poster when matched; subtitle shows TMDB year +
  ★ rating.
- Player header shows the TMDB title/year/rating chips when available.

### Test plan (Phase 6)
- [x] Unit: service normalization (search result → match dict), canonical image
      filenames, config masking/sanitization, query cleaning (`_clean_query` on
      messy scene names + edge titles like `2012`, `28 Days Later`).
- [x] Live matching matrix: 10/10 real TMDB matches across group-tagged,
      source-tagged and clean names; John Wick + 2023 → Chapter 4.
- [x] Isolated instance (`:9010`, `ETHOS_DATA_DIR=/tmp/ethos-test-data-tmdb`):
      scratch library under `/tmp/ethos-vs-tmdb-test` with a generated
      MKV named like a real release; set key via API → single + batch refresh →
      verify match cache, local image files on disk, `/tmdb/image` serving,
      and that `GET /config` masks the key.
- [x] Frontend: `node --check`; settings section renders; cards show poster +
      rating after fetch (Playwright if available).

## Phase 7 — localized metadata, original artwork, IMDb exact match (2026-10-12)

Goal: Jellyfin/Radarr parity for the TMDB layer — user-selectable metadata
language, full-resolution local artwork, zero-ambiguity matching when a
filename carries an IMDb id, and richer display data (genres/cast/runtime).

### Design decisions
- **Language setting**: `tmdb.language` in `video_station_config.json`
  (default `pl-PL`). Validated server-side with a conservative TMDB-style code
  regex (`[A-Za-z]{2,3}(-[A-Za-z]{2,4})?`) — an invalid or empty value keeps
  the previous one, so "no language" mode is unreachable. Sent as the `language`
  param on search, `/find` and detail calls → TMDB returns localized
  titles/overviews/posters where it has them.
- **Original-size artwork**: `download_image()` now requests `original`
  instead of w780/w342 — local disk is cheap and the UI never hotlinks TMDB's
  CDN. `_sweep_tmdb_images()` deletes unreferenced files after each refresh,
  so old w780/w342 files are cleaned up in place on upgrade.
- **IMDb exact match**: `extract_imdb_id()` finds `tt\d{7}` embedded in the
  filename (word-boundary, so `xtt1234567` doesn't count). When present,
  `/find/{imdb_id}?external_source=imdb_id` is tried first — zero ambiguity,
  the Radarr/Jellyfin path; on failure or no record it falls back to the
  language-aware title search.
- **Detail extras**: after a match, `_fetch_details()` fetches the full record
  with `append_to_response=credits` and stores `genres[]`, `cast[:5]` and
  `runtime` in the match cache (`TMDB_MATCH_FIELDS` extended). A failed detail
  fetch keeps the match usable but leaves the entry "stale" (no `genres` key)
  so the next batch refresh retries it.
- **Gated candidate picking** (fixes blind top-hit picks): `_pick_movie()` /
  `_pick_tv()` now require ≥50% query-token overlap in a non-exact candidate's
  title; a lone result is still accepted (translated/foreign titles); otherwise
  missing beats a wrong poster. Same-year movie candidates are also gated, so
  `John Wick` + 2023 picks *Chapter 4* but a random same-year film never wins.
- **Movie→TV fallback**: anthology films and specials sometimes exist only as
  TV shows — one plain (gated) TV lookup is the last resort for movie matches.
- **Staleness upgrade path**: cached entries without `genres` are treated as
  stale in `_tmdb_refresh_sync()` — a batch refresh re-fetches them, upgrading
  pre-Phase-7 caches to localized metadata + original artwork in place (no
  cache wipe needed).
- **Config partial-patch fix**: `_sanitize_tmdb()` now falls back to the
  previously stored value for *every* missing key (previously a key-only save
  reset `auto_fetch` to True). The frontend settings panel sends one partial
  `{api_key?, auto_fetch, language}` patch; empty key keeps the stored one.

### Frontend (`frontend/static/apps/video-station.js`)
- Settings → 🎭 Metadata (TMDB) gains a **Metadata language** input (TMDB code,
  e.g. `pl-PL` / `en-US`), persisted with the rest of the section on save.
- Hero meta row shows up to two genres; card info toast and "More Info" show
  full genre list + top-3 cast (`_infoBits()`); player header gains a first-genre
  chip next to rating/year.

### Test plan (Phase 7)
- [x] `tests/test_tmdb.py` (in-repo, standalone): IMDb-id extraction edge cases,
      gated picking (movie + tv), `_clean_query` regression, `_sanitize_tmdb`
      partial-patch semantics — plus a live section against the stored key:
      connection test, `/find` movie (Inception) + tv (Breaking Bad), localized
      vs default search matching the same `tmdb_id`, and a full `fetch_match`
      via IMDb id with genres/cast extras + original-size poster on disk.
      34/34 green (2026-10-12).

## Phase 8 — Cinema UI + HLS + hardware engine (2026-11-22)

This phase supersedes the 2026-10-04 HW note below: after the service user was
added to `render`/`video`, the VA-API H.264 micro-encode **passes** on this host
(production log: `Videos media engine: VA-API H.264 encode via
/dev/dri/renderD128`). QuickSync still fails with `MFX session: -9` because the
oneVPL implementation is missing; libx264 remains the software fallback.

### Hardware engine

- `backend/services/video_engine.py` owns playback-mode selection, ffmpeg
  argument builders and the cached `HardwareProbe` (QSV → VA-API → libx264,
  real micro-encode, captured stderr tails, pinned QSV device, probe lock/TTL).
- Transcode now uses **VA-API/QSV hardware decode** when a short per-file probe
  accepts the codec (`-hwaccel vaapi -hwaccel_output_format vaapi` +
  `scale_vaapi` / `h264_vaapi`), with automatic software decode + `hwupload`
  fallback. `config.options.hardware_decode` (default on) is the kill switch.
- Bitrate follows the **downscaled target height**, not the source resolution.
- `/api/videos/status` adds `encoder`, `vaapi`, `active_transcodes` and a
  structured `hw` object (`device`, `reason`, `candidates.{qsv,vaapi,libx264}`
  with ffmpeg stderr, `decode`, `ffmpeg_version`, `probed_at`); `qsv` stays for
  compatibility. `/api/videos/info` adds `engine`; transcode/HLS responses send
  `X-Ethos-Mode`, `X-Ethos-Encoder`, `X-Ethos-Decode`.
- A global limiter caps concurrent transcode/HLS work at 2 (HTTP 429 + Retry-After).

### HLS sessions

- `backend/services/video_hls.py`: per-session temp dir under
  `<DATA_DIR>/video_hls/<sid>`, owner-checked, uuid4 ids, `-re` real-time read,
  fMP4 segments (`-hls_time 4`, list 10 ≈ 40 s), `delete_segments` +
  `independent_segments` + `temp_file`, init.mp4. Caps: 2 sessions global, 1 per
  user; idle TTL 120 s, dir TTL 600 s; sweeper registered with `task_registry`;
  `stop_all()` on app shutdown.
- Endpoints (JWT + `path_scope.scope_request`, no public paths):
  `POST /api/videos/hls`, `GET /api/videos/hls/{sid}/index.m3u8` (rewrites
  segment URIs with `?token=` for API-key clients), `GET /hls/{sid}/{init.mp4|segNNNNN.m4s}`
  (strict allowlist), `POST|DELETE /hls/{sid}/stop`. `/transcode` stays as the
  legacy pipe/fallback.
- Player contract: start at an absolute `t`; far seeks restart the session at
  the target; audio/quality changes restart with `a=`/`q=`; resume times are
  absolute.

### Library API

- One short-TTL walk cache (10 s) shared by `/home`, `/library` and the new
  `GET /api/videos/bootstrap`; `truncated` reports the 10 000-file cap.
- `DELETE /api/videos/resume?path=` removes a title from Continue Watching.
- Per-user My List: `GET /api/videos/list`, `POST /api/videos/list` (add/remove
  by path), `DELETE /api/videos/list?path=` (store `video_station_list.json`,
  capped at 2 000 paths). The web package migrates the old localStorage list.

### Web package 2.0.0

- Full-bleed cinema UI (documented media exception): top nav, backdrop hero,
  poster shelves, Movies/Series/show-detail, Library, Search, My List, Settings
  and an immersive player; bottom tab bar on phones and touch-sized controls.
- HLS via native playback (Safari/iOS) or a lazily loaded `frontend/hls.min.js`
  (hls.js 1.7.4, Apache-2.0); `/transcode` fallback. Settings → Media engine
  shows the encoder candidates/reasons, hardware decode, active streams and a
  “Test hardware encode” refresh.
- New `data-testid` coverage, event delegation (no `window._vs*` globals or
  inline `onclick`), `ctx.notifications`/`ctx.progress`/`ctx.modal.confirm`,
  lazy posters and incremental grids.

### Android 1.4.0

- Uses `/api/videos/hls` for remux/transcode and multi-audio files, ExoPlayer
  native HLS, absolute resume mapping, far-seek/audio/quality session restarts,
  decoder fallback (`DefaultRenderersFactory.setEnableDecoderFallback(true)`),
  resume merge from `/resume/all`, server My List/options helpers; player stats
  show Direct/HLS + NAS encoder.

### Tests

- `tests/test_video_engine.py` (47 checks), `tests/test_video_hls.py` (13),
  `tests/test_video_library.py` (11), `tests/test_video_api.py` (17: isolated
  server, HLS playlist/segment/token/owner, resume delete, per-user My List,
  auth/scope), router smoke 139/139, Android `testDebugUnitTest` green.

### File map

| Concern | File |
|---|---|
| Engine/probe/builders | `backend/services/video_engine.py` |
| HLS sessions | `backend/services/video_hls.py` |
| Endpoints | `backend/routers/video_station.py` |
| Shutdown hook | `backend/main.py` |
| Tests | `tests/test_video_{engine,hls,library,api}.py` |
| Package UI | `ethos-packages/ethos-video/frontend/app.js` + `hls.min.js` |
| Android | `ethos-video/.../playback/{PlaybackManager,PlaybackPlan,PlayerService}.kt` |

## Status
- [x] Phase 1 — docs + metadata service + API enrichment + /home endpoint
- [x] Phase 2 — frontend home/movies/series + player subtitle picker
- [x] Phase 3 — E2E tests, production restart
- [x] Phase 4 — ethos-video Android app (separate repo)
- [x] Phase 5 — playback engine (direct/remux/transcode), audio tracks,
      quality presets, settings panel
- [x] Phase 6 — TMDB metadata + local posters (display-only, no renames)
- [x] Phase 7 — localized metadata (language setting), original-size artwork,
      IMDb exact match, genres/cast/runtime details
- [x] Phase 8 — cinema UI 2.0, HLS sessions, VA-API hardware decode/encode,
      media-engine diagnostics, per-user My List, Android HLS parity

## Verification log
| Date | What | Result |
|---|---|---|
| 2026-09-26 | Parser unit checks + isolated-instance API/UI E2E (`:9007`) | see below |
| 2026-09-27 | Phase 5 on isolated `:9008`: remux/transcode pipes, audio tracks, settings UI (Playwright) | all pass; production restarted on :9000 |
| 2026-09-29 | Phase 6 TMDB: service + endpoints + local artwork cache, isolated E2E (`:9010`) | see below |
| 2026-10-04 | ethos-video crash fix (HomeScreen negative padding → `offset`, v1.1.2 built + served via `/api/app/version?app=video`); TMDB batch refresh fail-fast on HTTP 401/403 (one warning + abort instead of per-title spam, see app_events 2026-10-01) | APK green; backend change pending production restart |
| 2026-10-04 | Poster-wall grid views (frontend-only): movie/series/browse/search grids switched to 2:3 TMDB-poster-first cards with always-visible titles; `node --check` green | done; no backend change, no restart needed |
| 2026-10-05 | Poster rows (frontend-only): Home rows (Continue Watching / Recently Added) + season rows in show detail switched to the same 2:3 TMDB-poster cards as the movies grid (CW keeps the resume bar + ✕; hover panel gains 🔄); browse/search grid cards gain 🔄 too; `node --check` green | done; no backend change, no restart needed |
| 2026-10-05 | ethos-video app v1.1.5 (separate repo, first commit pushed to SyncHot/ethos-video — an earlier stray `git init` in `~/apps/compose/` with a single accidental root commit was removed): home “Kontynuuj oglądanie” row switched to 2:3 TMDB poster cards with the resume bar on the poster (like the movies grid); APK built, update manifest served via `/api/app/version?app=video` (version_code 8, sha 116ae9f…) | APK green; phones pick it up on next update poll |
| 2026-10-12 | Phase 7: language setting + original-size artwork + `/find` IMDb exact match + genres/cast/runtime details; gated candidate picking (no blind top-hit); stale-cache in-place upgrade on batch refresh; `_sanitize_tmdb` partial-patch fix (auto_fetch no longer reset by key-only saves); frontend language input + genre/cast display; `tests/test_tmdb.py` 34/34 incl. live TMDB | all green; backend change pending production restart |
| 2026-10-04 | HW-accel re-check after kernel update (rebooted into 7.0.0-38-generic): re-ran the `_qsv_available()` h264_qsv probe, h264_vaapi/hevc_vaapi encode probes (default / `-vaapi_device renderD128` / `-hwaccel vaapi` paths), `vainfo` | **No change — kernel update did not fix encode.** QSV still `MFX session: -9` (oneVPL *implementation* still missing; only `libvpl2` 2.16 dispatcher + ffmpeg built `--disable-libmfx`). VA-API encode still fails in ffmpeg 8.0.1 at the `auto_scale` filtergraph (`rc=218`, same signature as above) even though `vainfo` shows iHD 26.1.2 exposing `VAEntrypointEncSliceLP` for H264/HEVC/VP8/VP9 → FFmpeg 8.0.1 userspace bug, not kernel/driver. Live `/api/videos/status` (fresh probe, server restarted post-reboot): `{"ffmpeg":true,"ffprobe":true,"qsv":false}`. HW **decode** (browser playback) unaffected. Remux path (library's h264 MKV + AC3/EAC3) still needs no video encoding. Fixes to pursue: install Intel oneVPL runtime (then QSV auto-detected after restart); wait for a patched FFmpeg ≥8.1 for the VA-API encode path |
