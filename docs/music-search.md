# Music search — ranking, unified results, suggestions

Status: implemented 2026-11-14 (web + Android).

The Music app used to send the raw query to yt-dlp and render whatever YouTube
returned. That produced covers, karaoke tracks and 10-hour loops mixed with the
official upload, and nothing outside YouTube (podcasts, radio, the downloaded
library) could be found. Search is now a NAS-side concern with a single API for
both clients.

## Pieces

| Layer | File | Responsibility |
|---|---|---|
| Normalisation + ranking | `backend/services/music_search.py` | Polish folding, `Artist - Title` parsing, noise stripping, scoring, de-duplication, catalog matching, fuzzy suggestions |
| HTTP | `backend/routers/music.py` (`_search_cached`, `_search_tracks`, `_search_all`, `search_suggest`) | yt-dlp cache (10 min, LRU 200, single-flight), pagination, one response with all sections |
| Web | `frontend/static/apps/music.js` (`viewSearch`) | debounce 300 ms, history, suggestions, filter chips, “Więcej wyników” |
| Android | `data/SearchRepository.kt`, `data/SearchNormalizer.kt`, `ui/screens/SearchScreen.kt` | 300 ms debounce, cancellable HTTP, local suggestions, LRU cache, history |

`SearchNormalizer.fold()` (Kotlin) and `music_search.fold()` (Python) must stay
identical: fold lowercase, strip Polish diacritics (`ą ć ę ł ń ó ś ź ż`),
replace punctuation with spaces and collapse whitespace. Tests:
`tests/test_music_search_service.py`, `app/src/test/.../SearchNormalizerTest.kt`.

## Endpoints

### `GET /api/music/search`

| Param | Default | Meaning |
|---|---|---|
| `q` | required | free text, 1–200 chars |
| `limit` | 25 | 1–100 |
| `offset` | 0 | 0–500 (pagination) |
| `type` | `track` | `track` = original response shape; `all` = everything below |

`type=track` keeps the old contract and adds `total`, `has_more`, `offset`,
`cached`:

```json
{"query": "doda", "results": [...], "total": 31, "has_more": true, "offset": 0, "cached": false}
```

`type=all` returns one response for the whole search screen:

```json
{
  "query": "kryminatorium",
  "tracks":    {"items": [...], "total": 31, "has_more": true, "cached": false},
  "podcasts":  {"items": [...], "total": 2},
  "radio":     {"items": [...], "total": 0},
  "library":   {"items": [...], "total": 0},
  "playlists": {"items": [{"id": "…", "name": "…", "tracks_count": 12}], "total": 1}
}
```

Track shape is unchanged (`id, title, artist, duration, thumbnail`). Library
items use the `library` shape (`name, title, artist, thumbnail, duration,
video_id, size, modified, url`). Podcast items are catalog entries
(`id, name, author, category, cover_url, rss_url, episode_count, last_episode`),
radio items are station entries (`id, name, category, stream_url, tags, city,
logo, codec, bitrate, hls, homepage`).

### `GET /api/music/search/suggest?q=&limit=8`

Fast, no yt-dlp: catalogs + downloaded library + playlist names, round-robin so
one source cannot crowd out the others. Each item is
`{type: podcast|radio|playlist|library, id, title, subtitle, image, …}`.

### `GET /api/music/podcasts/catalog?q=&category=&sort=&limit=&offset=`

`sort=popular` (catalog order), `name`, `recent`. Response:
`{categories, items, total, has_more, generated_at}`.

### `GET /api/music/radio/stations?q=&category=`

Backwards compatible: `{categories, stations, total, generated_at}`. `categories`
now contains only categories that actually have stations.

## Ranking rules (`rank_tracks`)

1. Score by folded query vs folded title/artist:
   exact/prefix/substring, every query token present, artist in either slot,
   prefix/fuzzy tolerance for tokens ≥ 5 chars.
2. Boost official uploads: `… - Topic` channels, VEVO, “official”.
3. Penalise covers/karaoke/instrumental/loops and long durations (> 15 min).
4. Hard-drop karaoke, instrumentals, backing tracks, 1/8/10-hour loops, nightcore
   and 8D audio unless the query asks for them.
5. Drop covers only when a clearly better (≥ 1.4× score) non-cover result exists.
6. De-duplicate by video id, then `(core title, artist)` (the `- Topic` suffix is
   stripped, `feat.` parts ignored), then by identical folded title. Highest score
   wins, yt-dlp order is the tie-breaker.
7. Slice `[offset, offset+limit)`.

If the ranked list is too small and the query had diacritics, the folded variant
is searched too and merged (`query_variants`).

## Caching

- yt-dlp results: 10 min TTL, 200 entries, keyed by `(folded query, fetch)`.
- Concurrent identical queries share one yt-dlp call (`asyncio.Lock` per key).
- Library listing: 20 s TTL (search/suggest run per keystroke), invalidated on
  delete.
- Android: 10 min LRU 20 for full search results; history capped at 12.

## Client behaviour

- Web: input → suggestions after 120 ms, search after 300 ms; Enter submits
  immediately; results are painted in place to keep focus; the old in-flight
  response is ignored via a sequence counter.
- Android: `SearchRepository` cancels the previous coroutine, and
  `NasMusicClient.getJsonCancelable` cancels the OkHttp call — a fast typist does
  not keep the NAS busy with stale yt-dlp runs.

## Follow-ups

- Optional SQLite FTS index once the library grows large (currently a directory
  scan, fine for thousands of files).
- Radio-browser live search (worldwide) as a separate, SSRF-guarded proxy.
