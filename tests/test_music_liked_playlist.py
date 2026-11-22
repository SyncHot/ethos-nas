#!/usr/bin/env python3
"""Liked tracks are the normal playlist "Polubione".

Boots its own isolated instance (throwaway port + ETHOS_DATA_DIR + test JWT
secret) and exercises the like/unlike ↔ playlist mirroring that makes the phone
download the liked tracks: legacy likes materialise as a playlist, the heart
writes both views, editing the playlist updates the liked view and asking for a
playlist called „Polubione” returns the one real object.

    .venv/bin/python tests/test_music_liked_playlist.py
"""

import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "music-liked-test-secret"
_results: list[tuple[str, bool, str]] = []

TRACK_A = {"id": "aaa11111111", "title": "Ulubiony A", "artist": "A", "thumb": "", "dur": 100, "file_url": ""}
TRACK_B = {"id": "bbb22222222", "title": "Ulubiony B", "artist": "B", "thumb": "", "dur": 120, "file_url": ""}
TRACK_C = {"id": "ccc33333333", "title": "Z playlisty", "artist": "C", "thumb": "", "dur": 140, "file_url": ""}


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _token(username: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": username, "username": username, "role": "user",
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base: str, method: str, path: str, token: str | None = None, body=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read(500).decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}
    except OSError as exc:
        return 0, {"detail": str(exc)}


def _wait_ready(base: str, proc: subprocess.Popen, log_path: Path, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            print(log_path.read_text()[-2000:])
            return False
        if _call(base, "GET", "/api/system/status")[0] == 401:
            return True
        time.sleep(0.3)
    return False


def _liked_playlist(state: dict) -> dict | None:
    return next((p for p in state.get("playlists") or []
                 if isinstance(p, dict) and p.get("id") == "liked"), None)


def _keys(tracks) -> set[str]:
    return {f"{t.get('id')}|{t.get('title')}" for t in tracks or [] if isinstance(t, dict)}


def main() -> int:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-music-liked-"))
    log_path = data_dir / "server.log"

    # A pre-upgrade account: likes exist, the playlist does not yet.
    legacy_user = data_dir / "users" / "liked-test"
    legacy_user.mkdir(parents=True, exist_ok=True)
    (legacy_user / "music_library.json").write_text(json.dumps({
        "liked": [TRACK_A],
        "playlists": [],
        "history": [],
        "podcasts": {"subscribed": [], "episodes": {}},
        "radio": {"favorites": [], "recents": []},
    }))

    env = {
        **os.environ,
        "ETHOS_PORT": str(port),
        "ETHOS_DATA_DIR": str(data_dir),
        "ETHOS_JWT_SECRET": SECRET,
        "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
    }
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )

    try:
        print(f"\n[live] isolated instance on {base}")
        if not _wait_ready(base, proc, log_path):
            print("  [FAIL] isolated instance did not become ready")
            return 1

        user = _token("liked-test")

        print("\n[legacy migration]")
        status, state = _call(base, "GET", "/api/music/state", user)
        playlist = _liked_playlist(state)
        check("GET /state is 200", status == 200, str(status))
        check("a 'Polubione' playlist exists", bool(playlist), str(state.get("playlists")))
        check("it has the legacy like", _keys(playlist.get("tracks") if playlist else []) == _keys([TRACK_A]),
              str(playlist))
        check("its id and name are fixed", playlist.get("id") == "liked"
              and playlist.get("name") == "Polubione", str(playlist))
        check("the liked view still answers for old clients", _keys(state.get("liked")) == _keys([TRACK_A]),
              str(state.get("liked")))
        saved = json.loads((legacy_user / "music_library.json").read_text())
        check("the migration was persisted", bool(_liked_playlist(saved)), str(saved.get("playlists")))

        print("\n[heart writes both views]")
        status, _body = _call(base, "POST", "/api/music/liked", user, {"track": TRACK_B})
        check("POST /liked is 200", status == 200, str(status))
        _, state = _call(base, "GET", "/api/music/state", user)
        playlist = _liked_playlist(state)
        check("the playlist gained the track",
              _keys(playlist.get("tracks")) == _keys([TRACK_A, TRACK_B]), str(playlist))
        check("the liked view gained the track",
              _keys(state.get("liked")) == _keys([TRACK_A, TRACK_B]), str(state.get("liked")))

        status, _body = _call(base, "DELETE", "/api/music/liked", user,
                              {"key": f"{TRACK_A['id']}|{TRACK_A['title']}"})
        _, state = _call(base, "GET", "/api/music/state", user)
        playlist = _liked_playlist(state)
        check("unliking removes it from the playlist", status == 200
              and _keys(playlist.get("tracks")) == _keys([TRACK_B]), str(playlist))
        check("unliking removes it from the liked view",
              _keys(state.get("liked")) == _keys([TRACK_B]), str(state.get("liked")))

        print("\n[the playlist is the source of truth]")
        status, _body = _call(base, "PUT", "/api/music/playlists/liked/tracks", user, {"track": TRACK_C})
        _, state = _call(base, "GET", "/api/music/state", user)
        check("adding to 'Polubione' likes the track", status == 200
              and _keys(state.get("liked")) == _keys([TRACK_B, TRACK_C]), str(state.get("liked")))

        status, _body = _call(base, "DELETE", "/api/music/playlists/liked/tracks", user,
                              {"key": f"{TRACK_C['id']}|{TRACK_C['title']}"})
        _, state = _call(base, "GET", "/api/music/state", user)
        check("removing from 'Polubione' unlikes it", status == 200
              and _keys(state.get("liked")) == _keys([TRACK_B]), str(state.get("liked")))

        print("\n[one playlist named Polubione]")
        status, created = _call(base, "POST", "/api/music/playlists", user, {"name": "Polubione"})
        check("asking for it again returns the same object",
              status == 200 and created.get("id") == "liked" and created.get("name") == "Polubione",
              str(created))
        _, state = _call(base, "GET", "/api/music/state", user)
        named = [p for p in state.get("playlists") or []
                 if str(p.get("name", "")).casefold() == "polubione"]
        check("no duplicate playlist was created", len(named) == 1, str(named))

        print("\n[clear likes]")
        status, body = _call(base, "DELETE", "/api/music/playlists/liked", user)
        _, state = _call(base, "GET", "/api/music/state", user)
        check("deleting 'Polubione' clears likes", status == 200 and body.get("deleted") is True
              and state.get("liked") == [], str(state.get("liked")))
        playlist = _liked_playlist(state)
        check("it comes back empty (the heart needs a home)",
              bool(playlist) and playlist.get("tracks") == [], str(playlist))

        print("\n[import path]")
        importer = _token("liked-import")
        status, body = _call(base, "PUT", "/api/music/state", importer, {
            "liked": [TRACK_A],
            "playlists": [],
        })
        playlist = _liked_playlist(body)
        check("PUT /state materialises the playlist too", status == 200
              and bool(playlist) and _keys(playlist.get("tracks")) == _keys([TRACK_A]), str(body.get("playlists")))

        status, _state = _call(base, "GET", "/api/music/state")
        check("the state endpoint still requires a token", status == 401, str(status))

        failed = [r for r in _results if not r[1]]
        print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
        return 1 if failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
