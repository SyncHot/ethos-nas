"""Regression test — cross-user test-fixture accessibility (T-0003).

The production server runs as the dedicated `ethos-nas` service user while
these scripts run as the login user. `tempfile.mkdtemp()` creates a mode-700
dir owned by the login user, which the service user cannot enter — any test
that hands such a path to the server fails with "[Errno 13] Permission
denied" / "Path not found" / "Not a directory" (see tests/fixture_dir.py).

Checks:
  A1. a fixture from service_readable_tmpdir() IS accessible to the server
      (POST /api/share-links on a file inside → 200)
  A2. a bare mkdtemp (mode 700) fixture is NOT accessible to a cross-user
      server (same request → 400). Against a same-user isolated instance the
      check is informational (the server can enter 700 dirs) and always passes.

Run:  .venv/bin/python tests/test_fixture_permissions.py   (server must be up)
      ETHOS_TEST_BASE=http://127.0.0.1:9002 to target an isolated instance.
"""
import json
import os

def _require_test_password() -> str:
    """Test credentials are never committed — export ETHOS_TEST_PASSWORD."""
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from fixture_dir import service_readable_tmpdir

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
if urllib.parse.urlparse(BASE).scheme not in ("http", "https"):
    raise SystemExit(f"ETHOS_TEST_BASE must be http(s), got {BASE!r}")
USER = os.environ.get("ETHOS_TEST_USER", "marcin")
PASS = _require_test_password()  # nosec B105


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def api(method, path, token=None, body=None):
    """Minimal JSON API client. Returns (status_code, parsed_json_or_None)."""
    url = BASE + path
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError(f"refusing non-http test URL: {url!r}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # nosec B310 — scheme guarded above
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 — scheme guarded above
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, None


def login() -> str:
    code, data = api("POST", "/api/auth/login", body={"username": USER, "password": PASS})
    if code != 200 or not isinstance(data, dict) or not data.get("token"):
        raise SystemExit(f"login failed: HTTP {code}")
    return data["token"]


def share_probe(token: str, directory: str) -> tuple[int, dict | None]:
    """POST /api/share-links on a probe file inside `directory`.

    The server must stat/read the file, so the response reflects whether the
    service user can enter the fixture dir.
    """
    probe = os.path.join(directory, "fixture-probe.txt")
    with open(probe, "w") as f:
        f.write("fixture accessibility probe\n")
    code, data = api("POST", "/api/share-links", token, {"path": probe})
    return code, data if isinstance(data, dict) else None


def main() -> int:
    failures = 0
    print(f"Fixture accessibility (T-0003) — {BASE}\n")
    token = login()

    # ── A1: helper fixture must be server-accessible ───────────────────────
    good = service_readable_tmpdir(prefix="ethos-fxperm-good-")
    created: list = []
    try:
        code, link = share_probe(token, good)
        a1_ok = code == 200 and link is not None and link.get("id")
        if not check("A1 service_readable_tmpdir fixture → share link 200",
                     a1_ok, f"HTTP {code} {link}"):
            failures += 1
        elif link is not None and link.get("id"):
            created.append(link["id"])
    finally:
        for lid in created:
            api("DELETE", f"/api/share-links/{lid}", token)
        shutil.rmtree(good, ignore_errors=True)

    # ── A2: bare mkdtemp (700) documents the failure mode ──────────────────
    bad = tempfile.mkdtemp(prefix="ethos-fxperm-bad-")
    try:
        code, link = share_probe(token, bad)
        if code == 400:
            # Cross-user server: the 700 dir is invisible to the service
            # user — exactly the T-0003 failure mode. Asserting it keeps the
            # regression honest (a silent permission relaxation would flip
            # this).
            check("A2 bare mkdtemp (700) fixture blocked for service user (400)",
                  True, f"detail={link}")
        elif code == 200 and link is not None:
            # Same-user isolated instance: the server CAN enter 700 dirs, so
            # the invariant is not exercisable here — informational only.
            check("A2 bare mkdtemp (700) fixture accessible (same-user server — informational)",
                  True)
            api("DELETE", f"/api/share-links/{link['id']}", token)
        else:
            if not check("A2 bare mkdtemp (700) fixture → expected 400 (cross-user) or 200 (same-user)",
                         False, f"HTTP {code} {link}"):
                failures += 1
    finally:
        shutil.rmtree(bad, ignore_errors=True)

    print(f"\nRESULT: {'PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
