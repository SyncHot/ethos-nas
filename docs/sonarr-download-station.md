# Sonarr/Radarr ↔ Downloads Integration

Goal: Make the EthOS NAS Downloads app serve as the qBittorrent-compatible
download client for Radarr (and later Sonarr), replacing rdtclient.

## Architecture

```
Radarr / Sonarr
    │  qBittorrent API (HTTP)
    ▼
EthOS NAS Downloads  (/api/downloads/qb/*)
    │  internal debrid engine (AllDebrid / RealDebrid / TorBox)
    ▼
AllDebrid (debrid provider)
```

- Radarr/Sonarr are configured with a **qBittorrent** download client pointing
  at the ethos-nas host, with a **dedicated API key** (not the ethos JWT).
- The qBittorrent endpoints translate qBittorrent requests into the existing
  Downloads task engine (magnet → debrid → HTTP download).
- Completed downloads land in the same folder Radarr/Sonarr import from.

## API Key Auth

- `POST /api/downloads/qb/auth/login` — accepts qBittorrent's form-encoded
  `username`/`password`, returns a `SID` cookie (or 200).
- All other `/api/downloads/qb/*` endpoints require the `SID` cookie **or**
  a `?token=` query param (the existing public-path JWT-free mechanism).
- The API key is stored in `downloads_config.json` under `qb_api_key`.
- Default: auto-generated on first access if empty.

## Endpoints (qBittorrent v2 API subset)

| qBittorrent endpoint | Mapping |
|---|---|
| `POST /qb/auth/login` | validate API key → set SID cookie |
| `POST /qb/auth/logout` | clear SID |
| `GET /qb/app/version` | return `ethos-nas 1.0.0` |
| `POST /qb/torrents/add` | parse `magnet` or `torrents` (base64 .torrent) → create Downloads task |
| `GET /qb/torrents/info` | list all tasks as qBittorrent torrent objects |
| `POST /qb/torrents/delete` | delete task(s) |
| `POST /qb/torrents/pause` | pause task |
| `POST /qb/torrents/resume` | resume task |
| `GET /qb/sync/maindata` | return `{torrents: [...], ...}` for Radarr/Sonarr polling |
| `GET /qb/transfer/info` | aggregate speed/progress |
| `GET /qb/torrents/categories` | list unique categories |
| `GET /qb/torrents/properties?hash=` | single-torrent `{hash, save_path, seeding_time}` (NzbDrone: IsTorrentLoaded + import path) — 404 if unknown |
| `GET /qb/torrents/files?hash=` | file list with names relative to the content dir (NzbDrone GetImportItem) — 404 if unknown |
| `POST /qb/torrents/setCategory` | set `_qb_category` on task(s) (post-import relabel) |
| `POST /qb/torrents/setShareLimits`, `/topPrio`, `/setForceStart` | no-op stubs (debrid engine doesn't seed/queue) — return empty text like real qBittorrent |

All of the above exist on both the base (`/api/downloads/qb/api`) and v2
(`/api/downloads/qb/api/v2`) routers. Radarr probes `/api/v2/app/webapiVersion`
and uses the V2 proxy when it answers (it does — `2.0`).

## NzbDrone compatibility notes (verified against Sonarr/Radarr source)

- **State strings must be valid qBittorrent states** — NzbDrone switches on the
  exact string; unknown values are treated as "downloading" with a warning.
  Mapping: waiting→`queuedDL`, debrid_selecting→`metaDL`, completed→`uploading`
  (NzbDrone maps uploading/stalledUP/… → Completed, which triggers import),
  paused→`pausedDL`, canceled→`pausedUP`.
- **Torrent object field names are the official v2 snake_case** (`save_path`,
  `content_path`, `ratio_limit`, `last_activity`, …) — NzbDrone's model only
  parses those names.
- `/app/preferences` reports `dht: true` — otherwise NzbDrone rejects magnet
  links without trackers ("Magnet Links without trackers not supported if DHT
  is disabled"). The debrid engine doesn't use BT networking, so this is safe.
- Radarr polls `GET /torrents/info?category=movies` for progress — the v2
  wrapper must forward `hashes` **and** `category` to the shared handler
  (a previous version mis-bound `category` into the `hashes` slot, so Radarr
  always saw an empty torrent list and showed no download progress).
- Import path resolution: NzbDrone combines `/torrents/properties.save_path`
  with the first subdirectory of `/torrents/files[0].name`. `_task_content()`
  mirrors where phase 3 writes files (`<base>/<torrent-name>/…` for magnets,
  file `dest` wins once set).

## Status

- [x] Plan
- [x] Backend: qBittorrent compat layer (`backend/services/qb_compat.py`)
- [x] Backend: API key auth (public path + SID cookie)
- [x] Frontend: Settings tab shows QB API key + Radarr/Sonarr connection info
- [x] Fix (2026-10-09): v2 `torrents/info` category filter, valid state strings,
      snake_case field names, `dht: true`, added `torrents/properties` +
      `torrents/files` + setCategory/setShareLimits/topPrio/setForceStart
      (verified against an isolated instance on :9002 replaying Radarr's exact V2 calls)
- [x] Fix (2026-11-14): `torrents/add` passed a bare dict to `add_task()` and returned
      500 for every grab; Real-Debrid now takes URLs from `links[]`; destinations are
      path-scoped; qB key is admin-only with a rotate endpoint. Covered by
      `tests/test_downloads.py` and `tests/test_qb_compat.py` (see `docs/download-station.md`)
- [ ] **User action: restart ethos-nas server** (Settings → Restart, or `sudo systemctl restart ethos-nas`) — required for the fix above to take effect
- [x] Radarr: repoint download client to ethos-nas (done — config verified in radarr.db:
      host 172.18.0.1:9000, URL base `/api/downloads/qb`, category `movies`)
- [ ] Sonarr: install + configure
- [ ] Test: end-to-end download via Radarr (grab → progress in movie page → import)

## After Restart — Repoint Radarr

1. Restart the server (Settings → Restart or `sudo systemctl restart ethos-nas`)
2. Open Downloads → Settings → copy the **API Key** (auto-generated on first restart)
3. In Radarr: Download Clients → edit the rdtclient entry:
   - **Host**: `172.18.0.1` (docker host gateway, reachable from Radarr container)
   - **Port**: `9000`
   - **URL Base**: `/api/downloads/qb`
   - **Username**: `admin`
   - **Password**: the API key from step 2
   - **Category**: `movies`
   - **Implementation**: qBittorrent
4. Test: grab a movie in Radarr and watch it appear in Downloads

## Notes

- rdtclient is **not** used in this integration. It can be removed later.
- The debrid provider is already configured (AllDebrid) in `downloads_config.json`.
- Radarr import path: needs to match the Downloads `download_dir`
  (currently `/mnt/SharedDrive/torrents/downloads`).
- Sonarr is not yet installed; will be a docker-compose addition.
