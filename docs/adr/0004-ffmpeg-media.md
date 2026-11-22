# ADR 0004 — ffmpeg for thumbnails and on-demand transcoding

**Status:** accepted (long-standing; written down 2026-11-11).

## Context

The NAS must show previews for images and videos and must let browsers play formats they cannot decode
natively (HEVC, MKV with AC-3). Pure-Python media handling is not viable; a GPU transcoding stack would
add drivers and a second service.

## Decision

- Thumbnails: Pillow for images, ffmpeg frame extraction for video, OpenCV Haar cascades for face-aware
  crops. Outputs are WebP and cached under `DATA_DIR/thumbnails` / `video_thumbs`.
- Playback: on-demand H.264 remux/transcode into `DATA_DIR/transcodes`, served with Range support and
  removed by the retention sweep after 24 h.
- Every ffmpeg invocation is an argument list (never `shell=True`), with a timeout and a capped log tail.

## Consequences

- `ffmpeg` is a hard runtime dependency; `/api/system/doctor` reports it and the UI hides video previews
  when it is missing.
- Transcodes cost CPU; they are single-flight per file and cancelled on shutdown (task registry).
- Caches must be bounded — the retention service enforces LRU caps, otherwise a `find /` in the File
  Manager can generate six figures of thumbnails.
- Hardware acceleration: Video Station probes VA-API/QuickSync and uses hardware H.264
  encode (with VA-API/QSV decode when the file supports it), falling back to software
  libx264 — see `docs/video-station.md` Phase 8. File Manager previews and Surveillance
  recordings are still software-only; a future ADR should cover NVENC/zero-copy for them.
