#!/usr/bin/env python3
"""Isolated-server checks for the new Videos endpoints (HLS, resume delete, My List)."""

import base64
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "video-api-test-secret"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def token(role: str, username: str) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({
        "sub": "1", "username": username, "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def make_sample(path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=duration=6:size=320x180:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "64k",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(proc.stderr[-600:])
    return proc.returncode == 0 and path.is_file()


def call(base: str, method: str, path: str, tok: str | None = None,
         body: dict | None = None, timeout: float = 45.0) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    try:
        req = urllib.request.Request(base + path, data=data, method=method)
    except ValueError as exc:
        return 0, str(exc).encode()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (OSError, ValueError) as exc:
        return 0, str(exc).encode()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ethos-video-api-"))
    data_dir = tmp / "data"
    share = tmp / "share"
    share.mkdir(parents=True)
    sample = share / "sample.mkv"
    has_media = make_sample(sample)
    port = free_port()
    env = os.environ.copy()
    env.update({
        "ETHOS_DATA_DIR": str(data_dir),
        "ETHOS_JWT_SECRET": SECRET,
        "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
        "ETHOS_PORT": str(port),
        "ETHOS_LOG_LEVEL": "warning",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    admin = token("admin", "video-admin")
    user = token("user", "video-user")
    sid = ""
    try:
        for _ in range(80):
            code, _ = call(base, "GET", "/api/system/status", timeout=2)
            if code in (200, 401):
                break
            time.sleep(0.25)
        else:
            check("isolated server starts", False, "no response")
            return 1

        if has_media:
            code, body = call(base, "POST", "/api/videos/config", admin,
                              {"roots": [str(share)], "tmdb": {"auto_fetch": False}})
            check("config saves a root", code == 200, f"{code} {body[:200]}")

            code, body = call(base, "GET", "/api/videos/bootstrap", admin)
            boot = json.loads(body) if code == 200 else {}
            check("bootstrap returns config/status/home",
                  code == 200 and "home" in boot and "status" in boot, f"{code} {body[:200]}")

            code, body = call(base, "GET", "/api/videos/metadata/status", admin)
            meta_status = json.loads(body) if code == 200 else {}
            check("metadata status is keyless by default",
                  code == 200 and meta_status.get("provider") == "auto"
                  and meta_status.get("source_mode") == "keyless", f"{code} {body[:300]}")
            code, _ = call(base, "GET", "/api/videos/metadata/status", None)
            check("metadata status requires auth", code == 401, str(code))

            code, body = call(base, "POST", "/api/videos/config", admin,
                              {"roots": [str(share)], "tmdb": {"provider": "off"}})
            check("metadata provider off saved",
                  code == 200 and json.loads(body).get("tmdb", {}).get("provider") == "off",
                  f"{code} {body[:200]}")
            code, _ = call(base, "POST", "/api/videos/metadata/refresh", admin, {})
            check("refresh refuses when metadata is off", code == 400, str(code))

            code, body = call(base, "POST", "/api/videos/config", admin,
                              {"roots": [str(share)], "tmdb": {"provider": "tmdb"}})
            check("metadata provider tmdb saved", code == 200, f"{code} {body[:200]}")
            code, _ = call(base, "POST", "/api/videos/metadata/refresh", admin, {})
            check("tmdb provider without key is rejected", code == 400, str(code))
            code, _ = call(base, "GET", "/api/videos/tmdb/test", admin)
            check("legacy tmdb test still requires a key", code == 400, str(code))

            image_dir = data_dir / "metadata_images"
            image_dir.mkdir(parents=True, exist_ok=True)
            (image_dir / "api-test.jpg").write_bytes(b"\xff\xd8\xff\xe0test")
            code, body = call(base, "GET", "/api/videos/metadata/image?file=api-test.jpg", admin)
            check("metadata image is served", code == 200 and body.startswith(b"\xff\xd8"), str(code))
            code, body = call(base, "GET", "/api/videos/tmdb/image?file=api-test.jpg", admin)
            check("legacy image alias works", code == 200, str(code))
            code, _ = call(base, "GET", "/api/videos/metadata/image?file=../api-test.jpg", admin)
            check("metadata image rejects traversal", code == 400, str(code))

            code, body = call(base, "POST", "/api/videos/config", admin,
                              {"roots": [str(share)], "tmdb": {"provider": "keyless"}})
            check("metadata provider keyless restored", code == 200, f"{code} {body[:200]}")

            code, body = call(base, "GET", "/api/videos/info?path=" + urllib.parse.quote(str(sample)), admin)
            info = json.loads(body).get("info", {}) if code == 200 else {}
            check("info returns a playback mode", code == 200 and info.get("playback_mode") in ("direct", "remux", "transcode"),
                  f"{code} {body[:300]}")

            code, body = call(base, "POST", "/api/videos/hls", admin,
                              {"path": str(sample), "t": 0, "a": 0, "q": "balanced"}, timeout=60)
            hls = json.loads(body) if code == 200 else {}
            check("HLS session starts", code == 200 and hls.get("session"), f"{code} {body[:300]}")
            sid = hls.get("session", "")
            if sid:
                playlist_path = hls.get("playlist", "") + "?token=" + urllib.parse.quote(admin, safe="")
                code, body = call(base, "GET", playlist_path)
                playlist = body.decode("utf-8", "replace") if code == 200 else ""
                check("HLS playlist is served", code == 200 and playlist.startswith("#EXTM3U"), f"{code} {body[:200]}")
                check("HLS playlist tokenizes segments", "?token=" in playlist, playlist[:300])
                seg = next((line for line in playlist.splitlines() if line and not line.startswith("#")), "")
                seg_name = seg.split("?token=")[0]
                if seg_name:
                    code, body = call(base, "GET", f"/api/videos/hls/{sid}/{seg_name}?token=" + urllib.parse.quote(admin, safe=""))
                    check("HLS segment is served", code == 200 and len(body) > 0, f"{code} {len(body)} bytes")
                code, _ = call(base, "GET", playlist_path, user)
                check("another user cannot read the playlist", code == 404, str(code))
                code, _ = call(base, "POST", f"/api/videos/hls/{sid}/stop", admin)
                check("HLS session stops", code == 200, str(code))
                sid = ""
            else:
                check("HLS playlist is served", False, "no session")
                check("HLS playlist tokenizes segments", False, "no session")
                check("another user cannot read the playlist", False, "no session")
                check("HLS session stops", False, "no session")

            code, _ = call(base, "POST", "/api/videos/hls", user,
                           {"path": "/etc/passwd", "t": 0, "a": 0, "q": "balanced"})
            check("non-admin HLS outside the scope is blocked", code == 403, str(code))

            code, _ = call(base, "POST", "/api/videos/resume", admin,
                           {"path": str(sample), "time": 60, "duration": 600})
            check("resume saves", code == 200, str(code))
            code, _ = call(base, "DELETE", "/api/videos/resume?path=" + urllib.parse.quote(str(sample)), admin)
            check("resume delete works", code == 200, str(code))
            code, body = call(base, "GET", "/api/videos/resume?path=" + urllib.parse.quote(str(sample)), admin)
            check("resume is cleared", code == 200 and json.loads(body).get("resume") is None, body[:200])

            code, body = call(base, "POST", "/api/videos/list", admin, {"path": str(sample), "add": True})
            paths = json.loads(body).get("paths", []) if code == 200 else []
            check("My List add works", code == 200 and str(sample) in paths, f"{code} {body[:200]}")
            code, body = call(base, "GET", "/api/videos/list", user)
            check("My List is per-user", code == 200 and json.loads(body).get("paths") == [], body[:200])
            code, body = call(base, "DELETE", "/api/videos/list?path=" + urllib.parse.quote(str(sample)), admin)
            check("My List delete works", code == 200 and str(sample) not in json.loads(body).get("paths", []),
                  f"{code} {body[:200]}")
        else:
            print("[SKIP] ffmpeg not installed — HLS media checks skipped")

        code, _ = call(base, "GET", "/api/videos/list", None)
        check("videos endpoints require auth", code == 401, str(code))
    finally:
        if sid:
            call(base, "POST", f"/api/videos/hls/{sid}/stop", admin, timeout=5)
        proc.terminate()
        try:
            proc.communicate(timeout=15)
        except Exception:
            proc.kill()
            proc.communicate()
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
