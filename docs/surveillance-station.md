# Cameras — Spec & Progress (AI handoff)

Camera surveillance app for EthOS NAS: manage RTSP / HTTP-MJPEG cameras,
watch them live in a grid, and get motion-detection events into the Notification Center.

**This file is the resume point.** A new session should read this doc first, check the
Status section below, and continue from the first unchecked item. Update the checkboxes
and Verification Log as work lands.

## Status (update as you go)

- [x] Phase 1 — Plan written (this doc)
- [x] Phase 2 — Backend: `camera_service.py` + `surveillance.py` router, mounted in main.py
- [x] Phase 3 — Frontend: `apps/surveillance.js` + wiring (registry / window-manager / app.js / desktop-layout)
- [x] Phase 4 — E2E test on isolated instance (`tests/test_surveillance.py`) — all checks pass
- [x] Phase 5 — Commit & push, verification log below
- [x] Phase 6 — MP4 clip recording (start/stop per camera, Clips tab) — all checks pass
- [x] Phase 7 — Network discovery (ONVIF WS-Discovery + RTSP/HTTP subnet scan) — all checks pass

## Scope (MVP)

**In:** camera CRUD with connection test; live MJPEG grid (ffmpeg re-mux); snapshot
grab + disk cache; per-camera motion detection (frame-diff thread) → persistent events +
`emit_event()` into Notification Center; Events tab.

**Out (later):** scheduled/continuous recording with retention rotation,
PTZ control, multi-user camera permissions, WebSocket push for live status (polling is fine).
(Manual start/stop MP4 clip recording shipped in Phase 6; ONVIF discovery + subnet scan
shipped in Phase 7.)

## File map

| What | Where |
|---|---|
| Backend service (store + motion threads) | `backend/services/camera_service.py` (new) |
| Backend service (network discovery, stdlib-only) | `backend/services/camera_discovery.py` (Phase 7) |
| Backend router (all API logic) | `backend/routers/surveillance.py` (new) |
| Router mount (`/api/surveillance`) | `backend/main.py` — import + `app.include_router(...)` |
| Frontend app | `frontend/static/apps/surveillance.js` (new, ES module) |
| Renderer wiring | `frontend/static/app.js` — `import { renderSurveillancePage }` + `RENDERERS.surveillance` |
| Desktop icon / drawer entry | `frontend/static/core/registry.js` — `APP_REGISTRY.surveillance` (`📹`, id `surveillance`) |
| Window title map | `frontend/static/core/window-manager.js` — `APP_INFO.surveillance = ['📹', 'Cameras']` |
| Drawer ordering | `frontend/static/core/desktop-layout.js` — add `"surveillance"` to `DEFAULT_ORDER` (check how other apps are listed) |
| Persistence (auto-created under `settings.DATA_DIR`) | `surveillance.json` (cameras), `surveillance_events.json` (events, cap 200), `surveillance/snaps/`, `surveillance/events/` (JPEGs) |
| E2E test | `tests/test_surveillance.py` |

## Data model

`surveillance.json`:

```jsonc
{
  "version": 1,
  "cameras": [
    {
      "id": "cam_a1b2c3d4e5f6",        // prefix + 12 hex (uuid4)
      "name": "Front Door",
      "url": "rtsp://user:pass@192.168.1.50:554/stream1",   // rtsp|http(s) only
      "enabled": true,
      "motion": { "enabled": false, "interval_s": 5, "sensitivity": 0.3 },
      "created_at": "2026-07-11T12:00:00+00:00"
    }
  ]
}
```

`surveillance_events.json`: `{version, events: [{id, camera_id, camera_name, at (ISO), image (rel path or null)}]}` — newest first, capped at 200.

## API (`/api/surveillance`, JWT-protected like all /api/*)

| Method & path | Purpose |
|---|---|
| `GET /cameras` | list cameras (+ `last_error` if known) |
| `POST /cameras` `{name, url}` | add; 400 on bad scheme (rtsp/http/https only), empty name/url, dup URL |
| `PUT /cameras/{id}` | update any of name/url/enabled/motion; restarts motion thread if changed |
| `DELETE /cameras/{id}` | remove + stop threads + delete cached images |
| `POST /cameras/{id}/test` | ffmpeg probe (≤8 s) → `{ok, detail}` — never raises on unreachable cam |
| `GET /cameras/{id}/snapshot?w=480&refresh=` | JPEG; disk-cached per camera; `refresh=1` forces re-grab; 502 with detail if grab fails |
| `GET /cameras/{id}/live` | `multipart/x-mixed-replace` MJPEG via ffmpeg (`-f mjpeg -q:v 6 -r 5`, `-rtsp_transport tcp` for rtsp); process killed on client disconnect |
| `POST /cameras/{id}/record` | start MP4 recording (libx264, no audio) → `surveillance/clips/<ts>_<camid>.mp4`; 409 if already recording, 507 if clips dir > 4 GiB cap |
| `DELETE /cameras/{id}/record` | stop; SIGINT lets ffmpeg finalize the MP4 (moov atom); returns path + size_bytes |
| `GET /cameras/{id}/record` | `{recording, started_at?, size_bytes?}` — also surfaced as `recording: bool` on every camera in `GET /cameras` |
| `GET /clips?camera_id=` | recorded clips (newest first, cap 50): id, camera_id, started_at, size_bytes |
| `GET /clips/{clip_id}/download` | the MP4 (`video/mp4`, Range-capable → `<video>` seeking works); clip ids are regex-validated, no path traversal |
| `DELETE /clips/{clip_id}` | delete one clip |
| `GET /events?limit=&camera_id=` | motion events (newest first) |
| `DELETE /events/{id}` / `DELETE /events` | delete one / clear all (+ their images) |
| `POST /discover` `{timeout_s=3, scan_subnets=true}` | search the LAN for cameras → `{results:[{ip,onvif,xaddr,rtsp,ports,suggested_url}], scanned:{network,hosts}, onvif_replies, duration_s}`; runs in a worker thread, never raises (empty results are valid) |

Auth note: `<img>` tags can't send headers — frontend builds URLs with `&token=<jwt>`
(same pattern as video-station.js `_streamUrl`). Do **not** add these paths to `public_paths`.

## Motion detection design

- One daemon thread per camera with `motion.enabled` (started on app boot for enabled cams,
  and on create/update). Thread loop: sleep(interval) → grab small JPEG (`scale=320:-1`,
  ffmpeg timeout ~8 s) → Pillow grayscale resize to 160×90 → mean abs diff vs previous frame.
- `diff > sensitivity` (default 0.3, range 0.05–0.9) **and** ≥10 s since last event for this
  camera → record event + save snapshot JPEG (`surveillance/events/<ts>_<camid>.jpg`) +
  `emit_event("warning", "Motion detected — <name>", source="surveillance")`.
- Consecutive grab failures (3×) → set `last_error` on camera, keep retrying; never crash the app.

## ffmpeg commands (arg lists only, no shell=True)

```
snapshot:  ffmpeg -hide_banner -loglevel error [-rtsp_transport tcp] [-stimeout 5000000]
           -i <url> -frames:v 1 -q:v 4 -y out.jpg          (subprocess timeout ~8 s)
live:      ffmpeg ... -i <url> -an -f mjpeg -q:v 6 -r 5 pipe:1   (stdout → StreamingResponse)
```

## Frontend design (`apps/surveillance.js`)

- `renderSurveillancePage(winBody)` — three tabs: **Live** | **Cameras** | **Events**.
- Live: responsive grid; tile = MJPEG `<img src="/api/surveillance/cameras/<id>/live?token=…">`,
  name, status dot (green if last snapshot ok / red on error), motion badge. Click → modal with
  big live view + "Snapshot" button (fetches `/snapshot` and shows it).
- Cameras: table (name, url, enabled toggle, motion settings inline or in edit modal) +
  **Add camera** modal: name, RTSP/HTTP URL, **Test connection** button → shows ok/fail detail.
- Events: rows with thumbnail (`/events/<id>/image?token=…` — add this route if needed),
  camera name, relative time; "Clear all" button.
- Conventions: `apiFetch` for JSON, `showToast`/`addNotification` for feedback,
  `emit_event` only on the backend side. No custom notification UI.

## Test plan (`tests/test_surveillance.py`, isolated instance)

Self-contained: the test starts its own throwaway uvicorn with a temp `ETHOS_DATA_DIR`
(never touches :9000 or production data) and a local HTTP-MJPEG "camera". Port is picked
freely at runtime (the AGENTS.md-conventional :9001 was occupied by a docker container on
this host); override with `ETHOS_TEST_PORT` if needed.

- Part A — API E2E: login → CRUD cameras (add/dup-reject/update/delete, 404s) →
  `POST /test` against dead URL → `{ok:false}` → spin up local **HTTP-MJPEG source**
  (Python http.server streaming ffmpeg `-f image2pipe -c:v mjpeg` output as multipart;
  ffmpeg has no RTSP *server* mode, so HTTP-MJPEG is the test transport) → add camera with it →
  `GET /snapshot` returns JPEG magic bytes (`\xff\xd8`) → `GET /live` first chunk contains
  `multipart/x-mixed-replace` + a JPEG part → events list shape + clear.
- Part B — UI (Playwright, auto-skip if chromium missing): open app → tabs render → add camera
  via modal with Test connection → tile appears in Live grid.

## Implementation notes (deviations from the original plan)

- **Live stream is re-wrapped to multipart in Python.** ffmpeg's `-f mjpeg pipe:1`
  emits *raw* concatenated JPEGs, which browser `<img>` tags cannot render. The service
  splits ffmpeg's raw MJPEG stdout into frames (SOI `\xff\xd8` … EOI `\xff\xd9`) and wraps
  each in a `multipart/x-mixed-replace; boundary=frame` part — the format browsers render.
  Works uniformly for RTSP and HTTP-MJPEG sources. (Verified: raw MJPEG over HTTP also
  hangs ffmpeg's demuxer, so multipart is the only viable test transport.)
- **Event image route added:** `GET /events/{id}/image` (410 if the JPEG was evicted).
- **Motion threads start at boot** via a lifespan hook in `main.py`
  (`start_all_motion_threads()`), stopped on shutdown.
- **Connection test never raises** — unreachable camera → `{ok:false, detail}` with the
  ffmpeg error line (≤8 s timeout).
- **Recording re-encodes to libx264** (`-preset veryfast -crf 28`, no audio) so both RTSP
  H.264 and HTTP-MJPEG sources yield universally playable MP4s; `-movflags +faststart` for
  web playback. Stop = SIGINT (graceful finalize), kill after 6 s. A 4 GiB cap on the clips
  dir refuses new recordings until old clips are deleted.
- **Bug fixed along the way:** `refreshAll()` never called `_loadEvents()`, so the Events tab
  always showed its empty state — events now load with cameras on every poll.

## Network discovery design (Phase 7)

`camera_discovery.py` — pure standard library, no root required:
- **ONVIF WS-Discovery**: UDP multicast `Probe` for `dn:Device` to `239.255.255.250:3702`
  (second burst ~1 s in); compliant cameras reply with their `XAddrs` endpoint.
  Replies are parsed leniently (regex on `<XAddrs>`) because real-world envelopes vary.
- **Subnet scan**: local network detected via UDP-connect trick + `ip route get` /
  `ip -4 addr` (arg lists, no shell); subnets >300 hosts are reduced to a bounded window
  around the local IP. Threaded TCP connect (64 workers) against ports 554/80/8080;
  open port 554 is confirmed as RTSP via an `OPTIONS` request expecting an `RTSP/1.x`
  status line.
- **Merge**: results sorted by IP; each row carries a `suggested_url` (rtsp://ip:554/ when
  RTSP or ONVIF, else http://ip[:port]/) — the frontend pre-fills the Add-camera modal
  from it (`_presetCam`, one-shot).
- Frontend: **🔍 Search network** button in the Cameras tab → busy state → results table
  (IP / type badges ONVIF·RTSP·HTTP / ports / suggested URL) with per-row **Add**.
- Testability: `scan_ips(ips, ports, rtsp_ports)` accepts explicit IPs/ports so tests run a
  fake RTSP responder + HTTP server on 127.0.0.1; `parse_onvif_response` is a pure function.

## Verification Log

- 2026-07-11 — Phase 2: `camera_service.py` + `surveillance.py` written; mounted in
  `main.py` (import, `include_router`, lifespan start/stop of motion threads). TestClient
  smoke test: 401 unauth, CRUD + validation (bad scheme 400, empty name 400), `/test`
  against dead URL → `{ok:false}` in <1 s, events shape, delete + 404s — all pass.
- 2026-07-11 — Phase 3: `apps/surveillance.js` (Live/Cameras/Events tabs) + wiring in
  `app.js`, `registry.js`, `window-manager.js`, `desktop-layout.js`. All files pass
  `node --check`; modal conventions (`showFormModal`, `.ethos-modal-input`) reused from
  storage.js.
- 2026-07-11 — Phase 4: `tests/test_surveillance.py` run → **all 24 checks PASS**
  (Part A API: login, validation, add/dup-reject, list+last_error, connection test ok in
  ~0.6 s, snapshot JPEG, live multipart MJPEG with frames, motion event recorded within
  the 30 s window + event image JPEG, delete/clear events, disable/delete camera;
  Part B UI: PAM login, tabs render, add-camera modal → row appears, live tile `<img>`
  targets `/live?token=`, events tab renders, zero JS page errors).
- 2026-07-11 — Phase 5: committed & pushed (`94423c7`); production server restarted on :9000.
- 2026-07-11 — Phase 6 (recording): `start/stop_recording`, `list_clips`, clip download/
delete in `camera_service.py`; 5 new endpoints in the router; lifespan shutdown stops all
recordings (`main.py`); frontend: ● Record / ■ Stop per camera row + 🎬 Clips tab (play /
download/delete). E2E extended with 9 recording checks → **all 34 checks PASS**
(start 201, double-record 409, stop finalizes a valid MP4 >1 KB with ftyp box, list/download/
delete round-trip, not-recording 409; UI: record button on camera row, Clips tab renders,
zero JS errors).
- 2026-09-26 — Phase 7 (network discovery): `camera_discovery.py` (ONVIF WS-Discovery +
subnet scan, stdlib-only) + `POST /api/surveillance/discover`; frontend 🔍 Search network
button with results table and per-row Add (pre-fills the camera modal). E2E extended with
7 Part C checks (XAddr parsing, fake RTSP/HTTP 127.0.0.1 scan + OPTIONS confirmation, API
shape incl. real LAN scan) → **all 44 checks PASS** (A: 27, C: 7, B: 10).
- 2026-09-26 — fix(camera): ffmpeg 8 removed `-stimeout` as a CLI option, so every RTSP
probe/snapshot/live grab aborted with "Error splitting the argument list" before reaching
the camera (E2E missed it — its source is HTTP-MJPEG). Replaced with the global
`-rw_timeout`; `test_connection` now also appends an actionable hint on RTSP 404 (server
answered, no stream at that path → use the exact URL from the vendor app, e.g. Eufy
Settings → Storage → NAS/RTSP). Full suite re-run: **all 44 checks PASS**.
