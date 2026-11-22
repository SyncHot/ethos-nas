"""Cameras service.

Camera store (JSON, thread-safe like notifications_service), ffmpeg-based
snapshot/live streaming, and per-camera motion-detection threads that feed the
Notification Center via emit_event().

Persistence (all under settings.DATA_DIR):
    surveillance.json          — {version: 1, cameras: [...]}
    surveillance_events.json   — {version: 1, events: [...]} newest first, cap 200
    surveillance/snaps/<camid>.jpg      — snapshot disk cache
    surveillance/events/<ts>_<camid>.jpg — motion event snapshots
    surveillance/clips/<ts>_<camid>.mp4  — MP4 recordings (libx264, no audio)

All ffmpeg invocations are argument lists (never shell=True). Motion threads
are daemons and must never take the app down: every loop iteration is wrapped.
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from PIL import Image, ImageChops

from backend.config import settings
from backend.services import child_limits
from backend.services.notifications_service import emit_event

logger = logging.getLogger("ethos-nas.services.camera_service")

# Camera URLs commonly embed user:password and ffmpeg echoes the input URL in
# its errors, so anything logged or surfaced in the UI must be redacted first.
_CRED_URL_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/\s:@]+):(?P<pw>[^/\s@]+)@")


def _redact_credentials(text: object) -> str:
    """Strip `user:password@` from any URL-like text before logging."""
    return _CRED_URL_RE.sub(r"\g<scheme>***:***@", str(text))


CAMERAS_PATH: Path = settings.DATA_DIR / "surveillance.json"
EVENTS_PATH: Path = settings.DATA_DIR / "surveillance_events.json"
SNAPS_DIR: Path = settings.DATA_DIR / "surveillance" / "snaps"
EVENTS_IMG_DIR: Path = settings.DATA_DIR / "surveillance" / "events"
CLIPS_DIR: Path = settings.DATA_DIR / "surveillance" / "clips"

MAX_EVENTS = 200
ALLOWED_SCHEMES = ("rtsp", "http", "https")
GRAB_TIMEOUT_S = 8          # snapshot / probe timeout
MOTION_MIN_GAP_S = 10       # min seconds between events for one camera
MOTION_FRAME_SIZE = (160, 90)
MAX_CLIPS_BYTES = 4 * 1024 ** 3   # safety cap on the clips dir before new recordings are refused
CLIP_NAME_RE = re.compile(r"^(\d{10})_(cam_[a-f0-9]{12})\.mp4$")

_LOCK = threading.Lock()
_MOTION_THREADS: dict[str, "MotionThread"] = {}
_THREADS_LOCK = threading.Lock()
_RECORD_PROCS: dict[str, subprocess.Popen] = {}
_RECORD_META: dict[str, dict] = {}   # cam_id → {started_at, path}
_RECORDS_LOCK = threading.Lock()


class CameraError(Exception):
    """User-facing error (bad input / unreachable camera)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── JSON stores (same pattern as notifications_service) ─────────────────────

def _atomic_write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _load_cameras() -> dict:
    try:
        with open(CAMERAS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("cameras"), list):
            return data
    except (OSError, ValueError):
        pass  # unreadable/missing file is treated as absent
    return {"version": 1, "cameras": []}


def _save_cameras(store: dict) -> None:
    _atomic_write(CAMERAS_PATH, store)


def _load_events() -> dict:
    try:
        with open(EVENTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data
    except (OSError, ValueError):
        pass  # unreadable/missing file is treated as absent
    return {"version": 1, "events": []}


def _save_events(store: dict) -> None:
    _atomic_write(EVENTS_PATH, store)


# ── ffmpeg helpers ──────────────────────────────────────────────────────────

def _url_input_args(url: str) -> list[str]:
    """Extra ffmpeg args that must precede -i for a given URL scheme."""
    if urlparse(url).scheme == "rtsp":
        # -rw_timeout is the global socket timeout (µs); the old RTSP-private
        # -stimeout was removed as a CLI option in ffmpeg 8 and aborts parsing.
        return ["-rtsp_transport", "tcp", "-rw_timeout", "5000000"]
    return []


def _ffmpeg_grab(url: str, out_path: Path, scale: Optional[str] = None, timeout: int = GRAB_TIMEOUT_S) -> tuple[bool, str]:
    """Grab a single JPEG frame from url. Returns (ok, detail). Never raises."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    cmd += _url_input_args(url)
    cmd += ["-i", url]
    if scale:
        cmd += ["-vf", f"scale={scale}"]
    cmd += ["-frames:v", "1", "-q:v", "4", "-y", str(out_path)]
    try:
        r = subprocess.run(child_limits.wrap(cmd, mem_mb=settings.CHILD_THUMB_MEM_LIMIT_MB),
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            return False, (r.stderr or "ffmpeg failed").strip().splitlines()[-1][:300]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as exc:
        return False, str(exc)


def _live_stream(url: str):
    """Yield multipart/x-mixed-replace chunks (bytes) for a live view.

    ffmpeg re-encodes the source to raw MJPEG on stdout; we split it into JPEG
    frames (SOI..EOI) and wrap each in a multipart part — the format browsers'
    <img> tags render natively. Works uniformly for RTSP and HTTP-MJPEG sources.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    cmd += _url_input_args(url)
    cmd += ["-i", url, "-an", "-f", "mjpeg", "-q:v", "6", "-r", "5", "pipe:1"]
    proc = subprocess.Popen(
        child_limits.wrap(cmd, mem_mb=settings.CHILD_THUMB_MEM_LIMIT_MB),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def gen():
        buf = b""
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                start = buf.find(b"\xff\xd8")
                while start != -1:
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end == -1:          # frame incomplete — wait for more data
                        del buf[:start]
                        break
                    frame = buf[start:end + 2]
                    part = (b"--frame\r\nContent-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(frame)).encode() +
                            b"\r\n\r\n" + frame + b"\r\n")
                    yield part
                    del buf[:end + 2]
                    start = buf.find(b"\xff\xd8")
        finally:
            try:
                proc.kill()
            except OSError:
                pass  # best-effort cleanup — nothing to report
            proc.wait(timeout=5)

    return gen()


# ── Camera CRUD ─────────────────────────────────────────────────────────────

def _validate_url(url: str) -> None:
    url = (url or "").strip()
    if not url:
        raise CameraError("URL must not be empty")
    scheme = urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise CameraError(f"Unsupported URL scheme '{scheme or 'missing'}' — use rtsp://, http:// or https://")


def _validate_name(name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise CameraError("Name must not be empty")
    if len(name) > 60:
        raise CameraError("Name too long (max 60 chars)")


def _validate_motion(motion: dict) -> None:
    interval = motion.get("interval_s", 5)
    sens = motion.get("sensitivity", 0.3)
    if not isinstance(interval, (int, float)) or not (1 <= float(interval) <= 3600):
        raise CameraError("motion.interval_s must be between 1 and 3600")
    if not isinstance(sens, (int, float)) or not (0.05 <= float(sens) <= 0.9):
        raise CameraError("motion.sensitivity must be between 0.05 and 0.9")


def _new_camera(name: str, url: str) -> dict:
    import uuid
    return {
        "id": f"cam_{uuid.uuid4().hex[:12]}",
        "name": name.strip(),
        "url": url.strip(),
        "enabled": True,
        "motion": {"enabled": False, "interval_s": 5, "sensitivity": 0.3},
        "created_at": _now_iso(),
    }


def list_cameras() -> list[dict]:
    with _LOCK:
        store = _load_cameras()
    cams = [dict(c) for c in store["cameras"]]
    with _RECORDS_LOCK:
        active_recs = {cid for cid, p in _RECORD_PROCS.items() if p.poll() is None}
    for c in cams:  # runtime-only fields
        t = _MOTION_THREADS.get(c["id"])
        c["last_error"] = getattr(t, "last_error", "") if t else ""
        c["recording"] = c["id"] in active_recs
    return cams


def add_camera(name: str, url: str) -> dict:
    _validate_name(name)
    _validate_url(url)
    with _LOCK:
        store = _load_cameras()
        for c in store["cameras"]:
            if c.get("url", "").strip().lower() == url.strip().lower():
                raise CameraError(f"A camera with this URL already exists ({c['name']})")
        cam = _new_camera(name, url)
        store["cameras"].append(cam)
        _save_cameras(store)
    if cam["enabled"]:
        start_motion_thread(cam)
    return cam


def update_camera(cam_id: str, fields: dict) -> dict:
    with _LOCK:
        store = _load_cameras()
        cam = next((c for c in store["cameras"] if c["id"] == cam_id), None)
        if cam is None:
            raise CameraError("Camera not found")
        if "name" in fields and fields["name"] is not None:
            _validate_name(fields["name"])
            cam["name"] = fields["name"].strip()
        if "url" in fields and fields["url"] is not None:
            _validate_url(fields["url"])
            for other in store["cameras"]:
                if other["id"] != cam_id and other.get("url", "").strip().lower() == fields["url"].strip().lower():
                    raise CameraError(f"A camera with this URL already exists ({other['name']})")
            cam["url"] = fields["url"].strip()
        if "enabled" in fields and fields["enabled"] is not None:
            cam["enabled"] = bool(fields["enabled"])
        if "motion" in fields and isinstance(fields["motion"], dict):
            _validate_motion(fields["motion"])
            m = cam.setdefault("motion", {"enabled": False, "interval_s": 5, "sensitivity": 0.3})
            for k in ("enabled", "interval_s", "sensitivity"):
                if k in fields["motion"] and fields["motion"][k] is not None:
                    m[k] = bool(fields["motion"][k]) if k == "enabled" else float(fields["motion"][k])
        _save_cameras(store)
    # Restart the motion thread so any change takes effect.
    stop_motion_thread(cam_id)
    if cam.get("enabled") and (cam.get("motion") or {}).get("enabled"):
        start_motion_thread(cam)
    return cam


def delete_camera(cam_id: str) -> None:
    with _LOCK:
        store = _load_cameras()
        before = len(store["cameras"])
        store["cameras"] = [c for c in store["cameras"] if c["id"] != cam_id]
        if len(store["cameras"]) == before:
            raise CameraError("Camera not found")
        _save_cameras(store)
    stop_motion_thread(cam_id)
    # Remove cached images (snapshot + this camera's event snapshots).
    snap = SNAPS_DIR / f"{cam_id}.jpg"
    try:
        if snap.exists():
            snap.unlink()
    except OSError:
        pass  # best-effort cleanup — nothing to report
    with _LOCK:
        ev_store = _load_events()
        kept, removed_ids = [], []
        for e in ev_store["events"]:
            if e.get("camera_id") == cam_id and e.get("image"):
                removed_ids.append(e)
            else:
                kept.append(e)
        ev_store["events"] = kept
        _save_events(ev_store)
    for e in removed_ids:
        try:
            p = EVENTS_IMG_DIR / e["image"]
            if p.exists():
                p.unlink()
        except OSError:
            pass  # best-effort cleanup — nothing to report


# ── Connection test / snapshot / live ───────────────────────────────────────

def test_connection(url: str) -> dict:
    """Probe a camera URL (≤8 s). Returns {ok, detail} — never raises."""
    _validate_url(url)  # scheme check only; bad host must not raise either
    try:
        with tempfile.TemporaryDirectory(prefix="ethos-cam-") as td:
            ok, detail = _ffmpeg_grab(url, Path(td) / "probe.jpg", scale="160:-1", timeout=GRAB_TIMEOUT_S)
        if ok:
            return {"ok": True, "detail": ""}
        # An RTSP 404 means the server answered (auth OK) but has no stream at
        # this path — usually a vendor app feature that must be enabled first.
        if "404" in detail:
            detail += (
                " — the camera's RTSP server responded, but there is no stream at this"
                " path. Check the exact URL shown by your camera's app (e.g. Eufy:"
                " Settings → Storage → NAS/RTSP) and use that one."
            )
        return {"ok": False, "detail": f"Cannot reach camera: {detail}"}
    except CameraError as exc:
        return {"ok": False, "detail": str(exc)}


def get_snapshot(cam_id: str, width: int = 480, refresh: bool = False) -> Path:
    """Return a JPEG path for the camera (disk-cached). Raises CameraError on grab failure."""
    with _LOCK:
        cam = next((c for c in _load_cameras()["cameras"] if c["id"] == cam_id), None)
    if cam is None:
        raise CameraError("Camera not found")

    SNAPS_DIR.mkdir(parents=True, exist_ok=True)
    cache = SNAPS_DIR / f"{cam_id}.jpg"
    if refresh or not (cache.exists() and cache.stat().st_size > 0):
        ok, detail = _ffmpeg_grab(cam["url"], cache)
        if not ok:
            raise CameraError(f"Snapshot failed: {detail}")

    # Serve at the requested width without re-grabbing.
    try:
        with Image.open(cache) as im:
            if width and im.width > int(width):
                ratio = int(width) / im.width
                out = SNAPS_DIR / f"{cam_id}_{width}.jpg"
                im.resize((int(width), max(1, int(im.height * ratio)))).save(out, quality=85)
                return out
    except (OSError, ValueError):
        pass  # fall through and serve the original
    return cache


def live_stream(cam_id: str):
    """MJPEG chunk generator for a camera. Raises CameraError if unknown."""
    with _LOCK:
        cam = next((c for c in _load_cameras()["cameras"] if c["id"] == cam_id), None)
    if cam is None:
        raise CameraError("Camera not found")
    return _live_stream(cam["url"])


# ── Events ──────────────────────────────────────────────────────────────────

def list_events(limit: int = 50, camera_id: Optional[str] = None) -> list[dict]:
    with _LOCK:
        events = list(_load_events()["events"])
    if camera_id:
        events = [e for e in events if e.get("camera_id") == camera_id]
    return events[: max(1, min(limit, 200))]


def delete_event(event_id: str) -> None:
    with _LOCK:
        store = _load_events()
        before = len(store["events"])
        gone = [e for e in store["events"] if e.get("id") == event_id]
        store["events"] = [e for e in store["events"] if e.get("id") != event_id]
        if len(store["events"]) == before:
            raise CameraError("Event not found")
        _save_events(store)
    for e in gone:
        if e.get("image"):
            try:
                p = EVENTS_IMG_DIR / e["image"]
                if p.exists():
                    p.unlink()
            except OSError:
                pass  # best-effort cleanup — nothing to report


def clear_events() -> int:
    with _LOCK:
        store = _load_events()
        n = len(store["events"])
        for e in store["events"]:
            if e.get("image"):
                try:
                    p = EVENTS_IMG_DIR / e["image"]
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass  # best-effort cleanup — nothing to report
        store["events"] = []
        _save_events(store)
    return n


def _record_motion_event(cam: dict, image_rel: Optional[str]) -> None:
    """Append a motion event (newest first, capped) + Notification Center entry."""
    import uuid
    with _LOCK:
        store = _load_events()
        store["events"].insert(0, {
            "id": f"mev_{uuid.uuid4().hex[:12]}",
            "camera_id": cam["id"],
            "camera_name": cam["name"],
            "at": _now_iso(),
            "image": image_rel,
        })
        if len(store["events"]) > MAX_EVENTS:
            del store["events"][MAX_EVENTS:]
        _save_events(store)
    emit_event("warning", f"Motion detected — {cam['name']}", source="surveillance")


# ── Motion detection threads ────────────────────────────────────────────────

class MotionThread(threading.Thread):
    """Frame-diff motion detector for one camera. Never raises out of the loop."""

    def __init__(self, cam: dict):
        super().__init__(name=f"motion-{cam['id']}", daemon=True)
        self.cam_id = cam["id"]
        self.last_error = ""
        self._stop_evt = threading.Event()
        self._prev_frame: Optional[bytes] = None
        self._last_event_at = 0.0

    def stop(self):
        self._stop_evt.set()

    @staticmethod
    def _frame_bytes(url: str) -> tuple[Optional[bytes], str]:
        """Grab a small grayscale frame as raw bytes. Returns (data, error)."""
        with tempfile.TemporaryDirectory(prefix="ethos-motion-") as td:
            out = Path(td) / "f.jpg"
            ok, detail = _ffmpeg_grab(url, out, scale="320:-1", timeout=GRAB_TIMEOUT_S)
            if not ok:
                return None, detail
            with Image.open(out) as im:
                g = im.convert("L").resize(MOTION_FRAME_SIZE)
                return bytes(g.getdata()), ""

    def run(self):
        fails = 0
        while not self._stop_evt.is_set():
            cam = self._current_cam()
            if cam is None or not cam.get("enabled") or not (cam.get("motion") or {}).get("enabled"):
                return  # camera deleted/disabled — thread exits
            motion = cam["motion"]
            frame, err = self._frame_bytes(cam["url"])
            if frame is None:
                fails += 1
                if fails == 3:
                    safe_err = _redact_credentials(err)
                    self.last_error = f"Motion check failing: {safe_err}"
                    logger.warning("Camera %s (%s): %s", cam["name"], cam["id"], safe_err)
                self._stop_evt.wait(min(motion.get("interval_s", 5), 10))
                continue
            fails = 0
            self.last_error = ""
            if self._prev_frame is not None:
                diff = sum(abs(a - b) for a, b in zip(frame, self._prev_frame)) / len(frame)
                now = time.time()
                sens = float(motion.get("sensitivity", 0.3))
                if diff > sens and (now - self._last_event_at) >= MOTION_MIN_GAP_S:
                    self._last_event_at = now
                    self._on_motion(cam, frame)
            self._prev_frame = frame
            self._stop_evt.wait(float(motion.get("interval_s", 5)))

    def _current_cam(self) -> Optional[dict]:
        try:
            with _LOCK:
                return next((c for c in _load_cameras()["cameras"] if c["id"] == self.cam_id), None)
        except Exception:  # noqa: BLE001 — thread must survive store errors
            return None

    def _on_motion(self, cam: dict, frame: bytes) -> None:
        """Save an event snapshot (best effort) and record the event."""
        image_rel = None
        try:
            EVENTS_IMG_DIR.mkdir(parents=True, exist_ok=True)
            rel = f"{int(time.time())}_{self.cam_id}.jpg"
            Image.frombytes("L", MOTION_FRAME_SIZE, frame).convert("RGB").save(
                EVENTS_IMG_DIR / rel, quality=80)
            image_rel = rel
        except Exception as exc:  # noqa: BLE001 — event still recorded without image
            logger.warning("Motion snapshot save failed for %s: %s", self.cam_id, exc)
        try:
            _record_motion_event(cam, image_rel)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to record motion event for %s: %s", self.cam_id, exc)


def start_motion_thread(cam: dict) -> None:
    with _THREADS_LOCK:
        old = _MOTION_THREADS.get(cam["id"])
        if old is not None:
            old.stop()
        t = MotionThread(cam)
        _MOTION_THREADS[cam["id"]] = t
        t.start()


def stop_motion_thread(cam_id: str) -> None:
    with _THREADS_LOCK:
        t = _MOTION_THREADS.pop(cam_id, None)
    if t is not None:
        t.stop()


def start_all_motion_threads() -> int:
    """Boot hook: start threads for enabled cameras with motion on. Returns count."""
    n = 0
    for cam in list_cameras():
        if cam.get("enabled") and (cam.get("motion") or {}).get("enabled"):
            start_motion_thread(cam)
            n += 1
    return n


def stop_all_motion_threads() -> None:
    with _THREADS_LOCK:
        threads = list(_MOTION_THREADS.values())
        _MOTION_THREADS.clear()
    for t in threads:
        t.stop()


# ── MP4 clip recording ──────────────────────────────────────────────────────

def _clips_dir() -> Path:
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    return CLIPS_DIR


def _clips_used_bytes() -> int:
    total = 0
    try:
        for p in CLIPS_DIR.glob("*.mp4"):
            try:
                total += p.stat().st_size
            except OSError:
                pass  # probe failure means 'not present'
    except OSError:
        pass  # probe failure means 'not present'
    return total


def start_recording(cam_id: str) -> dict:
    """Start recording a camera to surveillance/clips/<ts>_<camid>.mp4.

    Re-encodes (libx264, no audio) so any source — RTSP H.264 or HTTP-MJPEG —
    yields a universally playable MP4. Raises CameraError on bad state.
    """
    with _LOCK:
        cam = next((c for c in _load_cameras()["cameras"] if c["id"] == cam_id), None)
    if cam is None:
        raise CameraError("Camera not found")
    with _RECORDS_LOCK:
        if cam_id in _RECORD_PROCS and _RECORD_PROCS[cam_id].poll() is None:
            raise CameraError(f"{cam['name']} is already recording")
        used = _clips_used_bytes()
        if used > MAX_CLIPS_BYTES:
            raise CameraError(
                f"Clip storage full ({used / 1024**3:.1f} GiB of {MAX_CLIPS_BYTES // 1024**3} GiB cap) — delete old clips first")

        out = _clips_dir() / f"{int(time.time())}_{cam_id}.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        cmd += _url_input_args(cam["url"])
        cmd += ["-i", cam["url"], "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-g", "50", "-movflags", "+faststart", "-y", str(out)]
        try:
            proc = subprocess.Popen(
                child_limits.wrap(cmd, mem_mb=settings.CHILD_PROBE_MEM_LIMIT_MB),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise CameraError(f"Failed to start ffmpeg: {exc}") from exc
        _RECORD_PROCS[cam_id] = proc
        started_at = _now_iso()
        _RECORD_META[cam_id] = {"started_at": started_at, "path": str(out)}
    logger.info("Recording started for %s (%s) → %s", cam["name"], cam_id, out)
    return {"recording": True, "camera_id": cam_id, "started_at": started_at, "path": str(out)}


def stop_recording(cam_id: str) -> dict:
    """Stop a recording. SIGINT lets ffmpeg finalize the MP4 moov atom."""
    with _RECORDS_LOCK:
        proc = _RECORD_PROCS.pop(cam_id, None)
        meta = _RECORD_META.pop(cam_id, None)
    if proc is None or meta is None:
        raise CameraError("Camera is not recording")
    try:
        proc.send_signal(signal.SIGINT)  # lets ffmpeg finalize the MP4 moov atom
        proc.wait(timeout=6)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    except OSError:
        pass  # best-effort cleanup — nothing to report
    size = 0
    path = Path(meta["path"])
    try:
        size = path.stat().st_size
    except OSError:
        pass  # probe failure means 'not present'
    if size == 0 and path.exists():
        try:  # ffmpeg produced nothing (e.g. source died) — don't keep an empty file
            path.unlink()
        except OSError:
            pass  # best-effort cleanup — nothing to report
    logger.info("Recording stopped for %s (%s bytes)", cam_id, size)
    return {"stopped": True, "camera_id": cam_id, "path": meta["path"],
            "started_at": meta["started_at"], "size_bytes": size}


def recording_status(cam_id: str) -> dict:
    with _RECORDS_LOCK:
        proc = _RECORD_PROCS.get(cam_id)
        meta = _RECORD_META.get(cam_id)
    if proc is None or meta is None or proc.poll() is not None:
        return {"recording": False}
    size = 0
    try:
        size = Path(meta["path"]).stat().st_size
    except OSError:
        pass  # probe failure means 'not present'
    return {"recording": True, "started_at": meta["started_at"], "size_bytes": size}


def list_clips(camera_id: Optional[str] = None) -> list[dict]:
    clips: list[dict] = []
    try:
        files = sorted(CLIPS_DIR.glob("*.mp4"), reverse=True)
    except OSError:
        return []
    for p in files[:100]:  # bound the scan; names start with epoch ts
        m = CLIP_NAME_RE.match(p.name)
        if not m:
            continue
        if camera_id and m.group(2) != camera_id:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        clips.append({
            "id": p.name,
            "camera_id": m.group(2),
            "started_at": datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc).isoformat(timespec="seconds"),
            "size_bytes": st.st_size,
        })
    clips.sort(key=lambda c: c["id"], reverse=True)
    return clips[:50]


def get_clip_path(clip_id: str) -> Path:
    if not CLIP_NAME_RE.match(clip_id or ""):
        raise CameraError("Invalid clip id")
    path = (CLIPS_DIR / clip_id).resolve()
    if not str(path).startswith(str(CLIPS_DIR.resolve())) or not path.is_file():
        raise CameraError("Clip not found")
    return path


def delete_clip(clip_id: str) -> None:
    path = get_clip_path(clip_id)
    try:
        path.unlink()
    except OSError as exc:
        raise CameraError(f"Failed to delete clip: {exc}") from exc


def stop_all_recordings() -> int:
    """Shutdown hook. Returns the number of recordings stopped."""
    with _RECORDS_LOCK:
        cam_ids = [cid for cid, p in _RECORD_PROCS.items() if p.poll() is None]
    n = 0
    for cid in cam_ids:
        try:
            stop_recording(cid)
            n += 1
        except CameraError:
            pass  # intentional: a failure here must not break the caller
    return n
