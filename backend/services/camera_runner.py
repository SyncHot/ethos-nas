"""Cameras — recording/live pipelines.

One long-lived ffmpeg per stream instead of the old "spawn a process every few
seconds" motion loop:

    live stream  → fMP4 HLS window (1 s segments, 6 in the playlist)
                 → rawvideo pipe (160×90 gray, 1 fps) for motion detection
    main stream  → fMP4 HLS archive, one directory per run:
                   <storage>/recordings/<cam>/<YYYY-MM-DD>/<HH-MM-SS>/

`-c:v copy` is used whenever the source is H.264 (HEVC for the archive), so a
five-camera install costs almost no CPU. The motion detector consumes the raw
frames the live process already decodes — no extra process, no extra decode,
and every camera connection is long-lived (some cameras refuse a new session
every few seconds, which is exactly what the old loop did).

A per-camera supervisor thread keeps the processes alive (backoff 5/15/60 s),
rolls the archive directory at midnight and drains motion events. A single
janitor thread enforces retention/quota through `camera_storage.purge()`.
"""

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from backend.services import camera_service, camera_settings, camera_storage, shutdown_state
from backend.services.child_limits import wrap as limit_child

logger = logging.getLogger("ethos-nas.services.camera_runner")

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
FRAME_W, FRAME_H = 160, 90
FRAME_BYTES = FRAME_W * FRAME_H
MOTION_FPS = 1
LIVE_SEGMENT_S = 1
LIVE_LIST_SIZE = 6
LIVE_STALE_S = 10
RESTART_BACKOFF = (5, 15, 60)
SUPERVISOR_TICK_S = 2
JANITOR_INTERVAL_S = 300
CODEC_PROBE_TIMEOUT_S = 8
COPY_CODECS = ("h264", "hevc")

_LOCK = threading.Lock()
_PIPELINES: dict[str, "CameraPipeline"] = {}
_JANITOR: Optional["_Janitor"] = None


def status_for(cam_id: str) -> dict:
    with _LOCK:
        pipeline = _PIPELINES.get(cam_id)
    return pipeline.status() if pipeline else {"online": False, "recording": False,
                                               "last_error": "", "restarts": 0}


# ── ffmpeg helpers ──────────────────────────────────────────────────────────

def _input_args(url: str) -> list[str]:
    # HTTP-MJPEG carries no container timestamps; generate them or the encoders
    # abort with EINVAL before the first packet.
    args: list[str] = ["-fflags", "+genpts", "-use_wallclock_as_timestamps", "1"]
    if url.startswith("rtsp://") or url.startswith("rtsps://"):
        args += ["-rtsp_transport", "tcp", "-rw_timeout", "5000000"]
    return args


def _probe(url: str) -> dict:
    """Codec/resolution via ffprobe (never raises)."""
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-select_streams", "v:0",
           "-show_entries", "stream=codec_name,width,height", *_input_args(url), "-i", url]
    try:
        proc = subprocess.run(limit_child(cmd, mem_mb=256), capture_output=True,
                              text=True, timeout=CODEC_PROBE_TIMEOUT_S)
        data = json.loads(proc.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        return {"codec": str(stream.get("codec_name") or ""),
                "width": int(stream.get("width") or 0),
                "height": int(stream.get("height") or 0)}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.debug("ffprobe failed for a camera stream: %s", camera_service.mask_text(exc))
        return {}


def _video_args(codec: str, *, allow_copy: bool = True, fps: int = 15) -> list[str]:
    if allow_copy and codec in COPY_CODECS:
        return ["-c:v", "copy"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-crf", "26", "-g", "50", "-sc_threshold", "0",
            "-fps_mode", "cfr", "-r", str(max(1, int(fps)))]


def _live_cmd(url: str, codec: str, out_dir: Path) -> list[str]:
    """Live HLS + a raw gray frame pipe on fd 3 (both from one decode)."""
    return [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
        *_input_args(url), "-i", url,
        "-map", "0:v:0", *_video_args(codec), "-an",
        "-f", "hls",
        "-hls_time", str(LIVE_SEGMENT_S), "-hls_list_size", str(LIVE_LIST_SIZE),
        "-hls_flags", "delete_segments+independent_segments+temp_file",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(out_dir / "seg%05d.m4s"),
        str(out_dir / "index.m3u8"),
        "-map", "0:v:0",
        "-vf", f"fps={MOTION_FPS},scale={FRAME_W}:{FRAME_H},format=gray",
        "-an", "-f", "rawvideo", "pipe:1",
    ]


def _archive_cmd(url: str, codec: str, out_dir: Path, segment_s: int) -> list[str]:
    """Archive HLS, one playlist per run directory (append_list is unsafe across restarts)."""
    return [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
        *_input_args(url), "-i", url,
        "-map", "0:v:0", *_video_args(codec), "-an",
        "-f", "hls",
        "-hls_time", str(max(10, int(segment_s))), "-hls_list_size", "0",
        "-hls_flags", "independent_segments+temp_file+program_date_time",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", str(out_dir / "seg%05d.m4s"),
        str(out_dir / "index.m3u8"),
    ]


def _segment_stats(directory: Path) -> tuple[int, int, float]:
    """(total bytes, segment count, newest mtime) for a playlist directory."""
    total, count, newest = 0, 0, 0.0
    try:
        for item in directory.glob("*.m4s"):
            try:
                stat = item.stat()
            except OSError:
                continue  # segment rotated away between glob and stat
            total += stat.st_size
            count += 1
            newest = max(newest, stat.st_mtime)
    except OSError:
        pass  # missing directory means "no segments yet"
    return total, count, newest


def _stderr_tail(path: Path, limit: int = 300) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return camera_service.mask_text(lines[-1][:limit]) if lines else ""


# ── Motion detection ────────────────────────────────────────────────────────

class MotionDetector:
    """Cell-grid frame diff honouring the camera's mask and cooldown."""

    def __init__(self, cam: dict):
        self.cam = cam
        motion = cam.get("motion") or {}
        self.sensitivity = float(motion.get("sensitivity", 0.3))
        self.min_cells = int(motion.get("min_cells", 3))
        self.cooldown = float(motion.get("cooldown_s", camera_service.MOTION_MIN_GAP_S))
        zones = motion.get("zones") or {}
        self.cols = int(zones.get("cols") or 16)
        self.rows = int(zones.get("rows") or 9)
        mask = zones.get("mask")
        if isinstance(mask, list) and len(mask) == self.cols * self.rows:
            self.mask = [bool(v) for v in mask]
        else:
            self.mask = [True] * (self.cols * self.rows)
        self.enabled_cells = sum(1 for v in self.mask if v) or 1
        self._prev: Optional[bytes] = None
        self._last_event = 0.0
        self._cell_w = FRAME_W / self.cols
        self._cell_h = FRAME_H / self.rows

    def evaluate(self, frame: bytes) -> tuple[bool, float]:
        """Return (triggered, score) for one frame."""
        prev, self._prev = self._prev, frame
        if prev is None or len(prev) != len(frame):
            return False, 0.0
        threshold = 8.0 + self.sensitivity * 200.0
        changed = 0
        for row in range(self.rows):
            y0 = int(row * self._cell_h)
            y1 = max(y0 + 1, int((row + 1) * self._cell_h))
            for col in range(self.cols):
                if not self.mask[row * self.cols + col]:
                    continue
                x0 = int(col * self._cell_w)
                x1 = max(x0 + 1, int((col + 1) * self._cell_w))
                total = 0
                pixels = 0
                for y in range(y0, y1):
                    offset = y * FRAME_W
                    for x in range(x0, x1):
                        total += abs(frame[offset + x] - prev[offset + x])
                        pixels += 1
                if pixels and (total / pixels) >= threshold:
                    changed += 1
        score = changed / self.enabled_cells
        if changed < self.min_cells:
            return False, score
        now = time.time()
        if (now - self._last_event) < self.cooldown:
            return False, score
        self._last_event = now
        return True, score


# ── Pipeline ────────────────────────────────────────────────────────────────

@dataclass
class _Proc:
    name: str
    process: subprocess.Popen
    log_path: Path
    started_at: float
    detail: dict = field(default_factory=dict)


class CameraPipeline(threading.Thread):
    """Supervisor for one camera: live HLS, optional archive, motion events."""

    def __init__(self, cam: dict):
        super().__init__(name=f"camera-{cam['id']}", daemon=True)
        self.cam = dict(cam)
        self.cam_id = cam["id"]
        self._stop = threading.Event()
        self._live: Optional[_Proc] = None
        self._archive: Optional[_Proc] = None
        self._archive_day = ""
        self._frame_reader: Optional[threading.Thread] = None
        self._detector = MotionDetector(self.cam)
        self._pending: list[tuple[float, float]] = []   # (at, score)
        self._restarts = 0
        self._fails = {"live": 0, "archive": 0}
        self._next_try = {"live": 0.0, "archive": 0.0}
        self._last_error = ""
        self._last_frame = 0.0
        self._codec = ""
        self._resolution = ""
        self._recording = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        total, count, newest = _segment_stats(camera_storage.camera_live_dir(self.cam_id))
        online = (time.time() - self._last_frame) < LIVE_STALE_S if self._last_frame else False
        bitrate = int(total * 8 / max(1, count * LIVE_SEGMENT_S) / 1000) if count else 0
        return {
            "online": online,
            "recording": self._recording,
            "last_error": self._last_error,
            "restarts": self._restarts,
            "bitrate_kbps": bitrate,
            "codec": self._codec,
            "resolution": self._resolution,
            "last_frame_at": (datetime.fromtimestamp(self._last_frame, tz=timezone.utc)
                              .isoformat(timespec="seconds") if self._last_frame else None),
            "live_playlist": f"/api/surveillance/cameras/{self.cam_id}/hls/index.m3u8",
        }

    # ── supervisor ─────────────────────────────────────────────────────────

    def run(self) -> None:
        logger.info("Camera pipeline started: %s (%s)", self.cam.get("name"), self.cam_id)
        if not self._codec:
            probe = _probe(_live_source(self.cam))
            self._codec = probe.get("codec", "")
            if probe.get("width"):
                self._resolution = f"{probe['width']}x{probe['height']}"
        while not self._stop.is_set() and not shutdown_state.is_set():
            try:
                self._ensure_live()
                self._ensure_archive()
                self._drain_events()
                if self._live and self._live.process.poll() is not None:
                    self._handle_exit("live")
                if self._archive and self._archive.process.poll() is not None:
                    self._handle_exit("archive")
            except Exception:  # noqa: BLE001 — a supervisor iteration must never die
                logger.error("Camera supervisor error for %s", self.cam_id, exc_info=True)
            self._stop.wait(SUPERVISOR_TICK_S)
        self._shutdown()

    def _shutdown(self) -> None:
        for name in ("live", "archive"):
            proc = getattr(self, f"_{name}")
            if proc and proc.process.poll() is None:
                try:
                    proc.process.send_signal(2)  # SIGINT: let ffmpeg finalize segments
                    proc.process.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        proc.process.kill()
                    except OSError:
                        pass  # already gone
        self._recording = False
        logger.info("Camera pipeline stopped: %s (%s)", self.cam.get("name"), self.cam_id)

    # ── process management ─────────────────────────────────────────────────

    def _ensure_live(self) -> None:
        if self._live and self._live.process.poll() is None:
            return
        if time.time() < self._next_try["live"]:
            return
        url = _live_source(self.cam)
        out_dir = camera_storage.camera_live_dir(self.cam_id)
        log_path = out_dir / "ffmpeg.log"
        self._truncate(log_path)
        cmd = _live_cmd(url, self._codec, out_dir)
        try:
            process = subprocess.Popen(
                limit_child(cmd, mem_mb=1024),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=open(log_path, "ab"))
        except OSError as exc:
            self._fail("live", f"{type(exc).__name__}: {exc}")
            return
        self._live = _Proc("live", process, log_path, time.time())
        self._frame_reader = threading.Thread(
            target=self._read_frames, args=(process.stdout,),
            name=f"frames-{self.cam_id}", daemon=True)
        self._frame_reader.start()

    def _ensure_archive(self) -> None:
        active = camera_service.recording_active(self.cam) if self.cam.get("enabled") else False
        if not active:
            if self._archive and self._archive.process.poll() is None:
                self._stop_proc("archive")
            self._recording = False
            return
        day = datetime.now().strftime("%Y-%m-%d")
        if self._archive and self._archive.process.poll() is None and self._archive_day == day:
            self._recording = True
            return
        if self._archive and self._archive.process.poll() is None:
            self._stop_proc("archive")      # midnight rollover
        if time.time() < self._next_try["archive"]:
            return
        segment_s = camera_settings.load()["segment_s"]
        run = datetime.now().strftime("%H-%M-%S")
        try:
            out_dir = camera_storage.run_dir(self.cam_id, day, run)
            out_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            self._fail("archive", f"Cannot create recordings folder: {exc}")
            return
        log_path = out_dir / "ffmpeg.log"
        cmd = _archive_cmd(camera_service.stream_urls(self.cam, "main"), self._codec,
                           out_dir, segment_s)
        try:
            process = subprocess.Popen(
                limit_child(cmd, mem_mb=768), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=open(log_path, "ab"))
        except OSError as exc:
            self._fail("archive", f"{type(exc).__name__}: {exc}")
            return
        self._archive = _Proc("archive", process, log_path, time.time(), {"day": day})
        self._archive_day = day
        self._fails["archive"] = 0
        self._next_try["archive"] = 0.0
        self._recording = True
        logger.info("Recording started: %s → %s", self.cam_id, out_dir)

    def _stop_proc(self, name: str) -> None:
        proc = getattr(self, f"_{name}", None)
        if not proc:
            return
        if proc.process.poll() is None:
            try:
                proc.process.send_signal(2)
                proc.process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.process.kill()
                except OSError:
                    pass  # already gone
        setattr(self, f"_{name}", None)
        if name == "archive":
            self._recording = False

    def _handle_exit(self, name: str) -> None:
        proc = getattr(self, f"_{name}")
        if not proc:
            return
        code = proc.process.returncode
        tail = _stderr_tail(proc.log_path)
        setattr(self, f"_{name}", None)
        if name == "archive":
            self._recording = False
        if self._stop.is_set() or shutdown_state.is_set():
            return
        self._fail(name, tail or f"ffmpeg exited with {code}")

    def _fail(self, name: str, detail: str) -> None:
        self._fails[name] = self._fails.get(name, 0) + 1
        attempt = min(self._fails[name] - 1, len(RESTART_BACKOFF) - 1)
        self._next_try[name] = time.time() + RESTART_BACKOFF[attempt]
        self._restarts += 1
        self._last_error = f"{name}: {detail}"
        logger.warning("Camera %s (%s) %s failed (%d): %s", self.cam.get("name"), self.cam_id,
                       name, self._fails[name], detail)
        if self._fails[name] == 3:
            try:
                from backend.services.notifications_service import emit_event

                if camera_settings.load().get("notify_offline", True):
                    emit_event("error", f"Camera offline — {self.cam.get('name')}",
                               detail, source="surveillance",
                               action={"label": "Open Cameras", "appId": "surveillance"},
                               dedupe_key=f"camera:{self.cam_id}:offline",
                               users=(self.cam.get("viewers") or []) or None)
            except Exception:  # noqa: BLE001 — notification is best effort
                logger.debug("Camera offline notification failed", exc_info=True)

    @staticmethod
    def _truncate(path: Path) -> None:
        try:
            if path.exists() and path.stat().st_size > 256 * 1024:
                path.write_text("", encoding="utf-8")
        except OSError:
            pass  # a log we cannot truncate is not worth failing over

    # ── frames + motion ────────────────────────────────────────────────────

    def _read_frames(self, stream) -> None:
        """Consume 160×90 gray frames from the live ffmpeg stdout."""
        buf = bytearray()
        try:
            while not self._stop.is_set() and not shutdown_state.is_set():
                chunk = stream.read(FRAME_BYTES) if stream is not None else b""
                if not chunk:
                    break
                buf.extend(chunk)
                while len(buf) >= FRAME_BYTES:
                    frame = bytes(buf[:FRAME_BYTES])
                    del buf[:FRAME_BYTES]
                    self._on_frame(frame)
        except (OSError, ValueError):
            pass  # the pipe closes when ffmpeg dies; the supervisor restarts it
        finally:
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass  # already closed by the process teardown

    def _on_frame(self, frame: bytes) -> None:
        self._last_frame = time.time()
        if not camera_service.motion_active(self.cam):
            return
        triggered, score = self._detector.evaluate(frame)
        if triggered:
            self._pending.append((time.time(), score))

    def _drain_events(self) -> None:
        while self._pending:
            at, score = self._pending.pop(0)
            try:
                self._store_event(at, score)
            except Exception:  # noqa: BLE001 — one bad event must not stop the camera
                logger.error("Could not store motion event for %s", self.cam_id, exc_info=True)

    def _store_event(self, at: float, score: float) -> None:
        image_rel = None
        try:
            camera_service.EVENTS_IMG_DIR.mkdir(parents=True, exist_ok=True)
            path = camera_service.get_snapshot(self.cam_id, width=640, refresh=True)
            rel = f"{int(at)}_{self.cam_id}.jpg"
            (camera_service.EVENTS_IMG_DIR / rel).write_bytes(path.read_bytes())
            image_rel = rel
        except Exception as exc:  # noqa: BLE001 — event is still useful without a picture
            logger.warning("Motion snapshot failed for %s: %s", self.cam_id, exc)
        cfg = camera_settings.load()
        start = datetime.fromtimestamp(at, tz=timezone.utc)
        end = start + timedelta(seconds=int(cfg["post_s"]))
        camera_service.record_motion_event(
            self.cam, image_rel, score=score,
            recording={"from": start.isoformat(timespec="seconds"),
                       "to": end.isoformat(timespec="seconds")},
            notify=True)


# ── Janitor ─────────────────────────────────────────────────────────────────

class _Janitor(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="camera-janitor", daemon=True)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(JANITOR_INTERVAL_S):
            if shutdown_state.is_set():
                return
            try:
                result = camera_storage.purge()
                if result.get("removed_dirs"):
                    logger.info("Retention sweep: %s", result)
                usage = camera_storage.usage()
                quota = usage.get("quota_bytes") or 0
                if quota and usage.get("recordings_bytes", 0) > quota * 0.9:
                    from backend.services.notifications_service import emit_event

                    emit_event("warning", "Camera storage almost full",
                               f"{usage['recordings_bytes'] / 1024**3:.1f} GiB of "
                               f"{quota / 1024**3:.1f} GiB used",
                               source="surveillance", dedupe_key="camera:storage",
                               action={"label": "Open Cameras", "appId": "surveillance"})
            except Exception:  # noqa: BLE001 — janitor must survive a bad disk
                logger.error("Camera retention sweep failed", exc_info=True)


def _live_source(cam: dict) -> str:
    return camera_service.stream_urls(cam, "sub") or camera_service.stream_urls(cam, "main")


# ── Module API ──────────────────────────────────────────────────────────────

def start_camera(cam: dict) -> None:
    """Start (or restart) the pipeline for one camera."""
    if not cam.get("enabled", True):
        stop_camera(cam["id"])
        return
    stop_camera(cam["id"])
    pipeline = CameraPipeline(cam)
    with _LOCK:
        _PIPELINES[cam["id"]] = pipeline
    pipeline.start()


def stop_camera(cam_id: str) -> None:
    with _LOCK:
        pipeline = _PIPELINES.pop(cam_id, None)
    if pipeline is not None:
        pipeline.stop()


def start_all() -> int:
    """Boot hook: start every enabled camera + the retention janitor."""
    global _JANITOR
    count = 0
    for cam in camera_service.list_cameras():
        if cam.get("enabled", True):
            start_camera(cam)
            count += 1
    with _LOCK:
        if _JANITOR is None or not _JANITOR.is_alive():
            _JANITOR = _Janitor()
            _JANITOR.start()
    return count


def stop_all() -> None:
    global _JANITOR
    with _LOCK:
        pipelines = list(_PIPELINES.values())
        _PIPELINES.clear()
        janitor, _JANITOR = _JANITOR, None
    for pipeline in pipelines:
        pipeline.stop()
    for pipeline in pipelines:
        pipeline.join(timeout=8)
    if janitor is not None:
        janitor.stop()
        janitor.join(timeout=3)
