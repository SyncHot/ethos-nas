#!/usr/bin/env python3
"""API tests for /api/users lifecycle and the self-service password change.

The root helper is replaced with a stub (ETHOS_USER_ACCOUNTS_HELPER), so no
Linux account is touched. Samba and share links are stubbed as well.
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
DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-users-api-"))
STUB_DIR = Path(tempfile.mkdtemp(prefix="ethos-users-stub-"))
STUB_LOG = STUB_DIR / "calls.jsonl"
STUB = STUB_DIR / "ethos-user-accounts"
STUB.write_text(
    """#!/usr/bin/env python3
import json, os, sys
request = json.loads(sys.stdin.read())
with open(os.environ["ETHOS_USER_ACCOUNTS_STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(request) + "\\n")
if request.get("username") == "ethostestboom":
    print("Account deletion failed — the user may still have running processes", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"ok": True, "username": request.get("username"),
                  "uid": 1500, "home": "/home/" + str(request.get("username")), "acl": True}))
""",
    encoding="utf-8",
)
STUB.chmod(0o755)

SECRET = "users-api-test-secret"
os.environ["ETHOS_DATA_DIR"] = str(DATA_DIR)
os.environ["ETHOS_JWT_SECRET"] = SECRET
os.environ["ETHOS_PACKAGES_DEFAULT_SOURCE"] = "0"
os.environ["ETHOS_USER_ACCOUNTS_HELPER"] = str(STUB)
os.environ["ETHOS_USER_ACCOUNTS_STUB_LOG"] = str(STUB_LOG)
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402
from backend.routers import auth as auth_router  # noqa: E402
from backend.services import json_store, share_links_service, users_store, user_accounts  # noqa: E402

ADMIN = "ethostestadmin"
ALICE = "ethostestalice"
BOB = "ethostestbob"
CAROL = "ethostestcarol"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _token(role: str, username: str = ADMIN, uid: str = "1") -> str:
    def encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    header = encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = encode(json.dumps({
        "sub": uid, "username": username, "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    signature = encode(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def _calls() -> list[dict]:
    if not STUB_LOG.exists():
        return []
    return [json.loads(line) for line in STUB_LOG.read_text(encoding="utf-8").splitlines() if line.strip()]


def _clear_calls() -> None:
    if STUB_LOG.exists():
        STUB_LOG.unlink()


def main() -> int:
    # The admin record is what makes the middleware trust the role; the seed is
    # the same shape auth_pam writes on a real install.
    users_store.save([{
        "id": "1", "username": ADMIN, "role": "admin", "status": "active",
        "created_at": "2026-01-01T00:00:00Z", "last_login": None,
        "source": "test", "api_key": "",
    }])

    original_list = share_links_service.list_links
    original_delete_link = share_links_service.delete_link
    original_samba_list = user_accounts.shares_service.get_samba_accounts
    original_samba_delete = user_accounts.shares_service.delete_samba_account
    original_samba_set = user_accounts.shares_service.set_samba_password
    original_pam = auth_router.pam_authenticate
    original_call = user_accounts._call

    samba_calls = []
    deleted_links = []
    user_accounts.shares_service.get_samba_accounts = lambda: {"users": [], "groups": []}
    user_accounts.shares_service.delete_samba_account = lambda name: (
        samba_calls.append(("delete", name)) or {"username": name, "status": "deleted"})
    user_accounts.shares_service.set_samba_password = lambda name, password: (
        samba_calls.append(("set", name)) or {"username": name, "status": "updated"})
    share_links_service.list_links = lambda: [
        {"id": "link-bob", "owner": BOB}, {"id": "link-keep", "owner": "someone-else"},
    ]
    share_links_service.delete_link = lambda link_id: (
        deleted_links.append(link_id) or True)

    client = TestClient(app, raise_server_exceptions=False)
    admin = {"Authorization": f"Bearer {_token('admin')}"}
    plain = {"Authorization": f"Bearer {_token('user', 'ethostestplain')}"}

    try:
        # ── authorization ───────────────────────────────────────────────
        response = client.get("/api/users")
        check("anonymous listing is rejected", response.status_code == 401, str(response.status_code))
        response = client.get("/api/users", headers=plain)
        check("non-admin listing is rejected", response.status_code == 403, str(response.status_code))
        for method, path, body in (
            ("post", "/api/users", {"username": "x", "password": "correct horse 42"}),
            ("patch", f"/api/users/{ALICE}", {"role": "admin"}),
            ("post", f"/api/users/{ALICE}/password", {"password": "correct horse 42"}),
            ("delete", f"/api/users/{ALICE}", None),
        ):
            kwargs = {"headers": plain}
            if body is not None:
                kwargs["json"] = body
            response = getattr(client, method)(path, **kwargs)
            check(f"non-admin {method.upper()} {path} is rejected",
                  response.status_code == 403, str(response.status_code))

        # ── create validation ──────────────────────────────────────────
        _clear_calls()
        response = client.post("/api/users", headers=admin,
                               json={"username": "Alice", "password": "correct horse 42"})
        check("uppercase username is rejected with 422", response.status_code == 422,
              str(response.status_code))
        response = client.post("/api/users", headers=admin,
                               json={"username": "root", "password": "correct horse 42"})
        check("reserved username is rejected with 422", response.status_code == 422)
        response = client.post("/api/users", headers=admin,
                               json={"username": ALICE, "password": "short"})
        check("weak password is rejected with 422", response.status_code == 422)
        response = client.post("/api/users", headers=admin,
                               json={"username": ALICE, "password": "correct horse 42",
                                     "surprise": True})
        check("unknown fields are rejected", response.status_code == 422)
        check("invalid requests never reach the helper", not _calls())

        secret = "correct horse 42"
        response = client.post("/api/users", headers=admin,
                               json={"username": ALICE, "password": secret})
        payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        check("admin creates an account", response.status_code == 201,
              f"{response.status_code} {response.text[:120]}")
        check("created account is an active user",
              payload.get("user", {}).get("role") == "user"
              and payload.get("user", {}).get("status") == "active")
        check("password is never echoed", secret not in response.text)
        check("helper receives the create request",
              any(call.get("action") == "create" and call.get("username") == ALICE for call in _calls()))

        response = client.post("/api/users", headers=admin,
                               json={"username": ALICE, "password": secret})
        check("duplicate account is a 409", response.status_code == 409, str(response.status_code))

        # ── listing ────────────────────────────────────────────────────
        response = client.get("/api/users", headers=admin)
        body = response.json()
        names = [u["username"] for u in body.get("users", [])]
        check("listing includes registry accounts", response.status_code == 200 and ALICE in names)
        check("listing exposes status and Linux state",
              all("status" in u and "linux" in u and "samba" in u for u in body.get("users", [])))
        check("listing never leaks API keys", "api_key" not in response.text)

        # ── roles ──────────────────────────────────────────────────────
        response = client.patch(f"/api/users/{ALICE}", headers=admin, json={"role": "admin"})
        check("admin can promote a user", response.status_code == 200
              and response.json()["user"]["role"] == "admin")
        forged = {"Authorization": f"Bearer {_token('user', ALICE, '2')}"}
        response = client.get("/api/users", headers=forged)
        check("role comes from the store, not the JWT claim", response.status_code == 200,
              str(response.status_code))
        response = client.patch(f"/api/users/{ALICE}", headers=admin, json={"role": "user"})
        check("admin can demote a user", response.status_code == 200
              and response.json()["user"]["role"] == "user")
        response = client.get("/api/users", headers=forged)
        check("demotion applies on the next request (403)", response.status_code == 403,
              str(response.status_code))

        # ── status / lock ──────────────────────────────────────────────
        _clear_calls()
        response = client.patch(f"/api/users/{ALICE}", headers=admin, json={"status": "disabled"})
        check("admin can disable an account", response.status_code == 200
              and response.json()["user"]["status"] == "disabled")
        check("disabling locks the Linux account",
              any(call.get("action") == "lock" for call in _calls()))
        response = client.get("/api/auth/me", headers=forged)
        check("disabled account's JWT is rejected immediately",
              response.status_code == 401 and "disabled" in response.json().get("detail", "").lower(),
              f"{response.status_code} {response.text[:80]}")
        response = client.patch(f"/api/users/{ALICE}", headers=admin, json={"status": "active"})
        check("admin can re-enable an account", response.status_code == 200)
        response = client.get("/api/auth/me", headers=forged)
        check("re-enabled account's JWT works again", response.status_code == 200)

        # ── guards ─────────────────────────────────────────────────────
        response = client.patch(f"/api/users/{ADMIN}", headers=admin, json={"role": "user"})
        check("cannot remove your own admin role", response.status_code == 422)
        response = client.patch(f"/api/users/{ADMIN}", headers=admin, json={"status": "disabled"})
        check("cannot disable your own account", response.status_code == 422)
        response = client.delete(f"/api/users/{ADMIN}", headers=admin)
        check("cannot delete your own account", response.status_code == 422)

        # Only ADMIN is left before ALICE is promoted again? ALICE is 'user' now,
        # so admin count is 1 — the service guard must refuse an outside actor.
        try:
            user_accounts.delete_user(ADMIN, actor="system")
            refused = False
        except user_accounts.UserAccountConflict:
            refused = True
        check("service refuses to delete the last active admin", refused)
        try:
            user_accounts.set_role(ADMIN, "user", actor="system")
            refused = False
        except user_accounts.UserAccountConflict:
            refused = True
        check("service refuses to demote the last active admin", refused)

        # ── delete + purge ─────────────────────────────────────────────
        response = client.post("/api/users", headers=admin,
                               json={"username": BOB, "password": secret})
        check("second account created for deletion", response.status_code == 201)
        bob_token = {"Authorization": f"Bearer {_token('user', BOB, '3')}"}

        bob_dir = DATA_DIR / "users" / BOB
        bob_dir.mkdir(parents=True, exist_ok=True)
        (bob_dir / "gallery_config.json").write_text("{}", encoding="utf-8")
        (DATA_DIR / "trash" / BOB).mkdir(parents=True, exist_ok=True)
        json_store.get(DATA_DIR / "wallpapers.json").save({BOB: {"wallpaper": "aurora"}, "keep": {}})
        json_store.get(DATA_DIR / "favorites.json").save({BOB: ["/mnt/x"], "keep": ["/mnt/y"]})
        json_store.get(DATA_DIR / "desktop_layouts.json").save({BOB: {"order": []}, "keep": {"order": []}})
        json_store.get(DATA_DIR / "downloads_tasks.json", list).save(
            [{"owner": BOB, "name": "mine"}, {"owner": "keep", "name": "theirs"}])
        users_store.update(lambda users: next(
            u for u in users if u["username"] == BOB
        ).update({"api_key": "ethos_" + "b" * 48}))

        response = client.delete(f"/api/users/{BOB}?remove_home=false&remove_ethos_state=true",
                                 headers=admin)
        body = response.json()
        check("admin deletes an account", response.status_code == 200 and body.get("ok") is True,
              f"{response.status_code} {response.text[:120]}")
        check("deletion tombstones the record",
              users_store.status_of(users_store.find_username(BOB)) == users_store.STATUS_DELETED)
        check("Samba credentials are removed", ("delete", BOB) in samba_calls)
        check("home is kept by default", body.get("home_removed") is False)
        check("per-user state is purged",
              not bob_dir.exists() and not (DATA_DIR / "trash" / BOB).exists())
        check("global stores drop the user's slices",
              BOB not in json_store.get(DATA_DIR / "wallpapers.json").load()
              and BOB not in json_store.get(DATA_DIR / "favorites.json").load()
              and BOB not in json_store.get(DATA_DIR / "desktop_layouts.json").load())
        check("download tasks owned by the user are removed",
              all(task.get("owner") != BOB
                  for task in json_store.get(DATA_DIR / "downloads_tasks.json", list).load()))
        check("the user's public share links are revoked", deleted_links == ["link-bob"])
        response = client.get("/api/users", headers=admin)
        check("deleted accounts are hidden by default",
              BOB not in [u["username"] for u in response.json().get("users", [])])
        response = client.get("/api/users?include_deleted=true", headers=admin)
        check("include_deleted shows the tombstone",
              BOB in [u["username"] for u in response.json().get("users", [])])
        response = client.get("/api/auth/me", headers=bob_token)
        check("deleted account's JWT is rejected immediately", response.status_code == 401,
              str(response.status_code))
        response = client.post("/api/auth/login", json={"api_key": "ethos_" + "b" * 48})
        check("deleted account's API key is rejected", response.status_code == 401,
              str(response.status_code))

        # Deleting again is a retry (e.g. the Linux account had live processes).
        response = client.delete(f"/api/users/{BOB}?remove_home=false&remove_ethos_state=true",
                                 headers=admin)
        check("deletion can be retried after a helper failure", response.status_code == 200)

        # ── Samba on create ────────────────────────────────────────────
        _clear_calls()
        response = client.post("/api/users", headers=admin,
                               json={"username": CAROL, "password": secret,
                                     "samba_password": secret})
        check("create can set a Samba password in one step",
              response.status_code == 201 and ("set", CAROL) in samba_calls,
              f"{response.status_code} {response.text[:120]}")
        check("samba password is not echoed", secret not in response.text)

        # ── helper unavailable ─────────────────────────────────────────
        def unavailable(_request):
            raise user_accounts.UserAccountUnavailable(
                "Account helper is unavailable; rerun setup_service.sh once on this server")

        user_accounts._call = unavailable
        response = client.post("/api/users", headers=admin,
                               json={"username": "ethostestdave", "password": secret})
        check("missing helper maps to an actionable 503",
              response.status_code == 503 and "setup_service.sh" in response.json().get("detail", ""))
        user_accounts._call = original_call

        # A stale install has the helper path in sudoers but not the file:
        # sudo exits 1 with "command not found" — still a deployment 503.
        class _MissingHelper:
            returncode = 1
            stdout = ""
            stderr = "sudo: /usr/local/bin/ethos-user-accounts: command not found"

        original_run = user_accounts.subprocess.run
        user_accounts.subprocess.run = lambda *args, **kwargs: _MissingHelper()
        response = client.post("/api/users", headers=admin,
                               json={"username": "ethostesterin", "password": secret})
        check("stale helper installation maps to 503",
              response.status_code == 503 and "setup_service.sh" in response.json().get("detail", ""),
              f"{response.status_code} {response.text[:120]}")
        user_accounts.subprocess.run = original_run

        # ── legacy alias ───────────────────────────────────────────────
        response = client.get("/api/auth/users", headers=admin)
        check("legacy /api/auth/users still lists accounts",
              response.status_code == 200 and response.json().get("total", 0) >= 1)

        # ── self-service password change ───────────────────────────────
        auth_router.pam_authenticate = lambda username, password: password == "current secret 1"
        _clear_calls()
        response = client.post("/api/auth/password", headers=admin,
                               json={"current_password": "current secret 1",
                                     "new_password": "brand new secret 9"})
        check("user can change their own password", response.status_code == 200,
              f"{response.status_code} {response.text[:120]}")
        check("self-service change reaches the helper",
              any(call.get("action") == "set_password" and call.get("username") == ADMIN
                  for call in _calls()))
        response = client.post("/api/auth/password", headers=admin,
                               json={"current_password": "wrong", "new_password": "brand new secret 9"})
        check("wrong current password is rejected", response.status_code == 401,
              str(response.status_code))
        response = client.post("/api/auth/password", headers=admin,
                               json={"current_password": "current secret 1", "new_password": "short"})
        check("weak new password is rejected with 422", response.status_code == 422)

        for _ in range(4):
            client.post("/api/auth/password", headers=admin,
                        json={"current_password": "wrong", "new_password": "brand new secret 9"})
        response = client.post("/api/auth/password", headers=admin,
                               json={"current_password": "current secret 1",
                                     "new_password": "brand new secret 9"})
        check("repeated failures are rate-limited", response.status_code == 429,
              str(response.status_code))
    finally:
        share_links_service.list_links = original_list
        share_links_service.delete_link = original_delete_link
        user_accounts.shares_service.get_samba_accounts = original_samba_list
        user_accounts.shares_service.delete_samba_account = original_samba_delete
        user_accounts.shares_service.set_samba_password = original_samba_set
        auth_router.pam_authenticate = original_pam
        user_accounts._call = original_call
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        shutil.rmtree(STUB_DIR, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
