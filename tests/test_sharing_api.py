#!/usr/bin/env python3
"""API tests — user-to-user sharing (view-only path grants).

Boots the app with a throwaway DATA_DIR and a temporary “NAS root” so no real
account or file is touched. Verifies the directory, grant CRUD, read-only
enforcement, revoke, “all users”, DATA_DIR rejection and owner lifecycle.

Run: .venv/bin/python tests/test_sharing_api.py
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
DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-sharing-data-"))
SHARE_ROOT = Path(tempfile.mkdtemp(prefix="ethos-sharing-root-"))
SECRET = "sharing-api-test-secret"
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
DAVE = "ethostestdave"
DISABLED = "ethostestdisabled"
DELETED = "ethostestdeleted"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _token(role: str, username: str) -> str:
    def encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    header = encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = encode(json.dumps({
        "sub": username, "username": username, "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    signature = encode(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(),
                                hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def _record(username: str, role: str = "user", status: str = "active") -> dict:
    return {
        "id": username, "username": username, "role": role, "status": status,
        "created_at": "2026-01-01T00:00:00Z", "last_login": None,
        "source": "test", "api_key": "",
    }


def _home(username: str) -> str:
    return str(SHARE_ROOT / username)


def main() -> int:
    # Isolated “NAS”: each account owns a home under the temporary root.
    for name in (ALICE, BOB, CAROL):
        (SHARE_ROOT / name).mkdir(parents=True, exist_ok=True)
    (SHARE_ROOT / ALICE / "photos" / "trip").mkdir(parents=True)
    (SHARE_ROOT / ALICE / "photos" / "trip" / "one.txt").write_text("trip-one")
    (SHARE_ROOT / ALICE / "photos" / "trip" / "two.txt").write_text("trip-two")
    (SHARE_ROOT / ALICE / "photos" / "secret").mkdir()
    (SHARE_ROOT / ALICE / "photos" / "secret" / "hidden.txt").write_text("hidden")

    # Path scope is patched so the temporary root behaves like /home for these
    # synthetic accounts. Production keeps /home + /mnt + /media.
    path_scope.SHARE_ROOTS = (str(SHARE_ROOT),)
    sharing_resources.SHAREABLE_ROOTS = (str(SHARE_ROOT),)
    path_scope.home_dir = _home

    users_store.save([
        _record(ALICE), _record(BOB), _record(CAROL),
        _record(DISABLED, status="disabled"),
        _record(DELETED, status="deleted"),
    ])

    client = TestClient(app, raise_server_exceptions=False)
    alice = {"Authorization": f"Bearer {_token('user', ALICE)}"}
    bob = {"Authorization": f"Bearer {_token('user', BOB)}"}
    carol = {"Authorization": f"Bearer {_token('user', CAROL)}"}

    shared_dir = SHARE_ROOT / ALICE / "photos" / "trip"
    shared_file = shared_dir / "one.txt"
    secret_file = SHARE_ROOT / ALICE / "photos" / "secret" / "hidden.txt"

    try:
        # ── directory ────────────────────────────────────────────────────
        response = client.get("/api/sharing/users", headers=bob)
        names = [u["username"] for u in response.json().get("users", [])] if response.status_code == 200 else []
        check("sharing directory is available to a normal user", response.status_code == 200, str(response.status_code))
        check("directory lists active accounts only",
              ALICE in names and BOB in names and CAROL in names
              and DISABLED not in names and DELETED not in names, str(names))
        record = next((u for u in response.json().get("users", []) if u["username"] == ALICE), {})
        check("directory exposes only username + role",
              set(record) == {"username", "role"} and "home" not in record and "api_key" not in record,
              str(record))

        # ── grant creation ───────────────────────────────────────────────
        response = client.post("/api/sharing/grants", headers=alice, json={
            "kind": "path", "resource": str(shared_dir), "recipients": [BOB],
        })
        grant = (response.json() or {}).get("grant") or {}
        check("owner can share a folder", response.status_code == 200, str(response.status_code))
        check("grant carries a label and recipients",
              grant.get("label") == "trip" and grant.get("recipients") == [BOB], str(grant))
        grant_id = grant.get("id")

        response = client.get("/api/sharing/incoming", headers=bob)
        incoming = (response.json() or {}).get("grants") or []
        check("recipient sees the incoming grant",
              response.status_code == 200 and len(incoming) == 1
              and incoming[0]["owner"] == ALICE, str(incoming))

        response = client.get("/api/sharing/outgoing", headers=alice)
        outgoing = (response.json() or {}).get("grants") or []
        check("owner sees the grant in outgoing", len(outgoing) == 1, str(outgoing))

        response = client.get("/api/sharing/resource", headers=alice, params={
            "kind": "path", "resource": str(shared_dir),
        })
        check("resource prefill returns the grant",
              (response.json() or {}).get("grant", {}).get("id") == grant_id)
        response = client.get("/api/sharing/resource", headers=bob, params={
            "kind": "path", "resource": str(shared_dir),
        })
        check("resource prefill is owner-only",
              (response.json() or {}).get("grant") is None)

        # ── recipient read access ────────────────────────────────────────
        response = client.get("/api/files", headers=bob, params={"path": str(shared_dir)})
        listing = response.json() if response.status_code == 200 else {}
        check("recipient can list a shared folder", response.status_code == 200, str(response.status_code))
        check("shared listing is marked read-only", listing.get("can_write") is False, str(listing.get("can_write")))
        check("shared listing contains the files",
              {f["name"] for f in listing.get("files", [])} == {"one.txt", "two.txt"},
              str([f.get("name") for f in listing.get("files", [])]))

        response = client.get("/api/files/raw", headers=bob, params={"path": str(shared_file)})
        check("recipient can read a shared file",
              response.status_code == 200 and response.text == "trip-one", str(response.status_code))

        response = client.get("/api/files/stat", headers=bob, params={"path": str(shared_dir)})
        check("shared stat is read-only",
              response.status_code == 200 and response.json().get("can_write") is False,
              str(response.status_code))

        # ── write isolation ──────────────────────────────────────────────
        response = client.post("/api/files/mkdir", headers=bob, json={
            "path": str(shared_dir / "bob-new"),
        })
        check("recipient cannot create folders in a share", response.status_code == 403, str(response.status_code))
        response = client.put("/api/files/rename", headers=bob, json={
            "path": str(shared_file), "new_name": "renamed.txt",
        })
        check("recipient cannot rename shared files", response.status_code == 403, str(response.status_code))
        response = client.request("DELETE", "/api/files", headers=bob, json={
            "path": str(shared_file),
        })
        check("recipient cannot delete shared files", response.status_code == 403, str(response.status_code))
        check("shared files were not modified",
              shared_file.exists() and shared_file.read_text() == "trip-one")

        response = client.get("/api/files/raw", headers=bob, params={"path": str(secret_file)})
        check("recipient cannot read an ungranted sibling", response.status_code == 403, str(response.status_code))

        # ── update recipients ────────────────────────────────────────────
        response = client.patch(f"/api/sharing/grants/{grant_id}", headers=alice, json={
            "recipients": [BOB, CAROL],
        })
        check("owner can add a recipient", response.status_code == 200, str(response.status_code))
        check("new recipient gets access",
              client.get("/api/files/raw", headers=carol,
                         params={"path": str(shared_file)}).status_code == 200)
        response = client.patch(f"/api/sharing/grants/{grant_id}", headers=bob, json={
            "recipients": [CAROL],
        })
        check("non-owner cannot edit a grant", response.status_code == 404, str(response.status_code))

        # ── revoke ───────────────────────────────────────────────────────
        response = client.delete(f"/api/sharing/grants/{grant_id}", headers=bob)
        check("non-owner cannot revoke a grant", response.status_code == 404, str(response.status_code))
        response = client.delete(f"/api/sharing/grants/{grant_id}", headers=alice)
        check("owner can revoke a grant", response.status_code == 200, str(response.status_code))
        response = client.get("/api/files/raw", headers=bob, params={"path": str(shared_file)})
        check("revoke removes read access immediately", response.status_code == 403, str(response.status_code))
        response = client.get("/api/sharing/incoming", headers=bob)
        check("revoked grant disappears from incoming",
              (response.json() or {}).get("grants") == [], str(response.json()))

        # ── all users, including future accounts ─────────────────────────
        response = client.post("/api/sharing/grants", headers=alice, json={
            "kind": "path", "resource": str(shared_dir), "all": True,
        })
        check("owner can share with all users", response.status_code == 200, str(response.status_code))
        all_grant_id = (response.json() or {}).get("grant", {}).get("id")
        users_store.update(lambda rows: rows.append(_record(DAVE)))
        dave = {"Authorization": f"Bearer {_token('user', DAVE)}"}
        response = client.get("/api/sharing/incoming", headers=dave)
        check("a new account inherits an all-users grant",
              len((response.json() or {}).get("grants") or []) == 1, str(response.json()))
        check("a new account can read the shared file",
              client.get("/api/files/raw", headers=dave,
                         params={"path": str(shared_file)}).status_code == 200)
        client.delete(f"/api/sharing/grants/{all_grant_id}", headers=alice)

        # ── validation ───────────────────────────────────────────────────
        response = client.post("/api/sharing/grants", headers=alice, json={
            "kind": "path", "resource": str(DATA_DIR / "users.json"), "recipients": [BOB],
        })
        check("DATA_DIR cannot be shared", response.status_code == 403, str(response.status_code))
        response = client.post("/api/sharing/grants", headers=alice, json={
            "kind": "path", "resource": str(shared_dir), "recipients": ["nobody-here"],
        })
        check("unknown recipients are rejected", response.status_code == 400, str(response.status_code))
        response = client.post("/api/sharing/grants", headers=bob, json={
            "kind": "path", "resource": str(shared_dir), "recipients": [CAROL],
        })
        check("a user cannot share another user's folder", response.status_code == 403, str(response.status_code))
        response = client.post("/api/sharing/grants", headers=alice, json={
            "kind": "album", "resource": "does-not-exist", "recipients": [BOB],
        })
        check("unknown album is rejected", response.status_code == 404, str(response.status_code))

        # ── owner lifecycle ──────────────────────────────────────────────
        response = client.post("/api/sharing/grants", headers=alice, json={
            "kind": "path", "resource": str(shared_dir), "recipients": [BOB],
        })
        lifecycle_grant = (response.json() or {}).get("grant", {}).get("id")
        users_store.update(lambda rows: [
            row.update(status="disabled") if row.get("username") == ALICE else None
            for row in rows
        ])
        check("a disabled owner stops serving shares",
              client.get("/api/files/raw", headers=bob,
                         params={"path": str(shared_file)}).status_code == 403)
        users_store.update(lambda rows: [
            row.update(status="active") if row.get("username") == ALICE else None
            for row in rows
        ])
        check("re-enabling the owner restores access",
              client.get("/api/files/raw", headers=bob,
                         params={"path": str(shared_file)}).status_code == 200)
        client.delete(f"/api/sharing/grants/{lifecycle_grant}", headers=alice)

    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        shutil.rmtree(SHARE_ROOT, ignore_errors=True)

    print()
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"{len(failed)} sharing check(s) failed")
        return 1
    print(f"All {len(results)} sharing checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
