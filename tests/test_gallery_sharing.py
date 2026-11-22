#!/usr/bin/env python3
"""Photos album sharing — read-only owner-scoped album views.

Verifies that a granted album returns the owner's indexed items, hides
private/encrypted items, refuses the private view, and that album mutations
stay owner-only.

Run: .venv/bin/python tests/test_gallery_sharing.py
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
DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-gallery-share-data-"))
SHARE_ROOT = Path(tempfile.mkdtemp(prefix="ethos-gallery-share-root-"))
SECRET = "gallery-sharing-test-secret"
os.environ["ETHOS_DATA_DIR"] = str(DATA_DIR)
os.environ["ETHOS_JWT_SECRET"] = SECRET
os.environ["ETHOS_PACKAGES_DEFAULT_SOURCE"] = "0"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402
from backend.routers import gallery  # noqa: E402
from backend.services import path_scope, sharing_resources, users_store  # noqa: E402

ALICE = "ethostestalice"
BOB = "ethostestbob"
CAROL = "ethostestcarol"
ALBUM = "alb-share-1"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII="
)

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
        (SHARE_ROOT / name / "photos").mkdir(parents=True, exist_ok=True)
    folder = SHARE_ROOT / ALICE / "photos" / "trip"
    folder.mkdir(parents=True)
    public_photo = folder / "public.png"
    private_photo = folder / "private.png"
    encrypted = folder / "gone.png.ethos.enc"
    public_photo.write_bytes(PNG)
    private_photo.write_bytes(PNG)
    encrypted.write_bytes(b"ciphertext")

    path_scope.SHARE_ROOTS = (str(SHARE_ROOT),)
    sharing_resources.SHAREABLE_ROOTS = (str(SHARE_ROOT),)
    path_scope.home_dir = lambda name: str(SHARE_ROOT / name)
    users_store.save([_record(ALICE), _record(BOB), _record(CAROL)])

    def item(path: Path, private: bool = False) -> dict:
        return {
            "path": str(path), "name": path.name, "folder": str(folder),
            "size": path.stat().st_size, "modified": path.stat().st_mtime,
            "type": "image", "taken": None, "private": private,
            "favorite": False, "camera": "TestCam",
        }

    # Seed Alice's gallery exactly as a completed scan would have left it.
    lib = gallery._library(ALICE)
    lib.state["items"] = [
        item(public_photo),
        item(private_photo, private=True),
        item(encrypted, private=True),
    ]
    lib.albums_store.save([{
        "id": ALBUM, "name": "Trip", "created": time.time(),
        "paths": [str(public_photo), str(private_photo), str(encrypted)],
    }])

    client = TestClient(app, raise_server_exceptions=False)
    alice = {"Authorization": f"Bearer {_token(ALICE)}"}
    bob = {"Authorization": f"Bearer {_token(BOB)}"}
    carol = {"Authorization": f"Bearer {_token(CAROL)}"}

    try:
        response = client.post("/api/sharing/grants", headers=alice, json={
            "kind": "album", "resource": ALBUM, "recipients": [BOB],
        })
        check("owner can share an album", response.status_code == 200, str(response.status_code))
        grant = (response.json() or {}).get("grant") or {}
        check("album grant uses the album name", grant.get("label") == "Trip", str(grant))
        grant_id = grant.get("id")

        response = client.get("/api/gallery/albums", headers=bob, params={"owner": ALICE})
        albums = (response.json() or {}).get("albums") or []
        check("recipient sees the shared album",
              response.status_code == 200 and len(albums) == 1 and albums[0]["id"] == ALBUM,
              str(response.status_code))
        check("shared album is tagged and counted without private items",
              albums and albums[0].get("shared") is True and albums[0].get("count") == 1,
              str(albums))

        response = client.get("/api/gallery/items", headers=bob, params={
            "owner": ALICE, "album": ALBUM,
        })
        items = (response.json() or {}).get("items") or []
        check("recipient sees the album items", response.status_code == 200, str(response.status_code))
        check("private/encrypted items are excluded",
              [it["name"] for it in items] == ["public.png"], str(items))

        response = client.get("/api/gallery/items", headers=bob, params={
            "owner": ALICE, "album": ALBUM, "view": "private", "ps": "nope",
        })
        check("the private view is never shared", response.status_code == 403, str(response.status_code))

        response = client.get("/api/gallery/items", headers=bob, params={"album": ALBUM})
        check("bob's own album namespace does not contain alice's album",
              response.status_code == 404, str(response.status_code))

        response = client.get("/api/gallery/albums", headers=carol, params={"owner": ALICE})
        check("a non-recipient cannot list the albums", response.status_code == 403, str(response.status_code))

        response = client.get("/api/gallery/meta", headers=bob, params={"path": str(public_photo)})
        check("recipient can read metadata for a shared item",
              response.status_code == 200, str(response.status_code))

        response = client.delete(f"/api/gallery/albums/{ALBUM}", headers=bob)
        check("recipient cannot delete the owner's album", response.status_code == 404,
              str(response.status_code))
        check("the album still exists for alice",
              len(gallery._library(ALICE).albums_store.load()) == 1)

        response = client.get("/api/gallery/albums", headers=alice, params={"owner": ALICE})
        check("the owner still sees the album in their own view",
              len((response.json() or {}).get("albums") or []) == 1, str(response.status_code))

        client.delete(f"/api/sharing/grants/{grant_id}", headers=alice)
        response = client.get("/api/gallery/items", headers=bob, params={
            "owner": ALICE, "album": ALBUM,
        })
        check("revoke removes the shared album view", response.status_code == 404,
              str(response.status_code))
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        shutil.rmtree(SHARE_ROOT, ignore_errors=True)

    print()
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"{len(failed)} gallery sharing check(s) failed")
        return 1
    print(f"All {len(results)} gallery sharing checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
