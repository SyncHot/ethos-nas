# Cameras — spec & progress (AI handoff)

Camera surveillance for EthOS NAS: RTSP / HTTP-MJPEG cameras, a live HLS wall,
continuous or scheduled recording with retention, timeline playback with
range export, masked motion detection, and a Notification Center alert per
event. The desktop app is the `ethos-surveillance` package; the engine lives in
EthOS core so recordings keep running headless.

**This file is the resume point.** Read it first, continue from the first
unchecked item, and update the boxes + Verification Log as work lands.

## Status

- [x] Phase 0 — Authz (admin-only management, per-camera `viewers`), `json_store` v2
      migration, AES-GCM stream credentials, settings store
- [x] Phase 1 — `camera_runner`: one ffmpeg per stream (live HLS + 60 s archive +
      160×90 raw frames for motion), watchdog/backoff, day rollover
- [x] Phase 2 — Storage root + quota/retention janitor, recordings index, generated
      playback playlist, MP4 range export, archive snapshot
- [x] Phase 3 — Masked cell-grid motion detection with cooldown/schedule, event v2
      (score, recording range, reviewed) and Notification Center deep link
- [x] Phase 4 — `/ws/surveillance` status/event push
- [x] Phase 5 — Package 2.0.0 UI rewrite: shared shell, Live/Playback/Events/
      Cameras/Storage/Settings, HLS wall, timeline, zoom-free mask editor
- [ ] Phase 6 — ONVIF device info + PTZ (next)
- [ ] Phase 7 — Object detection via an optional Frigate adapter (out of scope for 2.0)

## Architecture

```
one ffmpeg per stream (CameraPipeline supervisor, restart 5/15/60 s)

  sub stream  -> fMP4 HLS window (1 s, 6 segments)  -> /api/.../hls/index.m3u8
              -> rawvideo 160×90 gray on stdout    -> MotionDetector (no extra decode)

  main stream -> fMP4 HLS archive, one directory per run:
                 <storage>/recordings/<cam>/<YYYY-MM-DD>/<HH-MM-SS>/{index.m3u8,*.m4s}

  Retention janitor (every 5 min): days, then oldest runs until the quota holds.
  Playback: generated HLS playlist over archive segments (hls.js) — no transcode.
```

- `-c:v copy` whenever the source is H.264 (HEVC for the archive), so a
  multi-camera install costs almost no CPU. MJPEG-only sources transcode at
  CFR 15 fps.
- Archive uses one run directory per ffmpeg start: `append_list` restarts
  segment numbering at zero and would overwrite older playlists.
- The live window is deleted/rotated by ffmpeg (`delete_segments`); the janitor
  never touches it.
- A run is only purged when its newest file is ≥ 5 min old and it is not the
  camera's newest run, so a write in progress is never deleted.

## Data model

`surveillance.json` (v2, `json_store`):

```jsonc
{
  "version": 2,
  "cameras": [{
    "id": "cam_0123456789ab",
    "name": "Front door",
    "enabled": true,
    "streams": {"main": "rtsp://host:554/main", "sub": "rtsp://host:554/sub"},
    "auth": {"user": "admin", "password_enc": "…AES-GCM…"},
    "record": {"mode": "off|continuous|schedule", "schedule": {"days": [0], "from": "08:00", "to": "18:00"}, "retention_days": null},
    "motion": {"enabled": true, "sensitivity": 0.3, "cooldown_s": 10, "min_cells": 3,
               "zones": {"cols": 16, "rows": 9, "mask": [1, 1, …]},
               "schedule": null},
    "viewers": ["anna"],
    "onvif": null,
    "created_at": "…", "updated_at": "…"
  }]
}
```

- `surveillance_settings.json`: `storage_dir` (null → `<DATA_DIR>/surveillance`),
  `quota_pct` (50–95), `default_retention_days`, `segment_s`, `pre_s`/`post_s`,
  `notify_motion`, `notify_offline`.
- `surveillance_events.json` v2: newest first, cap 2000, each event
  `{id, camera_id, camera_name, kind, at, ended_at, score, image,
  recording: {from, to}, reviewed}`.
- `surveillance_exports.json`: last 30 export jobs (`op_id`, state, size, error).
- Migration v1 → v2 splits `url` into `streams`, encrypts the embedded password,
  maps `motion.interval_s` to `cooldown_s`, and starts every camera with
  `record.mode = "off"` (no surprise disk use).

## Security

- Every mutating route (`/cameras*`, `/discover`, `/test`, `/settings`,
  `/storage*`, `/exports`, event delete) requires an admin JWT.
- Read routes (list, snapshot, live, HLS, playback, events, images) are
  filtered by `viewers`: admin sees everything, a user sees only cameras that
  name them. A camera with an empty list is admin-only.
- Credentials never leave the backend: API payloads use `url_masked`, the
  password is AES-256-GCM encrypted with a key derived from the JWT secret, and
  every ffmpeg log/detail goes through `mask_text`.
- ffmpeg writes the input URL (with `user:password@`) to stderr on connection
  errors, so stderr is drained through a redacting writer into
  `<live>/ffmpeg.log`. The log is reset on every run; nothing on disk contains a
  stream password. A camera *named* after its URL is masked in the UI, events,
  notifications and logs.
- Media URLs carry `?token=` (the documented pattern for `<img>`/`<video>`);
  hls.js sends `Authorization` through `xhrSetup`.

## API (`/api/surveillance`, all JWT)

| Method & path | Who | Purpose |
|---|---|---|
| `GET /cameras` | viewer | cameras + runtime status, masked URLs |
| `POST /cameras` · `PUT /cameras/{id}` · `DELETE /cameras/{id}` | admin | CRUD; `streams`, `record`, `motion`, `viewers` |
| `POST /cameras/{id}/test` · `POST /test` | admin | ffmpeg probe (≤8 s), never raises |
| `POST /discover` | admin | ONVIF WS-Discovery + RTSP/HTTP scan |
| `GET /cameras/{id}/snapshot?w=&refresh=&at=` | viewer | live grab, or a still from the archive |
| `GET /cameras/{id}/live` | viewer | legacy MJPEG fallback |
| `GET /cameras/{id}/hls/{index.m3u8,init.mp4,seg*.m4s}` | viewer | live HLS window |
| `GET /cameras/{id}/recordings?date=` | viewer | merged ranges + total seconds |
| `GET /recordings/{id}/days` | viewer | days that have recordings |
| `GET /playback/{id}/index.m3u8?from=&to=` | viewer | generated VOD playlist |
| `GET /playback/{id}/seg?p=day/run/seg` | viewer | archive segment |
| `POST /exports` → `{op_id}` · `GET /exports/{id}` · `GET /exports/{id}/download` | admin / viewer | MP4 export of a range |
| `GET /events?camera_id=&date=&reviewed=` | viewer | filtered events |
| `POST /events/{id}/reviewed` | viewer | mark reviewed |
| `DELETE /events/{id}` · `DELETE /events` | admin | delete / clear |
| `GET /events/{id}/image` | viewer | event snapshot |
| `GET /settings` · `PUT /settings` | admin | global defaults |
| `GET /storage` · `POST /storage/purge` | admin | usage/quota + manual retention |

Legacy manual clips (`/cameras/{id}/record`, `/clips*`) stay for compatibility
but are admin-only; the desktop app records through `record.mode`.

## Motion detection

- Frames come from the live ffmpeg (`-vf fps=1,scale=160:90,format=gray`), so
  there is no second connection or decode per camera.
- `MotionDetector`: 16×9 cell grid, per-cell mean absolute difference against
  the previous frame, threshold `8 + sensitivity×200`; a cell is "changed" above
  that. Trigger when `changed ≥ min_cells` and the cooldown elapsed.
- Zones mask cells; a masked-out cell can never trigger. The API validates the
  mask length and that at least one cell stays enabled.
- Per-camera schedule (`days`, `from`, `to`, overnight aware) gates detection.
- Event snapshot: a 640 px archived/live still, stored under
  `surveillance/events/<ts>_<cam>.jpg`; the event carries a `recording` range
  (`pre_s` before, `post_s` after) the UI seeks to.
- Notification: `emit_event("warning", …, action={label: View, appId:
  surveillance, arg: event_id}, dedupe_key=camera:<id>:motion, users=viewers)`.
  Camera offline alerts use `dedupe_key=camera:<id>:offline` after 3 failed
  restarts. Both can be disabled in Settings.

## Package UI (`ethos-surveillance` 2.0.0)

- Shared shell: 218 px text-only rail, `sv-*` palette with light + dark tokens,
  summary tiles, panels, 11 px tables, tags, responsive < 900 px.
- **Live** — 1/4/9/16 grid, camera picker, HLS `<video>` per tile with MJPEG
  fallback, online dot, bitrate/REC tag.
- **Playback** — camera + day picker, 24 h timeline with recorded ranges,
  click-to-seek, "Export day" (progress via `ctx.progress`).
- **Events** — thumbnail grid; a click marks reviewed, opens playback at the
  event range and seeks to the pre-roll.
- **Cameras** — table plus inline editor (main/sub stream, credentials, recording
  mode + schedule + retention, viewers) and the motion-zone grid editor.
- **Storage / Settings** — admin-only usage, quota, folder picker
  (`ctx.pickFolder(['/media','/mnt'])`), purge, and notification defaults.
- Feedback only through `ctx.notifications`, dialogs through `ctx.modal`, logs
  through `ctx.log`, status through `ctx.ws.connect('/ws/surveillance')`.

## Verification Log

- 2026-07-11 — original MVP (phases 1–5 in the previous revision of this file).
- 2026-09-26 — network discovery + ffmpeg `-stimeout` fix.
- 2026-11-22 — Cameras 2.0 backend: `camera_secrets`, `camera_settings`,
  `camera_storage`, `camera_runner`, `camera_recordings`, router authz + new
  endpoints, `/ws/surveillance`. `tests/test_cameras_core.py` **49/49 pass**
  (credentials, migration, ACL, schedules, detector, playlist/range math,
  path traversal, retention). Pipeline smoke against a local MJPEG source:
  live HLS window, 60 s archive segment, raw frames, motion events with
  snapshots — **PASS**.
- 2026-11-22 — Package 2.0.0 UI rewritten on the shared shell; manifest signed
  with `ethos-pack`.
- 2026-11-22 — Eufy Floodlight Cam 2 Pro diagnosis: NAS reached the camera
  (`ping` OK) but `192.168.50.191:554` refused — the camera's RTSP server was
  not running. `ffmpeg.log` captured the full URL with the password; fixed by
  draining stderr through `mask_text`, resetting the log per run and masking
  URL-like camera names (v2.25.1).
