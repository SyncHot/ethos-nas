# Video metadata providers (keyless + optional TMDB)

Status: implementation in progress. Owner: core (metadata service) + `ethos-video`
package 2.1.0 + Android 1.5.0.

## Goal

Make posters, ratings, overviews, genres and cast appear **without an API key**,
while keeping TMDB as an optional upgrade for Polish titles/overviews and the
best artwork. Files are never renamed or moved — metadata is display-only and
cached by absolute path.

## Why keyless is possible (and its limits)

There is no official, keyless movie metadata API with posters. OMDb, TMDB,
TheTVDB, Trakt and Fanart all require a key or client id; IMDb has no public API
and scraping is not acceptable.

| Source | Keyless | Kinds | Artwork | Text | Notes |
|---|---:|---|---|---|---|
| **Cinemeta** (`v3-cinemeta.strem.io`, Stremio) | yes | movie + series | poster + backdrop (Metahub) | English | Unofficial addon; may change; opt-out in Settings |
| **TVmaze** (`api.tvmaze.com`) | yes | series | poster | English | Official, CC BY-SA 4.0, ≥20 calls/10 s |
| **TMDB** | no | movie + series | poster + backdrop | Polish + others | Optional free key; preferred when present |
| Wikipedia REST | yes | both | page image only* | localized | *Film posters are usually non-free; not used v1 |
| iTunes Search | yes | movies | promotional | English | `media=movie` probes returned 0 results; not used |

Keyless mode is therefore **English** and depends on Cinemeta for movie posters.
If Cinemeta is unacceptable, choose TVmaze-only (series) or add a TMDB key; the
provider abstraction makes the switch a setting, not a code change.

## Provider chain

Configured as `tmdb.provider` in `video_station_config.json` (the `tmdb` section
name is historical and kept for API compatibility):

- `auto` (default): TMDB if a key exists → Cinemeta → TVmaze (TV only) → local
  ffmpeg thumbnail.
- `keyless`: Cinemeta → TVmaze (TV only).
- `cinemeta`: Cinemeta only.
- `tvmaze`: TVmaze only (movies get no metadata).
- `tmdb`: TMDB only; requires a key.
- `off`: no fetch; already cached metadata stays visible.

First successful provider wins. A provider error is logged and the next source is
tried; a clean “no match” is cached as a negative result for 14 days. Transient
errors (429/timeout/5xx) are **not** cached as misses.

## Configuration

```json
{
  "tmdb": {
    "provider": "auto",
    "api_key": "",
    "auto_fetch": true,
    "language": "pl-PL"
  }
}
```

- `api_key` is write-only from the client. `GET /config` returns only
  `api_key_set` and `api_key_masked` (last 4).
- Empty/missing `api_key` keeps the previous value; `clear_key: true` removes it.
- Invalid provider/language values keep the previous value.
- `auto_fetch` schedules one bounded refresh after app open (max 20 title groups
  per user per 6 h) through `task_registry`; it never blocks the response.
- `language` applies to TMDB only; keyless sources are English.

## API

All routes require a JWT and path scope (router-level), no new public paths.

| Method / path | Purpose |
|---|---|
| `GET /api/videos/metadata/status` | Masked config, provider mode, match counts by source, source health (cached) |
| `GET /api/videos/metadata/test` | Health check every configured source; keyless works without a key |
| `POST /api/videos/metadata/refresh` | Batch refresh; body `{path?, force?}`. `force` re-queries cached entries |
| `GET /api/videos/metadata/image?file=` | Locally cached artwork (new + legacy TMDB dir), MIME-aware |
| `GET /api/videos/tmdb/status`, `/tmdb/refresh`, `/tmdb/image` | Legacy aliases for package 2.0.x / Android 1.4.x |
| `GET /api/videos/tmdb/test` | Legacy TMDB-key-only test; 400 when no key |

`refresh` response:
`{titles_checked, titles_matched, files_matched, errors, skipped, kept, by_source,
aborted?}` plus `match` for a single path.

## Match shape

Library items keep the historical `tmdb` envelope and gain source fields:

```json
{
  "kind": "movie",
  "source": "cinemeta",
  "source_id": "tt0816692",
  "source_label": "Cinemeta (Stremio)",
  "source_url": "https://www.stremio.com/",
  "imdb_id": "tt0816692",
  "title": "Interstellar",
  "year": 2014,
  "rating": 8.7,
  "overview": "…",
  "genres": ["Adventure", "Sci-Fi"],
  "cast": ["Matthew McConaughey", "…"],
  "runtime": 169,
  "poster_file": "0123456789abcdef.jpg",
  "backdrop_file": "fedcba9876543210.jpg",
  "language": "en"
}
```

`poster_file`/`backdrop_file` are local filenames only; the window never hotlinks
an external image. `source_id` is the provider id in that source's namespace.

## Cache

- Matches: `<DATA_DIR>/video_metadata_matches.json`, `json_store` schema 2,
  `{version, matches: {absolute path: match}}`, capped at 4 000 paths.
- Legacy `<DATA_DIR>/tmdb_matches.json` is migrated once on first read; the old
  file is left untouched for rollback. Legacy entries get `source="tmdb"`.
- Misses: `<DATA_DIR>/video_metadata_misses.json`, 14-day TTL, 4 000 entries.
- Artwork: `<DATA_DIR>/metadata_images/` (generic) and `<DATA_DIR>/tmdb_images/`
  (legacy). Hash-named, atomic writes, 10 MB/image cap, image content-type check.
  A sweep removes files no longer referenced by any match.
- Match cache is global (metadata is not user-specific); config is per user.
  Existing TMDB matches remain visible to every account.

## Security and limits

- HTTPS only; exact host allowlist per provider; redirects validated before they
  are followed (`v3-cinemeta.strem.io`, `images.metahub.space`,
  `api.tvmaze.com`, `static.tvmaze.com`, TMDB's existing hosts).
- 15 s timeout, 4 MB JSON cap, 10 MB image cap; 429 honours `Retry-After` and
  backs off (max 300 s); a blocked source is skipped and the chain falls back.
- API keys never appear in logs, errors or API responses. TMDB errors are
  sanitized (no URL/query string).
- Image filenames are allowlisted (`[A-Za-z0-9._-]{1,80}`); no path traversal.
- Batch refresh is serialized by one async guard; auto-fetch shares it.

## Attribution / licensing

- **TVmaze:** CC BY-SA 4.0 — visible “TVmaze” link in Settings and docs.
- **Cinemeta:** unofficial Stremio addon — clearly labelled, switchable off.
- **TMDB:** “This product uses the TMDB API but is not endorsed or certified by
  TMDB.” Visible in Settings.
- Posters remain the property of their owners; they are cached for personal
  display in a self-hosted library, not redistributed.

## Tests

- `tests/test_video_metadata.py` — offline: matching gates, Cinemeta/TVmaze
  normalization, provider fallback, config, legacy cache migration, negative
  cache, grouping, image lookup/sweep, HTTP allowlist.
- `tests/test_video_api.py` — isolated server: metadata status/auth, provider
  off/tmdb-without-key rejection, image + legacy alias, traversal rejection.
- `tests/test_tmdb.py` — existing TMDB parsing/live section + config patches.
- CI registers `tests/test_video_metadata.py`; live smoke is an opt-in manual
  check (not required for CI).

## Status

- [x] Provider-agnostic core (`http`, matching, images, base)
- [x] Cinemeta provider (keyless movies/series, Metahub artwork)
- [x] TVmaze provider (official series fallback, CC BY-SA)
- [x] TMDB adapter (optional, backward compatible)
- [x] Versioned cache + legacy migration + negative cache
- [x] `/metadata/*` API + legacy `/tmdb/*` aliases
- [x] Bounded auto-fetch
- [x] Package 2.1.0 provider UI / attribution
- [x] Package 2.1.0 sandbox/browser E2E
- [x] Android 1.5.0 source label + release
- [x] Core 2.13.0 + package 2.1.0 signed releases

## Out of scope

- Manual “Fix match” picker (recommended next: search candidates + cached
  per-file override).
- Polish Wikipedia text fallback (keyless Polish; short extracts, more requests).
- Music/book metadata (this doc is Video Station only).
