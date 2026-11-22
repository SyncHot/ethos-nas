"""Regression test — Files favorites self-heal (no phantom sidebar entries).

A favorite whose folder was deleted outside the UI (another app, SSH, a
removed share) used to stay in the sidebar forever. GET /api/files/favorites
now prunes dead links, while favorites on removable media whose volume is
currently unmounted are kept and reported with ``exists: false``.

Run:  ETHOS_TEST_PASSWORD=... .venv/bin/python tests/test_files_favorites.py
      ETHOS_TEST_BASE=http://127.0.0.1:9002 to target an isolated instance.
"""
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import uuid

from fixture_dir import service_writable_tmpdir

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
# Optional pre-minted JWT (e.g. against an isolated instance started with a known
# ETHOS_JWT_SECRET); otherwise the test logs in through PAM.
TOKEN_ENV = os.environ.get("ETHOS_TEST_TOKEN", "").strip()


def _require_test_password() -> str:
    """Test credentials are never committed — export ETHOS_TEST_PASSWORD."""
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw


PASS = "" if TOKEN_ENV else _require_test_password()


def api(method: str, path: str, token: str | None = None, body: dict | None = None):
    """JSON request helper → (status, parsed body or None)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, None


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    if TOKEN_ENV:
        token = TOKEN_ENV
        check("using ETHOS_TEST_TOKEN", True)
    else:
        status, login = api("POST", "/api/auth/login", body={"username": USER, "password": PASS})
        if status != 200 or not login:
            print(f"  [FAIL] login — HTTP {status}")
            return 1
        token = login.get("token") or login.get("access_token")
        check("login", bool(token))
        if not token:
            return 1

    fixture = service_writable_tmpdir("ethos-fav-")
    keep = os.path.join(fixture, "keep")
    gone = os.path.join(fixture, "gone")
    # Nonexistent path on a volume that is not mounted: must survive pruning.
    offline = f"/mnt/ethos-test-offline-{uuid.uuid4().hex[:8]}/sub"
    starred = [keep, gone, offline]

    try:
        os.makedirs(keep)
        os.makedirs(gone)

        print("Step 1: star two live folders and one path on an unmounted volume")
        for p in (keep, gone, offline):
            status, body = api("PUT", "/api/files/favorites", token, {"path": p, "starred": True})
            check(f"star {p}", status == 200 and p in (body or {}).get("favorites", []), f"HTTP {status} {body}")

        status, body = api("GET", "/api/files/favorites", token)
        favs = {f["path"]: f for f in (body or {}).get("favorites", [])}
        check("GET lists all three", status == 200 and {keep, gone, offline} <= set(favs), str(body))
        check("live folders report exists=true",
              favs.get(keep, {}).get("exists") is True and favs.get(gone, {}).get("exists") is True, str(favs))
        check("unmounted-media favorite kept with exists=false", favs.get(offline, {}).get("exists") is False, str(favs))

        print("Step 2: delete a starred folder behind the API's back")
        shutil.rmtree(gone)
        status, body = api("GET", "/api/files/favorites", token)
        favs = {f["path"]: f for f in (body or {}).get("favorites", [])}
        check("deleted folder pruned from response", status == 200 and gone not in favs, str(body))
        check("surviving favorite untouched", keep in favs, str(body))
        check("offline media still kept", offline in favs, str(body))

        status, body = api("GET", "/api/files/favorites", token)
        favs = {f["path"]: f for f in (body or {}).get("favorites", [])}
        check("prune persisted to the store", status == 200 and gone not in favs, str(body))

        print("Step 3: unstar removes the remaining entries")
        for p in (keep, offline):
            status, body = api("PUT", "/api/files/favorites", token, {"path": p, "starred": False})
            check(f"unstar {p}", status == 200 and p not in (body or {}).get("favorites", []), f"HTTP {status} {body}")
        status, body = api("GET", "/api/files/favorites", token)
        check("none of the test favorites remain",
              status == 200 and not ({keep, gone, offline} & {f["path"] for f in (body or {}).get("favorites", [])}),
              str(body))
    finally:
        for p in starred:  # never leave test stars behind on a shared instance
            api("PUT", "/api/files/favorites", token, {"path": p, "starred": False})
        shutil.rmtree(fixture, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) — {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
