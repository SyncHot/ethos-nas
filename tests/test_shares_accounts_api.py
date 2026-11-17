#!/usr/bin/env python3
"""API authorization and credential-safety tests for Samba account routes."""

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
DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-shares-account-api-"))
SECRET = "shares-account-api-test-secret"
os.environ["ETHOS_DATA_DIR"] = str(DATA_DIR)
os.environ["ETHOS_JWT_SECRET"] = SECRET
os.environ["ETHOS_PACKAGES_DEFAULT_SOURCE"] = "0"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402
from backend.routers import shares as shares_router  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _token(role: str) -> str:
    def encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    header = encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = encode(json.dumps({
        "sub": "1", "username": "shares-test", "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    signature = encode(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def main() -> int:
    original_list = shares_router.get_samba_accounts
    original_set = shares_router.set_samba_password
    original_delete = shares_router.delete_samba_account
    calls = []
    shares_router.get_samba_accounts = lambda: {"users": [], "groups": []}
    shares_router.set_samba_password = lambda username, password: (
        calls.append((username, password)) or {"username": username, "status": "updated"}
    )
    shares_router.delete_samba_account = lambda username: {"username": username, "status": "deleted"}
    client = TestClient(app, raise_server_exceptions=False)
    admin = {"Authorization": f"Bearer {_token('admin')}"}
    user = {"Authorization": f"Bearer {_token('user')}"}
    try:
        response = client.get("/api/shares/accounts")
        check("anonymous account listing is rejected", response.status_code == 401, str(response.status_code))
        response = client.get("/api/shares/accounts", headers=user)
        check("non-admin account listing is rejected", response.status_code == 403, str(response.status_code))
        response = client.get("/api/shares/accounts", headers=admin)
        check("admin can list Samba accounts", response.status_code == 200 and response.json() == {"users": [], "groups": []})
        def missing_samba():
            raise shares_router.SambaUnavailable("Samba tools are not installed")

        shares_router.get_samba_accounts = missing_samba
        response = client.get("/api/shares/accounts", headers=admin)
        check("account listing reports missing Samba as an actionable 503",
              response.status_code == 503 and "Samba tools" in response.json().get("detail", ""))
        shares_router.get_samba_accounts = lambda: {"users": [], "groups": []}

        response = client.put("/api/shares/accounts/alice/password", json={"password": "valid test password"})
        check("anonymous password update is rejected", response.status_code == 401, str(response.status_code))
        response = client.put("/api/shares/accounts/alice/password", headers=user,
                              json={"password": "valid test password"})
        check("non-admin password update is rejected", response.status_code == 403, str(response.status_code))
        secret = "unique-test-password-value"
        response = client.put("/api/shares/accounts/alice/password", headers=admin,
                              json={"password": secret})
        check("admin password update succeeds without returning secret",
              response.status_code == 200 and secret not in response.text)
        check("password reaches only the service call", calls == [("alice", secret)])
        response = client.put("/api/shares/accounts/alice/password", headers=admin,
                              json={"password": "short"})
        check("short passwords are rejected at the API boundary", response.status_code == 422,
              str(response.status_code))

        response = client.delete("/api/shares/accounts/alice", headers=user)
        check("non-admin account removal is rejected", response.status_code == 403, str(response.status_code))
        response = client.delete("/api/shares/accounts/alice", headers=admin)
        check("admin can remove only Samba credentials", response.status_code == 200
              and response.json().get("status") == "deleted")
    finally:
        shares_router.get_samba_accounts = original_list
        shares_router.set_samba_password = original_set
        shares_router.delete_samba_account = original_delete
        shutil.rmtree(DATA_DIR, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())