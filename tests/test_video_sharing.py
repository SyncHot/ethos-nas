#!/usr/bin/env python3
"""Video Station sharing — shared library roots are browsable and streamable.

Run: .venv/bin/python tests/test_video_sharing.py
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

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-video-share-data-"))
SHARE_ROOT = Path(tempfile.mkdtemp(prefix="ethos-video-share-root-"))
SECRET = "video-sharing-test-secret"
os.environ["ETHOS_DATA_DIR"] = str(DATA_DIR)
os.environ["ETHOS_JWT_SECRET"] = SECRET
os.environ["ETHOS_PACKAGES_DEFAULT_SOURCE"] = "0"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402
from backend.services import path_scope, sharing_resources, users_store  # noqa: E402

ALICE = "ethostestalice"
BOB = "ethostestbob"
CAROL = "ethostestcarol"

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
    videos = SHARE_ROOT / ALICE / "Videos"
    videos.mkdir()
    movie = videos / "movie.mp4"
    movie.write_bytes(b"fake-mp4-bytes")

    path_scope.SHARE_ROOTS = (str(SHARE_ROOT),)
    sharing_resources.SHAREABLE_ROOTS = (str(SHARE_ROOT),)
    path_scope.home_dir = lambda name: str(SHARE_ROOT / name)
    users_store.save([_record(ALICE), _record(BOB), _record(CAROL)])

    client = TestClient(app, raise_server_exceptions=False)
    alice = {"Authorization": f"Bearer {_token(ALICE)}"}
    bob = {"Authorization": f"Bearer {_token(BOB)}"}
    carol = {"Authorization": f"Bearer {_token(CAROL)}"}

    try:
        response = client.post("/api/sharing/grants", headers=alice, json={
            "kind": "path", "resource": str(videos), "recipients": [BOB],
        })
        check("owner can share a video folder", response.status_code == 200, str(response.status_code))
        grant_id = (response.json() or {}).get("grant", {}).get("id")

        response = client.get("/api/videos/library", headers=bob, params={"path": str(videos)})
        data = response.json() if response.status_code == 200 else {}
        check("recipient can browse a shared library root",
              response.status_code == 200, str(response.status_code))
        check("the shared root lists the video",
              [v.get("name") for v in data.get("videos", [])] == ["movie.mp4"], str(data.get("videos")))

        response = client.get("/api/videos/browse", headers=bob, params={"path": str(videos)})
        check("recipient can use the folder picker on a shared root",
              response.status_code == 200, str(response.status_code))

        response = client.get("/api/videos/stream", headers=bob, params={"path": str(movie)})
        check("recipient can stream the shared video",
              response.status_code == 200 and response.content == b"fake-mp4-bytes",
              str(response.status_code))

        response = client.post("/api/videos/config", headers=bob, json={"roots": [str(videos)]})
        saved_roots = (response.json() or {}).get("roots") if response.status_code == 200 else None
        check("a shared path cannot be persisted as the recipient's own root",
              saved_roots is not None and str(videos) not in saved_roots, str(saved_roots))

        response = client.get("/api/videos/library", headers=carol, params={"path": str(videos)})
        check("a non-recipient cannot browse the shared root",
              response.status_code == 403, str(response.status_code))

        client.delete(f"/api/sharing/grants/{grant_id}", headers=alice)
        response = client.get("/api/videos/library", headers=bob, params={"path": str(videos)})
        check("revoke closes the shared library root", response.status_code == 403,
              str(response.status_code))
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        shutil.rmtree(SHARE_ROOT, ignore_errors=True)

    print()
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"{len(failed)} video sharing check(s) failed")
        return 1
    print(f"All {len(results)} video sharing checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
