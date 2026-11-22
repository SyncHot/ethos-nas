"""Cameras service.

Camera store (JSON via `json_store`, schema v2), ffmpeg-based snapshot / live
fallback, motion events and the legacy manual MP4 clips. Continuous recording,
live HLS and motion analysis live in `camera_runner.py`.

Persistence (all under settings.DATA_DIR, recordings under the storage root):
    surveillance.json          — {version: 2, cameras: [...]}
    surveillance_events.json   — {version: 2, events: [...]} newest first, cap 2000
    surveillance/snaps/<camid>.jpg      — snapshot disk cache
    surveillance/events/<ts>_<camid>.jpg — motion event snapshots
    surveillance/clips/<ts>_<camid>.mp4  — legacy manual clips

Camera credentials are split out of the URL and encrypted at rest
(`camera_secrets.py`); every log line and API payload masks them.

All ffmpeg invocations are argument lists (never shell=True). Background
threads are daemons and must never take the app down: every loop iteration is
wrapped.
"""

import logging
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from PIL import Image

from backend.config import settings
from backend.services import camera_secrets, child_limits, json_store
from backend.services.camera_secrets import mask_text, mask_url
from backend.services.notifications_service import emit_event

logger = logging.getLogger("ethos-nas.services.camera_service")

# Kept for modules that imported the old helper from here.
_redact_credentials = mask_text

CAMERAS_PATH: Path = settings.DATA_DIR / "surveillance.json"
EVENTS_PATH: Path = settings.DATA_DIR / "surveillance_events.json"
SNAPS_DIR: Path = settings.DATA_DIR / "surveillance" / "snaps"
EVENTS_IMG_DIR: Path = settings.DATA_DIR / "surveillance" / "events"
CLIPS_DIR: Path = settings.DATA_DIR / "surveillance" / "clips"

MAX_EVENTS = 2000
MAX_VIEWERS = 50
ALLOWED_SCHEMES = ("rtsp", "http", "https")
GRAB_TIMEOUT_S = 8          # snapshot / probe timeout
MOTION_MIN_GAP_S = 10       # default min seconds between events for one camera
MOTION_FRAME_SIZE = (160, 90)
MAX_CLIPS_BYTES = 4 * 1024 ** 3   # safety cap on the legacy clips dir
CLIP_NAME_RE = re.compile(r"^(\d{10})_(cam_[a-f0-9]{12})\.mp4$")
CAMERA_ID_RE = re.compile(r"^cam_[a-f0-9]{12}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
RECORD_MODES = ("off", "continuous", "schedule")

_LOCK = threading.Lock()
_RECORD_PROCS: dict[str, subprocess.Popen] = {}
_RECORD_META: dict[str, dict] = {}   # cam_id → {started_at, path}
_RECORDS_LOCK = threading.Lock()


class CameraError(Exception):
    """User-facing error (bad input / unreachable camera)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _username(user) -> str:
    if isinstance(user, dict):
        return str(user.get("username") or "")
    return str(user or "")


def _role(user) -> str:
    return str(user.get("role") or "user") if isinstance(user, dict) else "user"


def is_admin(user) -> bool:
    return _role(user) == "admin"


# ── Schema v2 helpers ───────────────────────────────────────────────────────

def _new_motion() -> dict:
    return {
        "enabled": False,
        "sensitivity": 0.3,
        "cooldown_s": MOTION_MIN_GAP_S,
        "zones": None,
        "schedule": None,
        "min_cells": 3,
    }


def _new_record() -> dict:
    return {"mode": "off", "schedule": None, "retention_days": None}


def _normalize_camera_v2(cam: dict) -> dict:
    """Make one stored camera match schema v2 without losing operator data."""
    if not isinstance(cam, dict):
        return cam
    url = str(cam.get("url") or "").strip()
    streams = cam.get("streams") if isinstance(cam.get("streams"), dict) else {}
    main = str(streams.get("main") or url).strip()
    sub_raw = streams.get("sub")
    sub = str(sub_raw).strip() if sub_raw else ""
    clean_main, url_user, url_pass = camera_secrets.split_credentials(main)
    clean_sub, sub_user, sub_pass = camera_secrets.split_credentials(sub)
    auth = cam.get("auth") if isinstance(cam.get("auth"), dict) else {}
    auth_user = str(auth.get("user") or url_user or sub_user or "")
    auth_enc = str(auth.get("password_enc") or "")
    if not auth_enc:
        plain = str(auth.get("password") or "") or url_pass or sub_pass
        auth_enc = camera_secrets.encrypt_secret(plain) if plain else ""
    cam.pop("url", None)
    cam.pop("password", None)
    cam["streams"] = {"main": clean_main, "sub": clean_sub or None}
    cam["auth"] = {"user": auth_user, "password_enc": auth_enc} if (auth_user or auth_enc) else None

    motion = cam.get("motion") if isinstance(cam.get("motion"), dict) else {}
    normalized_motion = _new_motion()
    normalized_motion["enabled"] = bool(motion.get("enabled"))
    for key in ("sensitivity", "cooldown_s", "min_cells"):
        if motion.get(key) is not None:
            normalized_motion[key] = motion[key]
    if isinstance(motion.get("zones"), dict):
        normalized_motion["zones"] = motion["zones"]
    if isinstance(motion.get("schedule"), dict):
        normalized_motion["schedule"] = motion["schedule"]
    # Legacy interval_s is meaningful as the event cooldown.
    if motion.get("cooldown_s") is None and motion.get("interval_s"):
        try:
            normalized_motion["cooldown_s"] = max(MOTION_MIN_GAP_S, float(motion["interval_s"]))
        except (TypeError, ValueError):
            pass  # bad legacy value — keep the default
    cam["motion"] = normalized_motion

    record = cam.get("record") if isinstance(cam.get("record"), dict) else {}
    normalized_record = _new_record()
    normalized_record.update({k: v for k, v in record.items() if k in normalized_record})
    if normalized_record.get("mode") not in RECORD_MODES:
        normalized_record["mode"] = "off"
    cam["record"] = normalized_record

    viewers = cam.get("viewers") if isinstance(cam.get("viewers"), list) else []
    cam["viewers"] = [str(v) for v in viewers if isinstance(v, str)][:MAX_VIEWERS]
    cam.setdefault("enabled", True)
    cam.setdefault("onvif", None)
    cam.setdefault("created_at", _now_iso())
    cam.setdefault("updated_at", cam["created_at"])
    return cam


def _migrate_cameras(from_version: int, to_version: int, data: dict) -> dict:
    cameras = data.get("cameras") if isinstance(data.get("cameras"), list) else []
    if from_version < 2:
        cameras = [_normalize_camera_v2(dict(c)) for c in cameras]
        logger.info("Camera store migrated v1→v2 (%d camera(s))", len(cameras))
    return {"version": 2, "cameras": cameras}


def _migrate_events(from_version: int, to_version: int, data: dict) -> dict:
    events = data.get("events") if isinstance(data.get("events"), list) else []
    if from_version < 2:
        for event in events:
            if not isinstance(event, dict):
                continue
            event.setdefault("kind", "motion")
            event.setdefault("score", 0.0)
            event.setdefault("reviewed", False)
            event.setdefault("recording", None)
            event.setdefault("ended_at", None)
    return {"version": 2, "events": events[:MAX_EVENTS]}


_CAMERAS = json_store.get(CAMERAS_PATH, dict, version=2, migrate=_migrate_cameras)
_EVENTS = json_store.get(EVENTS_PATH, dict, version=2, migrate=_migrate_events)


# ── JSON stores ─────────────────────────────────────────────────────────────

def _load_cameras() -> dict:
    data = _CAMERAS.load()
    cameras = data.get("cameras") if isinstance(data, dict) else None
    return {"version": 2, "cameras": cameras if isinstance(cameras, list) else []}


def _save_cameras(store: dict) -> None:
    cameras = store.get("cameras") if isinstance(store, dict) else None
    payload = cameras if isinstance(cameras, list) else []

    def _mutate(data) -> None:
        data.clear()
        data.update({"version": 2, "cameras": payload})

    _CAMERAS.update(_mutate)


def _load_events() -> dict:
    data = _EVENTS.load()
    events = data.get("events") if isinstance(data, dict) else None
    return {"version": 2, "events": events if isinstance(events, list) else []}


def _save_events(store: dict) -> None:
    events = store.get("events") if isinstance(store, dict) else None
    payload = (events if isinstance(events, list) else [])[:MAX_EVENTS]

    def _mutate(data) -> None:
        data.clear()
        data.update({"version": 2, "events": payload})

    _EVENTS.update(_mutate)


# ── ffmpeg helpers ──────────────────────────────────────────────────────────

def _url_input_args(url: str) -> list[str]:
    """Extra ffmpeg args that must precede -i for a given URL scheme."""
    # HTTP-MJPEG carries no container timestamps; without generated ones a
    # single-frame grab ends with "no packets" / EINVAL.
    args = ["-fflags", "+genpts", "-use_wallclock_as_timestamps", "1"]
    if urlparse(url).scheme == "rtsp":
        # -rw_timeout is the global socket timeout (µs); the old RTSP-private
        # -stimeout was removed as a CLI option in ffmpeg 8 and aborts parsing.
        args += ["-rtsp_transport", "tcp", "-rw_timeout", "5000000"]
    return args


def _ffmpeg_grab(url: str, out_path: Path, scale: Optional[str] = None, timeout: int = GRAB_TIMEOUT_S) -> tuple[bool, str]:
    """Grab a single JPEG frame from url. Returns (ok, detail). Never raises."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    cmd += _url_input_args(url)
    cmd += ["-i", url]
    if scale:
        cmd += ["-vf", f"scale={scale}"]
    cmd += ["-frames:v", "1", "-q:v", "4", "-y", str(out_path)]
    try:
        r = subprocess.run(child_limits.wrap(cmd, mem_mb=settings.CHILD_PROBE_MEM_LIMIT_MB),
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
        child_limits.wrap(cmd, mem_mb=settings.CHILD_PROBE_MEM_LIMIT_MB),
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


# ── Validation ──────────────────────────────────────────────────────────────

def _validate_url(url: str, label: str = "URL") -> None:
    text = (url or "").strip()
    if not text:
        raise CameraError(f"{label} must not be empty")
    scheme = urlparse(text).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise CameraError(
            f"Unsupported {label} scheme '{scheme or 'missing'}' — use rtsp://, http:// or https://")


def _validate_name(name: str) -> None:
    text = (name or "").strip()
    if not text:
        raise CameraError("Name must not be empty")
    if len(text) > 60:
        raise CameraError("Name too long (max 60 chars)")


def _validate_schedule(schedule, label: str) -> None:
    if schedule is None:
        return
    if not isinstance(schedule, dict):
        raise CameraError(f"{label} must be an object")
    days = schedule.get("days")
    if not isinstance(days, list) or not days or any(
            not isinstance(d, int) or d < 0 or d > 6 for d in days):
        raise CameraError(f"{label}.days must be a non-empty list of 0–6 (Mon=0)")
    for key in ("from", "to"):
        if not TIME_RE.match(str(schedule.get(key) or "")):
            raise CameraError(f"{label}.{key} must be HH:MM")


def _validate_zones(zones, label: str = "motion.zones") -> None:
    if zones is None:
        return
    if not isinstance(zones, dict):
        raise CameraError(f"{label} must be an object")
    cols, rows = zones.get("cols", 16), zones.get("rows", 9)
    if not isinstance(cols, int) or not isinstance(rows, int) or not (2 <= cols <= 64) or not (2 <= rows <= 36):
        raise CameraError(f"{label} grid must be 2–64 × 2–36")
    mask = zones.get("mask")
    if isinstance(mask, str):
        mask = [1 if ch in "1xX" else 0 for ch in mask]
    if not isinstance(mask, list) or len(mask) != cols * rows:
        raise CameraError(f"{label}.mask must have cols×rows = {cols * rows} cells")
    if not any(bool(v) for v in mask):
        raise CameraError(f"{label} must keep at least one cell enabled")


def _validate_motion(motion: dict) -> None:
    if not isinstance(motion, dict):
        raise CameraError("motion must be an object")
    # Legacy clients send interval_s; accept it as the cooldown.
    if motion.get("cooldown_s") is None and motion.get("interval_s") is not None:
        motion["cooldown_s"] = motion["interval_s"]
    sens = motion.get("sensitivity", 0.3)
    if not isinstance(sens, (int, float)) or not (0.02 <= float(sens) <= 0.95):
        raise CameraError("motion.sensitivity must be between 0.02 and 0.95")
    cooldown = motion.get("cooldown_s", MOTION_MIN_GAP_S)
    if not isinstance(cooldown, (int, float)) or not (1 <= float(cooldown) <= 3600):
        raise CameraError("motion.cooldown_s must be between 1 and 3600")
    min_cells = motion.get("min_cells", 3)
    if not isinstance(min_cells, (int, float)) or not (1 <= float(min_cells) <= 500):
        raise CameraError("motion.min_cells must be between 1 and 500")
    _validate_zones(motion.get("zones"))
    _validate_schedule(motion.get("schedule"), "motion.schedule")


def _validate_record(record: dict) -> None:
    if not isinstance(record, dict):
        raise CameraError("record must be an object")
    if record.get("mode") not in RECORD_MODES:
        raise CameraError(f"record.mode must be one of {', '.join(RECORD_MODES)}")
    _validate_schedule(record.get("schedule"), "record.schedule")
    days = record.get("retention_days")
    if days is not None and (not isinstance(days, (int, float)) or not (0 <= float(days) <= 365)):
        raise CameraError("record.retention_days must be 0–365 (null = global default)")


def _validate_viewers(viewers) -> list[str]:
    if viewers is None:
        return []
    if not isinstance(viewers, list):
        raise CameraError("viewers must be a list of usernames")
    clean: list[str] = []
    for raw in viewers[:MAX_VIEWERS]:
        name = str(raw).strip()
        if name and name not in clean:
            clean.append(name[:64])
    return clean


# ── Access control ──────────────────────────────────────────────────────────

def can_view(cam: dict, user) -> bool:
    if is_admin(user):
        return True
    username = _username(user)
    return bool(username) and username in (cam.get("viewers") or [])


def visible_cameras(user) -> list[dict]:
    return [c for c in _load_cameras()["cameras"] if can_view(c, user)]


def get_camera(cam_id: str, user=None) -> dict:
    """Return one camera (runtime fields included) or raise CameraError('Camera not found')."""
    for cam in _load_cameras()["cameras"]:
        if cam.get("id") == cam_id:
            if user is not None and not can_view(cam, user):
                raise CameraError("Camera not found")
            return _with_runtime(cam)
    raise CameraError("Camera not found")


def _with_runtime(cam: dict) -> dict:
    out = dict(cam)
    try:
        from backend.services import camera_runner

        status = camera_runner.status_for(cam["id"])
    except Exception:  # noqa: BLE001 — status is cosmetic, never break listing
        status = {}
    out["status"] = status
    out["last_error"] = status.get("last_error", "")
    out["recording"] = bool(status.get("recording"))
    return out


# ── Serialization ───────────────────────────────────────────────────────────

def stream_urls(cam: dict, which: str = "main") -> str:
    """ffmpeg-ready URL (credentials folded in) for `main` or `sub`."""
    streams = cam.get("streams") or {}
    url = str(streams.get(which) or "").strip()
    if not url and which == "sub":
        url = str(streams.get("main") or "")
    if not url:
        url = str(streams.get("main") or "")
    auth = cam.get("auth") or {}
    return camera_secrets.merge_credentials(
        url, auth.get("user", ""), camera_secrets.decrypt_secret(auth.get("password_enc", "")))


def public_camera(cam: dict) -> dict:
    """API-safe camera payload: masked URLs, no password material."""
    out = dict(cam)
    streams = cam.get("streams") or {}
    main = mask_url(streams.get("main", ""))
    sub = mask_url(streams.get("sub") or "")
    out["streams"] = {"main": main, "sub": sub or None}
    out["url"] = main          # legacy field the old UI/tests read
    # A camera named after its URL must not leak `user:password@` to the UI.
    out["name"] = mask_text(cam.get("name") or "")
    auth = cam.get("auth") or {}
    out["auth"] = ({"user": auth.get("user", ""),
                    "has_password": bool(auth.get("password_enc"))} if auth else None)
    return out


# ── Camera CRUD ─────────────────────────────────────────────────────────────

def _new_camera(name: str, main_url: str, sub_url: str | None = None,
                username: str = "", password: str = "") -> dict:
    clean_main, url_user, url_pass = camera_secrets.split_credentials(main_url)
    clean_sub, sub_user, sub_pass = camera_secrets.split_credentials(sub_url or "")
    user = username or url_user or sub_user
    secret = password or url_pass or sub_pass
    now = _now_iso()
    return {
        "id": f"cam_{uuid.uuid4().hex[:12]}",
        "name": name.strip(),
        "enabled": True,
        "streams": {"main": clean_main, "sub": clean_sub or None},
        "auth": ({"user": user, "password_enc": camera_secrets.encrypt_secret(secret)}
                 if (user or secret) else None),
        "record": _new_record(),
        "motion": _new_motion(),
        "viewers": [],
        "onvif": None,
        "created_at": now,
        "updated_at": now,
    }


def list_cameras(user=None) -> list[dict]:
    cameras = _load_cameras()["cameras"]
    if user is not None and not is_admin(user):
        cameras = [c for c in cameras if can_view(c, user)]
    return [_with_runtime(c) for c in cameras]


def add_camera(name: str, url: str = "", *, sub_url: str | None = None,
               username: str = "", password: str = "") -> dict:
    _validate_name(name)
    _validate_url(url, "Stream URL")
    if sub_url:
        _validate_url(sub_url, "Sub-stream URL")
    clean_main, _, _ = camera_secrets.split_credentials(url)
    with _LOCK:
        store = _load_cameras()
        for cam in store["cameras"]:
            if (cam.get("streams") or {}).get("main", "").strip().lower() == clean_main.strip().lower():
                raise CameraError(f"A camera with this URL already exists ({cam['name']})")
        cam = _new_camera(name, url, sub_url, username, password)
        store["cameras"].append(cam)
        _save_cameras(store)
    if cam["enabled"]:
        _start_camera(cam)
    logger.info("Camera added: %s (%s, record=%s)", cam["name"], cam["id"],
                cam["record"]["mode"])
    return cam


def update_camera(cam_id: str, fields: dict) -> dict:
    fields = dict(fields or {})
    with _LOCK:
        store = _load_cameras()
        cam = next((c for c in store["cameras"] if c["id"] == cam_id), None)
        if cam is None:
            raise CameraError("Camera not found")
        if fields.get("name") is not None:
            _validate_name(fields["name"])
            cam["name"] = fields["name"].strip()
        streams = fields.get("streams") if isinstance(fields.get("streams"), dict) else {}
        new_main = fields.get("url") if fields.get("url") is not None else streams.get("main")
        new_sub = fields.get("sub_url") if fields.get("sub_url") is not None else streams.get("sub")
        if new_main is not None:
            _validate_url(new_main, "Stream URL")
            clean_main, url_user, url_pass = camera_secrets.split_credentials(new_main)
            for other in store["cameras"]:
                if other["id"] != cam_id and (other.get("streams") or {}).get(
                        "main", "").strip().lower() == clean_main.strip().lower():
                    raise CameraError(f"A camera with this URL already exists ({other['name']})")
            cam.setdefault("streams", {})["main"] = clean_main
            if url_user or url_pass:
                auth = cam.get("auth") or {"user": "", "password_enc": ""}
                auth["user"] = auth.get("user") or url_user
                if url_pass:
                    auth["password_enc"] = camera_secrets.encrypt_secret(url_pass)
                cam["auth"] = auth
        if new_sub is not None:
            if str(new_sub).strip():
                _validate_url(new_sub, "Sub-stream URL")
                clean_sub, sub_user, sub_pass = camera_secrets.split_credentials(new_sub)
                cam.setdefault("streams", {})["sub"] = clean_sub
                if sub_user or sub_pass:
                    auth = cam.get("auth") or {"user": "", "password_enc": ""}
                    auth["user"] = auth.get("user") or sub_user
                    if sub_pass:
                        auth["password_enc"] = camera_secrets.encrypt_secret(sub_pass)
                    cam["auth"] = auth
            else:
                cam.setdefault("streams", {})["sub"] = None
        if fields.get("username") is not None or fields.get("password") is not None:
            auth = cam.get("auth") or {"user": "", "password_enc": ""}
            if fields.get("username") is not None:
                auth["user"] = str(fields["username"]).strip()[:64]
            if fields.get("password"):
                auth["password_enc"] = camera_secrets.encrypt_secret(str(fields["password"]))
            cam["auth"] = auth if (auth.get("user") or auth.get("password_enc")) else None
        if fields.get("enabled") is not None:
            cam["enabled"] = bool(fields["enabled"])
        if isinstance(fields.get("motion"), dict):
            merged = {**(cam.get("motion") or _new_motion()), **fields["motion"]}
            _validate_motion(merged)
            zones = merged.get("zones")
            if isinstance(zones, dict) and isinstance(zones.get("mask"), str):
                zones["mask"] = [1 if ch in "1xX" else 0 for ch in zones["mask"]]
            cam["motion"] = merged
        if isinstance(fields.get("record"), dict):
            merged_record = {**(cam.get("record") or _new_record()), **fields["record"]}
            _validate_record(merged_record)
            cam["record"] = merged_record
        if fields.get("viewers") is not None:
            cam["viewers"] = _validate_viewers(fields["viewers"])
        if isinstance(fields.get("onvif"), dict):
            cam["onvif"] = fields["onvif"]
        cam["updated_at"] = _now_iso()
        _save_cameras(store)

    # Restart the pipeline so stream/record/motion changes take effect.
    _stop_camera(cam_id)
    if cam.get("enabled"):
        _start_camera(cam)
    logger.info("Camera updated: %s (%s, enabled=%s, record=%s)", cam.get("name"), cam_id,
                cam.get("enabled"), (cam.get("record") or {}).get("mode"))
    return cam


def delete_camera(cam_id: str) -> None:
    with _LOCK:
        store = _load_cameras()
        cam = next((c for c in store["cameras"] if c["id"] == cam_id), None)
        if cam is None:
            raise CameraError("Camera not found")
        store["cameras"] = [c for c in store["cameras"] if c["id"] != cam_id]
        _save_cameras(store)
    _stop_camera(cam_id)
    _stop_legacy_recording(cam_id)
    # Remove cached images (snapshot + this camera's event snapshots).
    snap = SNAPS_DIR / f"{cam_id}.jpg"
    try:
        if snap.exists():
            snap.unlink()
    except OSError:
        pass  # best-effort cleanup — nothing to report
    with _LOCK:
        ev_store = _load_events()
        kept, removed = [], []
        for event in ev_store["events"]:
            if event.get("camera_id") == cam_id:
                removed.append(event)
            else:
                kept.append(event)
        _save_events({"version": 2, "events": kept})
    for event in removed:
        _delete_event_image(event)
    try:
        from backend.services import camera_storage

        camera_storage.delete_camera_data(cam_id)
    except Exception:  # noqa: BLE001 — storage cleanup must not block the delete
        logger.error("Could not remove recordings for %s", cam_id, exc_info=True)
    logger.info("Camera deleted: %s (%s)", cam.get("name"), cam_id)


def _start_camera(cam: dict) -> None:
    try:
        from backend.services import camera_runner

        camera_runner.start_camera(cam)
    except Exception:  # noqa: BLE001 — a bad camera must not break CRUD
        logger.error("Could not start camera pipeline for %s", cam.get("id"), exc_info=True)


def _stop_camera(cam_id: str) -> None:
    try:
        from backend.services import camera_runner

        camera_runner.stop_camera(cam_id)
    except Exception:  # noqa: BLE001
        logger.error("Could not stop camera pipeline for %s", cam_id, exc_info=True)


# ── Schedules ───────────────────────────────────────────────────────────────

def schedule_active(schedule, now: datetime | None = None) -> bool:
    """True when `now` falls inside a {days, from, to} window (overnight aware)."""
    if not isinstance(schedule, dict):
        return True
    moment = now or datetime.now()
    days = schedule.get("days") or []
    start, end = str(schedule.get("from") or "00:00"), str(schedule.get("to") or "23:59")
    if start == "00:00" and end == "23:59":
        return moment.weekday() in days
    hhmm = moment.strftime("%H:%M")
    weekday = moment.weekday()
    if start <= end:
        return weekday in days and start <= hhmm <= end
    # Overnight window: the slot belongs to the day it started.
    if hhmm >= start:
        return weekday in days
    return ((weekday - 1) % 7) in days and hhmm <= end


def recording_active(cam: dict, now: datetime | None = None) -> bool:
    record = cam.get("record") or {}
    mode = record.get("mode", "off")
    if mode == "continuous":
        return True
    if mode == "schedule":
        return schedule_active(record.get("schedule"), now)
    return False


def motion_active(cam: dict, now: datetime | None = None) -> bool:
    motion = cam.get("motion") or {}
    if not motion.get("enabled"):
        return False
    return schedule_active(motion.get("schedule"), now)


# ── Connection test / snapshot / live ───────────────────────────────────────

def test_connection(url: str, username: str = "", password: str = "") -> dict:
    """Probe a camera URL (≤8 s). Returns {ok, detail} — never raises."""
    try:
        _validate_url(url)  # scheme check only; bad host must not raise either
    except CameraError as exc:
        return {"ok": False, "detail": str(exc)}
    target = camera_secrets.merge_credentials(url, username, password)
    try:
        with tempfile.TemporaryDirectory(prefix="ethos-cam-") as td:
            ok, detail = _ffmpeg_grab(target, Path(td) / "probe.jpg", scale="160:-1",
                                      timeout=GRAB_TIMEOUT_S)
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
        if "401" in detail or "403" in detail:
            detail += " — the camera rejected the credentials; check the username/password."
        return {"ok": False, "detail": f"Cannot reach camera: {detail}"}
    except CameraError as exc:
        return {"ok": False, "detail": str(exc)}


def get_snapshot(cam_id: str, width: int = 480, refresh: bool = False, user=None) -> Path:
    """Return a JPEG path for the camera (disk-cached). Raises CameraError on grab failure."""
    cam = get_camera(cam_id, user)

    SNAPS_DIR.mkdir(parents=True, exist_ok=True)
    cache = SNAPS_DIR / f"{cam_id}.jpg"
    if refresh or not (cache.exists() and cache.stat().st_size > 0):
        ok, detail = _ffmpeg_grab(stream_urls(cam, "sub"), cache)
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


def live_stream(cam_id: str, user=None):
    """MJPEG chunk generator (legacy). Raises CameraError if unknown/forbidden."""
    cam = get_camera(cam_id, user)
    return _live_stream(stream_urls(cam, "sub"))


# ── Events ──────────────────────────────────────────────────────────────────

def list_events(limit: int = 50, camera_id: Optional[str] = None, *, user=None,
                day: str | None = None, reviewed: bool | None = None) -> list[dict]:
    events = list(_load_events()["events"])
    if user is not None and not is_admin(user):
        allowed = {c["id"] for c in visible_cameras(user)}
        events = [e for e in events if e.get("camera_id") in allowed]
    if camera_id:
        events = [e for e in events if e.get("camera_id") == camera_id]
    if day:
        events = [e for e in events if str(e.get("at", ""))[:10] == day]
    if reviewed is not None:
        events = [e for e in events if bool(e.get("reviewed")) is bool(reviewed)]
    return events[: max(1, min(limit, MAX_EVENTS))]


def get_event(event_id: str, user=None) -> dict:
    for event in list_events(MAX_EVENTS, user=user):
        if event.get("id") == event_id:
            return event
    raise CameraError("Event not found")


def _delete_event_image(event: dict) -> None:
    image = event.get("image")
    if not image:
        return
    try:
        path = (EVENTS_IMG_DIR / image).resolve()
        path.relative_to(EVENTS_IMG_DIR.resolve())
        if path.exists():
            path.unlink()
    except (OSError, ValueError):
        pass  # best-effort cleanup — nothing to report


def delete_event(event_id: str) -> None:
    with _LOCK:
        store = _load_events()
        gone = [e for e in store["events"] if e.get("id") == event_id]
        if not gone:
            raise CameraError("Event not found")
        _save_events({"version": 2,
                      "events": [e for e in store["events"] if e.get("id") != event_id]})
    for event in gone:
        _delete_event_image(event)


def clear_events() -> int:
    with _LOCK:
        store = _load_events()
        events = store["events"]
        _save_events({"version": 2, "events": []})
    for event in events:
        _delete_event_image(event)
    return len(events)


def set_event_reviewed(event_id: str, reviewed: bool = True) -> dict:
    with _LOCK:
        store = _load_events()
        found = None
        for event in store["events"]:
            if event.get("id") == event_id:
                event["reviewed"] = bool(reviewed)
                found = event
                break
        if found is None:
            raise CameraError("Event not found")
        _save_events(store)
    return found


def record_motion_event(cam: dict, image_rel: Optional[str], *,
                        score: float = 0.0, recording: Optional[dict] = None,
                        notify: bool = True) -> dict:
    """Append a motion event (newest first, capped) + Notification Center entry."""
    safe_name = mask_text(cam.get("name") or "") or "camera"
    event = {
        "id": f"mev_{uuid.uuid4().hex[:12]}",
        "camera_id": cam["id"],
        "camera_name": safe_name,
        "kind": "motion",
        "at": _now_iso(),
        "ended_at": None,
        "score": round(float(score), 3),
        "image": image_rel,
        "recording": recording,
        "reviewed": False,
    }
    with _LOCK:
        store = _load_events()
        store["events"].insert(0, event)
        _save_events(store)
    if notify:
        try:
            from backend.services import camera_settings

            if camera_settings.load().get("notify_motion", True):
                emit_event(
                    "warning", f"Motion detected — {safe_name}",
                    f"{round(float(score) * 100)}% of the watched area changed",
                    source="surveillance",
                    action={"label": "View", "appId": "surveillance", "arg": event["id"]},
                    dedupe_key=f"camera:{cam['id']}:motion",
                    users=(cam.get("viewers") or []) or None,
                )
        except Exception:  # noqa: BLE001 — a notification failure must not lose the event
            logger.error("Could not publish motion notification for %s", cam["id"],
                         exc_info=True)
    return event


def event_image_path(event_id: str, user=None) -> Path:
    event = get_event(event_id, user)
    image = event.get("image")
    if not image:
        raise CameraError("Event has no image")
    path = (EVENTS_IMG_DIR / image).resolve()
    try:
        path.relative_to(EVENTS_IMG_DIR.resolve())
    except ValueError as exc:
        raise CameraError("Event has no image") from exc
    if not path.exists():
        raise CameraError("Image no longer on disk")
    return path


# ── Pipeline lifecycle (delegates to camera_runner) ─────────────────────────

def start_all_motion_threads() -> int:
    """Boot hook: start a pipeline for every enabled camera. Returns the count."""
    from backend.services import camera_runner

    return camera_runner.start_all()


def stop_all_motion_threads() -> None:
    from backend.services import camera_runner

    camera_runner.stop_all()


def start_motion_thread(cam: dict) -> None:
    """Compatibility shim: start the camera pipeline for one camera."""
    from backend.services import camera_runner

    camera_runner.start_camera(cam)


def stop_motion_thread(cam_id: str) -> None:
    from backend.services import camera_runner

    camera_runner.stop_camera(cam_id)


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


def start_recording(cam_id: str, user=None) -> dict:
    """Start a manual MP4 clip (legacy). Continuous recording is `record.mode`.

    Re-encodes (libx264, no audio) so any source — RTSP H.264 or HTTP-MJPEG —
    yields a universally playable MP4. Raises CameraError on bad state.
    """
    cam = get_camera(cam_id, user)
    source = stream_urls(cam, "main")
    with _RECORDS_LOCK:
        if cam_id in _RECORD_PROCS and _RECORD_PROCS[cam_id].poll() is None:
            raise CameraError(f"{cam['name']} is already recording")
        used = _clips_used_bytes()
        if used > MAX_CLIPS_BYTES:
            raise CameraError(
                f"Clip storage full ({used / 1024**3:.1f} GiB of {MAX_CLIPS_BYTES // 1024**3} GiB cap) — delete old clips first")

        out = _clips_dir() / f"{int(time.time())}_{cam_id}.mp4"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        cmd += _url_input_args(source)
        cmd += ["-i", source, "-an",
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


def _stop_legacy_recording(cam_id: str) -> None:
    """Best-effort stop of a manual clip (camera deleted / shutdown)."""
    with _RECORDS_LOCK:
        proc = _RECORD_PROCS.pop(cam_id, None)
        _RECORD_META.pop(cam_id, None)
    if proc is not None and proc.poll() is None:
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=6)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass  # already gone


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
