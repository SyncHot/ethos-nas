# Music catalogs — Polish radio and podcasts

Status: implemented 2026-11-14.

The Radio and Podcasts tabs used to ship 38/20 hardcoded entries in three places
(`RadioModels.kt`, `PodcastModels.kt`, `music.js`). The catalogs are now generated
once into the NAS repository and read at request time; both clients consume the
API and bundle a copy for offline/first-launch use.

## Data files

| File | Schema | Count (2026-11-14) |
|---|---|---|
| `backend/radio_stations.json` | v2: `version, generated_at, source, categories[], stations[]` | 150 |
| `backend/podcast_catalog.json` | v1: `version, generated_at, source, categories[], podcasts[]` | 130 |

### Station fields

`id` (16-hex SHA-256 prefix of `stream_url`), `name`, `category`, `stream_url`,
`tags[]`, `city`, `language`, `logo`, `codec`, `bitrate`, `hls`, `homepage`,
`verified`, `verified_at`.

Categories are fixed and ordered: `Publiczne, Komercyjne, Regionalne, Religijne,
Muzyka, Kultura, Sport`. A station gets the first matching category:
Religijne → Sport → Kultura → Regionalne (city/brand keywords) → Publiczne →
Muzyka → Komercyjne. Only categories with at least one station are returned by
the API.

### Podcast fields

`id` (16-hex SHA-256 prefix of `rss_url`), `name`, `author`, `category`,
`description`, `rss_url`, `cover_url`, `language`, `episode_count`,
`last_episode`, `itunes_id`, `popularity`, `verified`, `verified_at`.

**IDs are identity.** `id = sha256(rss_url)[:16]` on the server, in the Android
app and in the web client; changing a feed URL silently creates a second
subscription and loses the old one’s episodes. The pinned 20-entry list in the
builder preserves the original URLs on purpose.

## Generators

```bash
cd /opt/ethos-nas
.venv/bin/python scripts/build_radio_catalog.py --limit 150
.venv/bin/python scripts/build_podcast_catalog.py --limit 130 --validate-limit 420
.venv/bin/python scripts/export_android_catalogs.py
.venv/bin/python scripts/verify_catalogs.py
```

### Radio (`build_radio_catalog.py`)

- Source: radio-browser.info mirrors (`countrycode=PL`, `hidebroken=true`,
  popularity order), plus every station already in the repo file (favourites on
  devices are keyed by id, so existing entries are never rewritten).
- Name cleanup strips codec/bitrate decorations (`(AAC+)`, `128 Kbps`).
- Over-selects candidates, probes every stream (Range GET, expects audio or an
  HLS playlist), keeps pinned entries and drops dead new relays.
- De-duplicates by a brand key: brackets/bitrate tokens removed, prefixes
  (`polskie radio`, `radio`, `radia`, `pr`), suffixes (`fm`, `pl`, `polska`, …)
  and whitespace dropped, repeated letters collapsed — so “Anty Radio” =
  “Antyradio” and “Polskie Radio Trójka” = “Trójka”.
- Records `verified`/`verified_at`; “ICY 200 OK” responses (HTTP/0.9-style
  SHOUTcast headers) count as broken because ExoPlayer cannot parse them.

### Podcasts (`build_podcast_catalog.py`)

- Source: iTunes Search/Lookup restricted to `country=PL`, plus Apple’s PL
  top-100 chart (resolved to feeds via a batched lookup).
- Round-robin ordering across search terms so one category cannot fill the list.
- Every candidate feed is fetched and parsed with
  `services/podcast_service.parse_feed`; feeds with no playable episode are
  skipped.
- Polish filter: accepted when the feed declares `<language>pl…`, or the title /
  description contains Polish diacritics or Polish function words. Pinned
  entries bypass the filter (BBC 6 Minute English is intentional).
- Non-Latin scripts are rejected; duplicate names are collapsed.
- Covers prefer the feed’s own artwork, with the iTunes artwork as fallback.

### Android assets

`scripts/export_android_catalogs.py` copies both files into
`ethos-music/app/src/main/assets/{radio_catalog.json,podcast_catalog.json}`.
`CatalogStore` loads the assets on first launch, caches the last server payload in
`filesDir/catalog_cache.json` and refreshes from the API once per day (or via
Settings → „Odśwież katalogi”).

## API

- `GET /api/music/radio/stations` (auth, read-only, re-read per request — no
  restart needed after regenerating).
- `GET /api/music/podcasts/catalog?q=&category=&sort=&limit=&offset=`.
- `GET /api/music/radio/state` / `PUT /api/music/radio/state` — per-user favourite
  and recently-played station ids (`music_library.json`, `json_store`, recents
  capped at 8, favourites at 500).

There is no write API for stations/podcasts: edit the repository JSON through the
generators, run `verify_catalogs.py`, commit.

## Permissions

The NAS service runs as **`ethos-nas`**, while the generators run in the developer
shell (`marcin`). Catalog files must be world-readable (`0644`) — `mkstemp` creates
`0600`, which made every catalog request answer **HTTP 500**
(`PermissionError: ... podcast_catalog.json` in `events.jsonl`). `write_json()`
now chmods the file, `verify_catalogs.py` fails on a non-world-readable file, and
`/api/music/search?type=all` degrades optional sections to empty instead of
failing the whole search.

## Verification

- Offline contract checks: `scripts/verify_catalogs.py` (unique/stable ids,
  http(s) URLs, known categories, minimum counts, pinned feeds present).
- API: `tests/test_music_catalog_api.py` (isolated instance, auth, filters,
  search, pagination, radio state).
- Unit: `tests/test_music_search_service.py`.
- Web smoke: Music → Radio tab lists all stations, search “trojka” → Trójka.
- Android: `./gradlew :app:testDebugUnitTest` (catalog parsing + id stability).

## Refresh cadence

Catalogs are snapshots; streams and feeds do die. Regenerate quarterly or when a
station/feed is reported broken. `verify_catalogs.py` warns about unverified
streams so a refresh can be prioritised.
