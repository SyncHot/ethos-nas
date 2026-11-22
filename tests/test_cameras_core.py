#!/usr/bin/env python3
"""Unit tests — Cameras core (store migration, credentials, ACL, retention math).

No server needed; everything is pure service logic against a temp DATA_DIR.

Run:
    .venv/bin/python tests/test_cameras_core.py
"""

import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-cameras-test-"))
    os.environ["ETHOS_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("ETHOS_ALLOW_INSECURE", "1")

    from backend.services import (camera_recordings, camera_secrets, camera_service,
                                  camera_settings, camera_storage, camera_runner)

    print("\nPart A — credentials")
    blob = camera_secrets.encrypt_secret("s3cr3t!")
    check("encrypt→decrypt round-trip", camera_secrets.decrypt_secret(blob) == "s3cr3t!")
    check("ciphertext does not contain the password", "s3cr3t" not in blob)
    check("empty password stays empty", camera_secrets.encrypt_secret("") == "")
    clean, user, password = camera_secrets.split_credentials(
        "rtsp://admin:p%40ss@192.168.1.50:554/stream1")
    check("split removes credentials", clean == "rtsp://192.168.1.50:554/stream1", clean)
    check("split decodes the username", user == "admin", user)
    check("split decodes the password", password == "p@ss", password)
    merged = camera_secrets.merge_credentials(clean, user, password)
    check("merge re-encodes credentials", merged == "rtsp://admin:p%40ss@192.168.1.50:554/stream1",
          merged)
    check("mask_url hides credentials", "***" in camera_secrets.mask_url(merged)
          and "p%40ss" not in camera_secrets.mask_url(merged))

    print("\nPart B — store migration v1→v2")
    legacy = {
        "version": 1,
        "cameras": [{
            "id": "cam_0123456789ab",
            "name": "Front",
            "url": "rtsp://admin:pw@10.0.0.5:554/main",
            "enabled": True,
            "motion": {"enabled": True, "interval_s": 15, "sensitivity": 0.4},
            "created_at": "2026-01-01T00:00:00+00:00",
        }],
    }
    upgraded = camera_service._migrate_cameras(1, 2, legacy)
    cam = upgraded["cameras"][0]
    check("migration marks version 2", upgraded["version"] == 2)
    check("migration splits the URL", cam["streams"]["main"] == "rtsp://10.0.0.5:554/main",
          str(cam.get("streams")))
    check("migration removes the plaintext url field", "url" not in cam)
    check("migration encrypts the password",
          camera_secrets.decrypt_secret(cam["auth"]["password_enc"]) == "pw")
    check("migration keeps the username", cam["auth"]["user"] == "admin")
    check("legacy interval becomes the cooldown", cam["motion"]["cooldown_s"] == 15)
    check("default record mode is off", cam["record"]["mode"] == "off")
    check("viewers default to empty", cam["viewers"] == [])

    print("\nPart C — public payload + ACL")
    public = camera_service.public_camera(cam)
    check("public payload keeps the clean URL", public["url"] == "rtsp://10.0.0.5:554/main",
          public["url"])
    dirty = {"streams": {"main": "rtsp://admin:pw@10.0.0.5:554/main", "sub": None},
             "auth": {"user": "admin", "password_enc": "x"}}
    check("public payload masks an embedded password",
          "***" in camera_service.public_camera(dirty)["url"])
    check("public payload never exposes the password blob",
          "password_enc" not in public.get("auth", {}))
    check("public payload reports has_password", public["auth"]["has_password"] is True)
    admin = {"username": "root", "role": "admin"}
    viewer = {"username": "anna", "role": "user"}
    stranger = {"username": "bob", "role": "user"}
    check("admin sees any camera", camera_service.can_view(cam, admin))
    check("viewer list grants access", not camera_service.can_view(cam, viewer))
    cam["viewers"] = ["anna"]
    check("named viewer sees the camera", camera_service.can_view(cam, viewer))
    check("other users do not", not camera_service.can_view(cam, stranger))

    print("\nPart D — schedules")
    monday_10 = datetime(2026, 3, 2, 10, 0)          # Monday
    check("full-day window matches", camera_service.schedule_active(
        {"days": [0], "from": "00:00", "to": "23:59"}, monday_10))
    check("wrong weekday does not match", not camera_service.schedule_active(
        {"days": [1], "from": "00:00", "to": "23:59"}, monday_10))
    check("inside a daytime window matches", camera_service.schedule_active(
        {"days": [0], "from": "08:00", "to": "18:00"}, monday_10))
    check("outside a daytime window does not match", not camera_service.schedule_active(
        {"days": [0], "from": "11:00", "to": "18:00"}, monday_10))
    check("overnight window matches after midnight", camera_service.schedule_active(
        {"days": [0], "from": "22:00", "to": "06:00"}, datetime(2026, 3, 3, 2, 0)))
    check("overnight window matches before midnight", camera_service.schedule_active(
        {"days": [0], "from": "22:00", "to": "06:00"}, datetime(2026, 3, 2, 23, 0)))
    check("record mode off is never active",
          not camera_service.recording_active({"record": {"mode": "off"}}))
    check("continuous is always active",
          camera_service.recording_active({"record": {"mode": "continuous"}}))

    print("\nPart E — motion detector")
    detector = camera_runner.MotionDetector({
        "motion": {"enabled": True, "sensitivity": 0.3, "min_cells": 2,
                   "cooldown_s": 10, "zones": None}})
    dark = bytes(camera_runner.FRAME_BYTES)
    bright = bytes([255]) * camera_runner.FRAME_BYTES
    triggered, score = detector.evaluate(dark)
    check("first frame never triggers", not triggered)
    triggered, score = detector.evaluate(bright)
    check("full-frame change triggers", triggered, f"score={score}")
    check("score is the changed fraction", score > 0.9, f"score={score}")
    triggered, _ = detector.evaluate(dark)
    check("cooldown suppresses immediate repeats", not triggered)

    masked = camera_runner.MotionDetector({
        "motion": {"enabled": True, "sensitivity": 0.3, "min_cells": 1, "cooldown_s": 10,
                   "zones": {"cols": 2, "rows": 2, "mask": [1, 1, 0, 0]}}})
    masked.evaluate(dark)
    bottom_bright = bytearray(dark)
    for y in range(camera_runner.FRAME_H // 2, camera_runner.FRAME_H):
        for x in range(camera_runner.FRAME_W):
            bottom_bright[y * camera_runner.FRAME_W + x] = 255
    triggered, score = masked.evaluate(bytes(bottom_bright))
    check("masked-out cells never trigger", not triggered, f"score={score}")

    print("\nPart F — recordings index + playback")
    cam_id = "cam_0123456789ab"
    day = "2026-03-02"
    for run, hour in (("10-00-00", 10), ("10-01-00", 10)):
        run_dir = camera_storage.run_dir(cam_id, day, run)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "init.mp4").write_bytes(b"\x00init")
        (run_dir / "seg00000.m4s").write_bytes(b"\x00" * 64)
        start = datetime(2026, 3, 2, hour, 0 if run == "10-00-00" else 1, tzinfo=timezone.utc)
        (run_dir / "index.m3u8").write_text(
            "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:60\n"
            f"#EXT-X-PROGRAM-DATE-TIME:{start.isoformat()}\n"
            "#EXTINF:60.000,\nseg00000.m4s\n#EXT-X-ENDLIST\n", encoding="utf-8")
    entries = camera_recordings._all_entries(cam_id, day)
    check("playlist parser reads start times", len(entries) == 2 and
          entries[0]["start"] == datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc),
          str(entries))
    # Age the first run so the retention sweep may delete it (the newest run is
    # always protected, and files written a second ago are inside the grace window).
    old = (datetime.now() - timedelta(hours=2)).timestamp()
    first_run = camera_storage.run_dir(cam_id, day, "10-00-00")
    for item in first_run.iterdir():
        os.utime(item, (old, old))
    merged = camera_recordings.ranges(cam_id, day)
    check("two adjacent runs merge into one range", len(merged) == 1 and
          merged[0]["duration_s"] == 120, str(merged))
    body = camera_recordings.playlist(cam_id, datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc),
                                      datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc))
    check("generated playlist lists an init map", "#EXT-X-MAP:URI=" in body)
    check("generated playlist lists both runs", body.count("#EXT-X-DISCONTINUITY") == 1)
    check("generated playlist points at segments", "/playback/cam_0123456789ab/seg?p=" in body)
    seg = camera_recordings.segment_path(cam_id, f"{day}/10-00-00/seg00000.m4s")
    check("segment resolver returns the file", seg.is_file())
    for bad in (f"{day}/10-00-00/../../etc/passwd", f"{day}/10-00-00/../x/seg00000.m4s",
                "../10-00-00/seg00000.m4s"):
        try:
            camera_recordings.segment_path(cam_id, bad)
            check(f"segment resolver rejects {bad}", False)
        except (camera_service.CameraError, ValueError):
            check(f"segment resolver rejects {bad}", True)

    print("\nPart G — retention math")
    check("live directory lives under the storage root",
          camera_storage.camera_live_dir(cam_id).name == cam_id)
    usage = camera_storage.usage()
    check("usage reports the recordings tree", usage["recordings_bytes"] >= 128,
          str(usage.get("recordings_bytes")))
    result = camera_storage.purge(retention_days=0, quota_pct=80)
    check("retention=0 purges old runs", result["removed_dirs"] >= 1, str(result))

    print("\nPart H — single-connection archive + space budget")
    live_dir = camera_storage.camera_live_dir(cam_id)
    archive_dir = camera_storage.run_dir(cam_id, day, "12-00-00")
    archive_dir.mkdir(parents=True, exist_ok=True)
    cmd = camera_runner._live_cmd("rtsp://host/live0", "h264", live_dir,
                                  archive_dir=archive_dir, segment_s=30)
    check("combined command has two HLS outputs", cmd.count("hls") == 2,
          f"{cmd.count('hls')} hls muxers")
    check("combined command writes the archive playlist",
          str(archive_dir / "index.m3u8") in cmd)
    check("combined command still pipes motion frames", "rawvideo" in cmd and "pipe:1" in cmd)
    check("single output command has no archive",
          str(archive_dir / "index.m3u8") not in camera_runner._live_cmd(
              "rtsp://host/live0", "h264", live_dir))

    camera_settings.save({"quota_pct": 80})
    real_disk_usage = camera_storage.shutil.disk_usage
    try:
        class _Disk:
            total = 100 * 1024 ** 3
            free = 50 * 1024 ** 3

        camera_storage.shutil.disk_usage = lambda _path: _Disk()
        usage = camera_storage.usage()
        expected = usage["recordings_bytes"] + 30 * 1024 ** 3
        check("budget reserves 20% of the volume", usage["quota_bytes"] == expected,
              f"{usage['quota_bytes']} != {expected}")

        class _FullDisk:
            total = 100 * 1024 ** 3
            free = 5 * 1024 ** 3

        camera_storage.shutil.disk_usage = lambda _path: _FullDisk()
        usage = camera_storage.usage()
        check("full disk gives no growth budget",
              usage["quota_bytes"] == usage["recordings_bytes"],
              f"{usage['quota_bytes']} != {usage['recordings_bytes']}")
    finally:
        camera_storage.shutil.disk_usage = real_disk_usage

    storage_alert = camera_runner.storage_alert
    check("no recordings → no 'storage almost full' alert",
          storage_alert({"recordings_bytes": 0, "quota_bytes": 0}) is None)
    check("collapsed quota with nothing recorded stays silent",
          storage_alert({"recordings_bytes": 0, "quota_bytes": 0, "used_pct": 97}) is None)
    check("headroom left → no alert",
          storage_alert({"recordings_bytes": 1024 ** 3, "quota_bytes": 10 * 1024 ** 3}) is None)
    check("tiny leftovers never read as '0.0 GiB of 0.0 GiB'",
          storage_alert({"recordings_bytes": 10 * 1024 ** 2, "quota_bytes": 10 * 1024 ** 2,
                         "used_pct": 97}) is None)
    _msg = storage_alert({"recordings_bytes": 2 * 1024 ** 3, "quota_bytes": 2 * 1024 ** 3,
                          "used_pct": 91})
    check("recordings above the floor at the quota → alert",
          bool(_msg) and "2.0 GiB recorded" in _msg and "91%" in _msg
          and "no room left" in _msg, str(_msg))

    # A camera that cannot be reached used to spawn ffmpeg every supervisor tick:
    # the dead process was replaced before _handle_exit() could charge it, so no
    # backoff applied and every 2 s left a new run directory behind.
    import types

    class _DeadProc:
        process = types.SimpleNamespace(poll=lambda: 1, returncode=1)
        detail: dict = {}
        last_line = "connection refused"
        log_path = Path("/nonexistent/ffmpeg.log")

    _cam = {"id": "cam_dead", "name": "Dead", "enabled": True,
            "streams": {"main": "rtsp://nope/main", "sub": "rtsp://nope/sub"}}
    pipeline = camera_runner.CameraPipeline(_cam)
    pipeline._codec = "h264"
    pipeline._archive = _DeadProc()
    _real_active = camera_service.recording_active
    camera_service.recording_active = lambda _cam: True
    try:
        pipeline._ensure_archive()
    finally:
        camera_service.recording_active = _real_active
    check("a dead archive process is charged as one failure",
          pipeline._fails.get("archive") == 1 and pipeline._archive is None,
          f"fails={pipeline._fails} archive={pipeline._archive}")
    check("the failure sets a restart backoff instead of an immediate respawn",
          pipeline._next_try["archive"] > time.time(), str(pipeline._next_try))

    pipeline._live = _DeadProc()
    pipeline._ensure_live()
    check("a dead live process is charged and not respawned at once",
          pipeline._fails.get("live") == 1 and pipeline._live is None
          and pipeline._next_try["live"] > time.time(),
          f"fails={pipeline._fails} live={pipeline._live}")

    print("\nPart I — per-camera retention override")
    other_id = "cam_bbbbbbbbbbbb"
    real_load = camera_service._load_cameras
    try:
        aged = (datetime.now() - timedelta(days=3)).timestamp()
        newer = (datetime.now() - timedelta(days=1)).timestamp()
        per_camera = {(cam_id, "06-00-00"): aged, (other_id, "07-00-00"): aged,
                      (other_id, "09-00-00"): newer}
        for (cid, run), mtime in per_camera.items():
            run_dir = camera_storage.run_dir(cid, day, run)
            run_dir.mkdir(parents=True, exist_ok=True)
            marker = run_dir / "seg00000.m4s"
            marker.write_bytes(b"x" * 32)
            os.utime(marker, (mtime, mtime))
        camera_service._load_cameras = lambda: {"version": 2, "cameras": [
            {"id": cam_id, "record": {"retention_days": 0}},
            {"id": other_id, "record": {"retention_days": None}},
        ]}
        camera_storage.purge(retention_days=30, quota_pct=80)
        check("per-camera retention=0 purges that camera only",
              not camera_storage.run_dir(cam_id, day, "06-00-00").exists())
        check("global retention keeps the other camera's old run",
              camera_storage.run_dir(other_id, day, "07-00-00").exists())
        check("newest run of every camera is always kept",
              camera_storage.run_dir(other_id, day, "09-00-00").exists())
    finally:
        camera_service._load_cameras = real_load
        shutil.rmtree(camera_storage.camera_recordings_dir(other_id), ignore_errors=True)

    failures = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failures)}/{len(_results)} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
