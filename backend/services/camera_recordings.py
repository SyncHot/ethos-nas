"""Cameras — recordings index, playback playlist and range export.

The archive is plain fMP4 HLS written by `camera_runner`: one playlist per run
directory. This module turns those directories into:

  * a day index (`ranges`) for the UI timeline — parsed from the playlists,
    with a filename/mtime fallback for a playlist that did not finalize;
  * a **generated** playlist for an arbitrary time range, so `hls.js` can seek
    across hours (and across run directories) without transcoding;
  * MP4 exports for a selected range (`-c copy`, `+faststart`);
  * a single frame from a past moment (motion event thumbnails).

Nothing here spawns a per-viewer decoder: playback is a static playlist plus
byte-range reads of the archive segments.
"""

import logging
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from backend.config import settings
from backend.services import json_store, camera_service, camera_storage

logger = logging.getLogger("ethos-nas.services.camera_recordings")

EXPORTS_PATH = settings.DATA_DIR / "surveillance_exports.json"
EXPORTS_DIR = settings.DATA_DIR / "surveillance" / "exports"
SEGMENT_RE = re.compile(r"^(init\.mp4|seg\d{5}\.m4s)$")
MAX_EXPORTS = 30
RANGE_GAP_S = 5

_EXPORTS = json_store.get(EXPORTS_PATH, dict, version=1)
_EXPORT_LOCK = threading.Lock()


def _parse_iso(value: str) -> datetime:
    text = str(value or "").replace("Z", "+00:00")
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


# ── Playlist parsing ────────────────────────────────────────────────────────

def parse_playlist(path: Path) -> list[dict]:
    """Return [{start, duration, name}] for one run playlist. Never raises."""
    entries: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return entries
    pending_start: Optional[datetime] = None
    pending_duration: Optional[float] = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            try:
                pending_start = _parse_iso(line.split(":", 1)[1])
            except ValueError:
                pending_start = None
        elif line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line[8:].split(",", 1)[0])
            except ValueError:
                pending_duration = None
        elif line and not line.startswith("#"):
            if pending_duration is not None:
                start = pending_start
                if start is None:
                    try:
                        mtime = (path.parent / line).stat().st_mtime
                        start = datetime.fromtimestamp(mtime - pending_duration, tz=timezone.utc)
                    except OSError:
                        start = None
                if start is not None:
                    entries.append({"start": start, "duration": pending_duration, "name": line})
            pending_duration = None
            pending_start = None
    return entries


def _run_entries(cam_id: str, day: str, run: str) -> list[dict]:
    run_path = camera_storage.run_dir(cam_id, day, run)
    playlist = run_path / "index.m3u8"
    entries = parse_playlist(playlist) if playlist.exists() else []
    if entries:
        for entry in entries:
            entry["run"] = run
            entry["day"] = day
        return entries
    # Playlist missing/unfinished (ffmpeg killed): derive the index from files.
    fallback: list[dict] = []
    try:
        files = sorted(p for p in run_path.glob("seg*.m4s"))
    except OSError:
        return fallback
    for item in files:
        try:
            mtime = item.stat().st_mtime
        except OSError:
            continue
        fallback.append({
            "start": datetime.fromtimestamp(mtime, tz=timezone.utc),
            "duration": 60.0,
            "name": item.name,
            "run": run,
            "day": day,
        })
    return fallback


def _all_entries(cam_id: str, day: str) -> list[dict]:
    day_path = camera_storage.camera_recordings_dir(cam_id) / day
    try:
        runs = sorted(p.name for p in day_path.iterdir()
                      if p.is_dir() and camera_storage.RUN_DIR_RE.match(p.name))
    except OSError:
        return []
    entries: list[dict] = []
    for run in runs:
        entries.extend(_run_entries(cam_id, day, run))
    entries.sort(key=lambda e: e["start"])
    return entries


# ── Public index ────────────────────────────────────────────────────────────

def available_days(cam_id: str, limit: int = 31) -> list[str]:
    """Dates (newest first) that have at least one run directory."""
    root = camera_storage.camera_recordings_dir(cam_id)
    try:
        days = sorted((p.name for p in root.iterdir()
                       if p.is_dir() and camera_storage.DATE_DIR_RE.match(p.name)), reverse=True)
    except OSError:
        return []
    return days[:limit]


def ranges(cam_id: str, day: str) -> list[dict]:
    """Merged recorded ranges for one day: [{start, end, duration_s}]."""
    entries = _all_entries(cam_id, day)
    if not entries:
        return []
    merged: list[dict] = []
    current_start = entries[0]["start"]
    current_end = entries[0]["start"] + timedelta(seconds=entries[0]["duration"])
    for entry in entries[1:]:
        start = entry["start"]
        end = start + timedelta(seconds=entry["duration"])
        if (start - current_end).total_seconds() <= RANGE_GAP_S:
            current_end = max(current_end, end)
        else:
            merged.append({"start": _iso(current_start), "end": _iso(current_end),
                           "duration_s": int((current_end - current_start).total_seconds())})
            current_start, current_end = start, end
    merged.append({"start": _iso(current_start), "end": _iso(current_end),
                   "duration_s": int((current_end - current_start).total_seconds())})
    return merged


def has_recording(cam_id: str, start: datetime, end: datetime) -> bool:
    day = start.astimezone(timezone.utc).date().isoformat()
    for item in ranges(cam_id, day):
        if _parse_iso(item["start"]) <= end and _parse_iso(item["end"]) >= start:
            return True
    return False


# ── Generated playback playlist ─────────────────────────────────────────────

def _segment_uri(cam_id: str, day: str, run: str, name: str) -> str:
    rel = f"{day}/{run}/{name}"
    return f"/api/surveillance/playback/{cam_id}/seg?p={quote(rel, safe='')}"


def playlist(cam_id: str, start: datetime, end: datetime) -> str:
    """HLS playlist over the archive segments overlapping [start, end]."""
    days = sorted({(start + timedelta(days=i)).astimezone(timezone.utc).date().isoformat()
                   for i in range(0, max(1, (end - start).days + 1))})
    entries: list[dict] = []
    for day in days:
        for entry in _all_entries(cam_id, day):
            entry_end = entry["start"] + timedelta(seconds=entry["duration"])
            if entry_end >= start and entry["start"] <= end:
                entries.append(entry)
    if not entries:
        raise camera_service.CameraError("No recording in this range")
    entries.sort(key=lambda e: e["start"])

    target = max(2, int(math.ceil(max(e["duration"] for e in entries))))
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{target}",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
    current_run = None
    for entry in entries:
        run_key = (entry["day"], entry["run"])
        if run_key != current_run:
            current_run = run_key
            if len(lines) > 5:
                lines.append("#EXT-X-DISCONTINUITY")
            init_uri = _segment_uri(cam_id, entry["day"], entry["run"], "init.mp4")
            lines.append(f'#EXT-X-MAP:URI="{init_uri}"')
        lines.append(f"#EXTINF:{entry['duration']:.3f},")
        lines.append(_segment_uri(cam_id, entry["day"], entry["run"], entry["name"]))
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def segment_path(cam_id: str, rel: str) -> Path:
    """Resolve `day/run/segment` returned by the playlist, safely."""
    parts = str(rel or "").split("/")
    if len(parts) != 3:
        raise camera_service.CameraError("Invalid segment path")
    day, run, name = parts
    if not camera_storage.DATE_DIR_RE.match(day) or not camera_storage.RUN_DIR_RE.match(run):
        raise camera_service.CameraError("Invalid segment path")
    if not SEGMENT_RE.match(name):
        raise camera_service.CameraError("Invalid segment name")
    path = camera_storage.run_dir(cam_id, day, run) / name
    if not path.is_file():
        raise camera_service.CameraError("Segment not found")
    return path


def latest_snapshot_source(cam_id: str, at: datetime) -> Optional[Path]:
    """Nearest archived segment at/before `at` for a still frame."""
    found = _entry_and_segment(cam_id, at)
    return found[0] if found else None


def _entry_and_segment(cam_id: str, at: datetime) -> Optional[tuple[Path, float]]:
    day = at.astimezone(timezone.utc).date().isoformat()
    entries = _all_entries(cam_id, day)
    if not entries:
        return None
    candidates = [e for e in entries if e["start"] <= at] or entries
    entry = max(candidates, key=lambda e: e["start"])
    offset = max(0.0, (at - entry["start"]).total_seconds())
    path = camera_storage.run_dir(cam_id, entry["day"], entry["run"]) / entry["name"]
    return path, offset


def snapshot_at(cam_id: str, at: datetime) -> Path:
    """Extract one frame from the archive (motion event / timeline snapshot)."""
    found = _entry_and_segment(cam_id, at)
    if found is None:
        raise camera_service.CameraError("No recording at this time")
    segment, offset = found
    camera_service.SNAPS_DIR.mkdir(parents=True, exist_ok=True)
    out = camera_service.SNAPS_DIR / f"{cam_id}_at_{int(at.timestamp())}.jpg"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-ss", f"{offset:.3f}", "-i", str(segment),
           "-frames:v", "1", "-q:v", "4", "-y", str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise camera_service.CameraError(f"Snapshot failed: {exc}") from exc
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise camera_service.CameraError(
            f"Snapshot failed: {(proc.stderr or 'ffmpeg').strip()[:200]}")
    return out


# ── Exports ─────────────────────────────────────────────────────────────────

def _load_exports() -> dict:
    data = _EXPORTS.load()
    return data if isinstance(data, dict) else {}


def _save_export(op: dict) -> None:
    def _mutate(data):
        if not isinstance(data, dict):
            data = {}
        raw_ops = data.get("ops")
        candidates = raw_ops if isinstance(raw_ops, list) else []
        # JSON on disk can hold anything: a non-dict entry must not crash the
        # export (hand-edited or truncated camera_exports.json).
        ops = [o for o in candidates if isinstance(o, dict) and o.get("op_id") != op["op_id"]]
        ops.insert(0, op)
        data["ops"] = ops[:MAX_EXPORTS]
        data["version"] = 1
        return data

    _EXPORTS.update(_mutate)


def start_export(cam_id: str, from_iso: str, to_iso: str) -> dict:
    start, end = _parse_iso(from_iso), _parse_iso(to_iso)
    if end <= start:
        raise camera_service.CameraError("End must be after start")
    if (end - start) > timedelta(hours=24):
        raise camera_service.CameraError("Export is limited to 24 hours")
    playlist(cam_id, start, end)     # validates that something exists
    op = {
        "op_id": f"exp_{int(time.time())}_{cam_id[-4:]}",
        "camera_id": cam_id,
        "from": _iso(start),
        "to": _iso(end),
        "state": "queued",
        "created_at": _iso(datetime.now(timezone.utc)),
        "size_bytes": 0,
        "error": "",
    }
    _save_export(op)
    with _EXPORT_LOCK:
        threading.Thread(target=run_export, args=(op["op_id"],), daemon=True,
                         name=f"camera-export-{op['op_id']}").start()
    return op


def get_export(op_id: str) -> dict:
    for op in (_load_exports().get("ops") or []):
        if op.get("op_id") == op_id:
            return op
    raise camera_service.CameraError("Export not found")


def export_path(op_id: str) -> Path:
    op = get_export(op_id)
    path = EXPORTS_DIR / f"{op_id}.mp4"
    if op.get("state") != "done" or not path.is_file():
        raise camera_service.CameraError("Export is not ready")
    return path


def run_export(op_id: str) -> None:
    op = get_export(op_id)
    start, end = _parse_iso(op["from"]), _parse_iso(op["to"])
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    temp_dir = EXPORTS_DIR / f"tmp-{op_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    playlist_path = temp_dir / "index.m3u8"
    out_path = EXPORTS_DIR / f"{op_id}.mp4"
    try:
        entries: list[dict] = []
        for day in sorted({(start + timedelta(days=i)).astimezone(timezone.utc).date().isoformat()
                           for i in range(0, max(1, (end - start).days + 1))}):
            entries.extend(e for e in _all_entries(op["camera_id"], day)
                           if e["start"] + timedelta(seconds=e["duration"]) >= start
                           and e["start"] <= end)
        entries.sort(key=lambda e: e["start"])
        if not entries:
            raise RuntimeError("No recording in this range")
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:60"]
        current_run = None
        for entry in entries:
            run_key = (entry["day"], entry["run"])
            run_path = camera_storage.run_dir(op["camera_id"], entry["day"], entry["run"])
            if run_key != current_run:
                current_run = run_key
                lines.append("#EXT-X-DISCONTINUITY")
                rel_init = os.path.relpath(run_path / "init.mp4", temp_dir)
                lines.append(f'#EXT-X-MAP:URI="{Path(rel_init).as_posix()}"')
            lines.append(f"#EXTINF:{entry['duration']:.3f},")
            rel_seg = os.path.relpath(run_path / entry["name"], temp_dir)
            lines.append(Path(rel_seg).as_posix())
        lines.append("#EXT-X-ENDLIST")
        playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-i", str(playlist_path), "-c", "copy",
               "-movflags", "+faststart", "-y", str(out_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError((proc.stderr or "ffmpeg export failed").strip()[:400])
        op.update({"state": "done", "size_bytes": out_path.stat().st_size})
        logger.info("Camera export ready: %s (%.1f MiB)", op_id, op["size_bytes"] / 1024 ** 2)
    except Exception as exc:  # noqa: BLE001 — the job state carries the reason to the UI
        op.update({"state": "error", "error": str(exc)[:400], "size_bytes": 0})
        logger.error("Camera export failed: %s", op_id, exc_info=True)
    finally:
        try:
            playlist_path.unlink(missing_ok=True)
            temp_dir.rmdir()
        except OSError:
            pass  # temp dir cleanup is best effort
        _save_export(op)


def cleanup_exports() -> None:
    """Drop export files whose op fell out of the bounded list."""
    known = {op["op_id"] for op in (_load_exports().get("ops") or [])}
    try:
        for path in EXPORTS_DIR.glob("*.mp4"):
            if path.stem not in known:
                path.unlink()
    except OSError:
        pass  # nothing to clean or a file is being read right now
