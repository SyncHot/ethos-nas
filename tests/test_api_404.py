"""Tests — SPA catch-all must not swallow /api/* routes (iteration 11).

Unknown GET /api/* paths must return 404 JSON, while the SPA fallback keeps
serving index.html for "/" and client-side routes.

Part A2 (live API E2E — needs an isolated instance per AGENTS.md):
    fuser -k 9001/tcp 2>/dev/null; \
    ETHOS_PORT=9001 ETHOS_DATA_DIR=/tmp/ethos-404-test-data nohup .venv/bin/python \
        -m uvicorn backend.main:app --host 127.0.0.1 --port 9001 \
        > /tmp/ethos-nas-404-test.log 2>&1 &
    then:  ETHOS_TEST_BASE=http://127.0.0.1:9001 .venv/bin/python tests/test_api_404.py
    cleanup after stopping the instance: rm -rf /tmp/ethos-404-test-data
"""
import json
import os

def _require_test_password() -> str:
    """Test credentials are never committed — export ETHOS_TEST_PASSWORD."""
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def http(method, path, token=None, body=None):
    """Returns (status_code, raw_body_text)."""
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def login():
    code, raw = http("POST", "/api/auth/login", body={"username": USER, "password": PASS})
    if code != 200:
        raise RuntimeError(f"login failed: HTTP {code}")
    return json.loads(raw)["token"]


def part_a2(token) -> int:
    print("\nPart A2 — API E2E (isolated instance)")
    failures = 0

    # 1. Unknown /api/* GET → 404 JSON, not 200 + HTML.
    code, raw = http("GET", "/api/nonexistent-xyz", token)
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    if not check("unknown /api/* GET → 404 with JSON body",
                 code == 404 and isinstance(parsed, dict) and "detail" in parsed,
                 f"HTTP {code} body={raw[:80]!r}"):
        failures += 1

    # Nested unknown API path too.
    code, raw = http("GET", "/api/health/nope/deeper", token)
    if not check("nested unknown /api/* GET → 404 JSON",
                 code == 404 and "detail" in (json.loads(raw) or {}), f"HTTP {code}"):
        failures += 1

    # 2. SPA fallback intact: root + client-side routes still serve index.html.
    code, raw = http("GET", "/")
    if not check("GET / → 200 index.html (SPA shell)",
                 code == 200 and "<!DOCTYPE html>" in raw[:100], f"HTTP {code}"):
        failures += 1

    # Authenticated: the middleware requires a token on non-public paths, so
    # probe with one — the point is that routing (not auth) serves index.html.
    code, raw = http("GET", "/some/client-side/route", token)
    if not check("unknown non-API GET → 200 index.html (SPA routing preserved)",
                 code == 200 and "<!DOCTYPE html>" in raw[:100], f"HTTP {code}"):
        failures += 1

    # 3. Known API endpoints unaffected by the catch-all change.
    code, raw = http("GET", "/api/system/status", token)
    if not check("known /api/* GET still works (JSON)",
                 code == 200 and "cpu_percent" in raw, f"HTTP {code} body={raw[:60]!r}"):
        failures += 1

    # Unauthenticated unknown API path → auth middleware still wins (401).
    code, raw = http("GET", "/api/nonexistent-xyz")
    if not check("unauthenticated unknown /api/* GET → 401 (auth before routing)",
                 code == 401, f"HTTP {code}"):
        failures += 1

    return failures


def main():
    try:
        token = login()
    except Exception as e:
        print(f"\n[SKIP] server not reachable at {BASE} ({e})")
        print("       Start an isolated instance first:")
        print("         fuser -k 9001/tcp 2>/dev/null; ETHOS_PORT=9001 \\")
        print("           ETHOS_DATA_DIR=/tmp/ethos-404-test-data nohup .venv/bin/python -m uvicorn \\")
        print("           backend.main:app --host 127.0.0.1 --port 9001 > /tmp/ethos-nas-404-test.log 2>&1 &")
        print(f"       then re-run with ETHOS_TEST_BASE=http://127.0.0.1:9001")
        sys.exit(1)

    failures = part_a2(token)
    print(f"\n{'=' * 50}\nRESULT: {'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
