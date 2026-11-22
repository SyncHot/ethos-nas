#!/usr/bin/env python3
"""Music playlist sharing — local tracks are rewritten to the read-scoped file API.

Run: .venv/bin/python tests/test_music_sharing.py
"""

import base64
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-music-share-data-"))
SHARE_ROOT = Path(tempfile.mkdtemp(prefix="ethos-music-share-root-"))
SECRET = "music-sharing-test-secret"
os.environ["ETHOS_DATA_DIR"] = str(DATA_DIR)
os.environ["ETHOS_JWT_SECRET"] = SECRET
os.environ["ETHOS_PACKAGES_DEFAULT_SOURCE"] = "0"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402
from backend.services import path_scope, sharing_resources, user_data, users_store  # noqa: E402

ALICE = "ethostestalice"
BOB = "ethostestbob"
CAROL = "ethostestcarol"
PLAYLIST = "pl-share-1"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _token(username: str) -> str:
    def encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    header = encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = encode(json.dumps({
        "sub": username, "username": username, "role": "user",
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    signature = encode(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(),
                                hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def _record(username: str) -> dict:
    return {
        "id": username, "username": username, "role": "user", "status": "active",
        "created_at": "2026-01-01T00:00:00Z", "last_login": None,
        "source": "test", "api_key": "",
    }


def main() -> int:
    for name in (ALICE, BOB, CAROL):
        (SHARE_ROOT / name).mkdir(parents=True, exist_ok=True)
    music_dir = SHARE_ROOT / ALICE / "Music"
    music_dir.mkdir()
    song = music_dir / "song.m4a"
    song.write_bytes(b"fake-audio-bytes")

    path_scope.SHARE_ROOTS = (str(SHARE_ROOT),)
    sharing_resources.SHAREABLE_ROOTS = (str(SHARE_ROOT),)
    path_scope.home_dir = lambda name: str(SHARE_ROOT / name)
    users_store.save([_record(ALICE), _record(BOB), _record(CAROL)])

    user_data.user_store("music_config.json", dict, username=ALICE).save({
        "download_dir": str(music_dir),
    })
    user_data.user_store("music_library.json", dict, username=ALICE).save({
        "liked": [],
        "playlists": [{
            "id": PLAYLIST,
            "name": "Road trip",
            "tracks": [
                {"id": "local", "title": "Local song", "artist": "A",
                 "file_url": "/api/music/library-file?name=song.m4a", "dur": 10, "date": 0},
                {"id": "remote", "title": "Remote song", "artist": "B",
                 "file_url": "/api/music/stream?video_id=abc", "dur": 20, "date": 0},
            ],
        }],
        "history": [],
        "podcasts": {"subscribed": [], "episodes": {}},
        "radio": {"favorites": [], "recents": []},
    })

    client = TestClient(app, raise_server_exceptions=False)
    alice = {"Authorization": f"Bearer {_token(ALICE)}"}
    bob = {"Authorization": f"Bearer {_token(BOB)}"}
    carol = {"Authorization": f"Bearer {_token(CAROL)}"}

    try:
        response = client.post("/api/sharing/grants", headers=alice, json={
            "kind": "playlist", "resource": PLAYLIST, "recipients": [BOB],
        })
        check("owner can share a playlist", response.status_code == 200, str(response.status_code))
        grant = (response.json() or {}).get("grant") or {}
        check("playlist grant uses the playlist name", grant.get("label") == "Road trip", str(grant))
        grant_id = grant.get("id")

        response = client.get("/api/music/playlists/shared", headers=bob, params={
            "owner": ALICE, "id": PLAYLIST,
        })
        data = response.json() if response.status_code == 200 else {}
        check("recipient can read the shared playlist", response.status_code == 200,
              str(response.status_code))
        tracks = data.get("tracks") or []
        check("both tracks are present", len(tracks) == 2, str(tracks))
        local = next((t for t in tracks if t.get("id") == "local"), {})
        remote = next((t for t in tracks if t.get("id") == "remote"), {})
        check("local track points at the read-scoped file API",
              local.get("file_url") == "/api/files/raw?path=" + quote(str(song)),
              str(local.get("file_url")))
        check("remote track keeps its engine URL",
              remote.get("file_url") == "/api/music/stream?video_id=abc", str(remote.get("file_url")))

        response = client.get(local["file_url"], headers=bob)
        check("recipient can stream the shared local track",
              response.status_code == 200 and response.content == b"fake-audio-bytes",
              str(response.status_code))

        response = client.delete(f"/api/music/playlists/{PLAYLIST}", headers=bob)
        check("recipient cannot delete the owner's playlist",
              (response.json() or {}).get("deleted") is False, str(response.json()))
        owner_state = user_data.user_store("music_library.json", dict, username=ALICE).load()
        check("the owner's playlist is untouched",
              len(owner_state.get("playlists") or []) == 1)

        response = client.get("/api/music/playlists/shared", headers=carol, params={
            "owner": ALICE, "id": PLAYLIST,
        })
        check("a non-recipient cannot read the playlist", response.status_code == 404,
              str(response.status_code))

        client.delete(f"/api/sharing/grants/{grant_id}", headers=alice)
        response = client.get("/api/music/playlists/shared", headers=bob, params={
            "owner": ALICE, "id": PLAYLIST,
        })
        check("revoke closes the shared playlist", response.status_code == 404,
              str(response.status_code))
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        shutil.rmtree(SHARE_ROOT, ignore_errors=True)

    print()
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"{len(failed)} music sharing check(s) failed")
        return 1
    print(f"All {len(results)} music sharing checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
