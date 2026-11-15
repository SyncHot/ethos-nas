"""Regression test — Files hardening (rename collisions, chunked uploads, search).

Checks the bugs fixed in docs/file-manager.md (§1.1):

  B1  rename must NOT overwrite an existing sibling (409), must reject `.`/`..`
      and names containing a slash (400), and must still work for valid names.
  B7  a zero-byte chunked upload (init → complete, no chunks) succeeds.
  B8  out-of-order chunks are rejected instead of corrupting the file.
  B9  GET /api/files/stat returns item_count/owner/mode for the Properties dialog.
  --  GET /api/files returns can_write (the UI uses it to disable actions).
  U10 recursive search does not descend into node_modules/.git/etc.

Self-cleaning: everything lives in one service-writable /tmp fixture that is
removed at the end.

Run:  .venv/bin/python tests/test_file_manager_fixes.py   (server must be up)
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
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fixture_dir import service_writable_tmpdir

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()


def api(method: str, path: str, token: str | None = None, body: dict | None = None):
    """JSON request helper → (status, parsed body or None)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, None


def upload_chunk(token: str, upload_id: str, chunk_index: int, blob: bytes):
    """POST one multipart chunk to /api/files/chunked/upload."""
    boundary = "----ethos" + uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="upload_id"\r\n\r\n{upload_id}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="chunk_index"\r\n\r\n{chunk_index}\r\n'.encode(),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="data"; filename="blob"\r\n'
         "Content-Type: application/octet-stream\r\n\r\n").encode() + blob + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(f"{BASE}/api/files/chunked/upload", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def complete_upload(token: str, upload_id: str):
    """POST /api/files/chunked/complete (multipart form)."""
    boundary = "----ethos" + uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="upload_id"\r\n\r\n'
            f"{upload_id}\r\n--{boundary}--\r\n").encode()
    req = urllib.request.Request(f"{BASE}/api/files/chunked/complete", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
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

    status, login = api("POST", "/api/auth/login", body={"username": USER, "password": PASS})
    if status != 200 or not login:
        print(f"  [FAIL] login — HTTP {status}")
        return 1
    token = login.get("token") or login.get("access_token")
    check("login", bool(token))
    if not token:
        return 1

    fixture = service_writable_tmpdir("ethos-fm-fixes-")
    try:
        a = os.path.join(fixture, "a.txt")
        b = os.path.join(fixture, "b.txt")
        for p in (a, b):
            with open(p, "w") as f:
                f.write("x")
        os.chmod(a, 0o666)
        os.chmod(b, 0o666)

        print("Step 1: rename must not clobber an existing file")
        status, body = api("PUT", "/api/files/rename", token, {"path": a, "new_name": "b.txt"})
        check("rename onto existing → 409", status == 409, f"HTTP {status} {body}")
        check("target file untouched", open(b).read() == "x")

        status, _ = api("PUT", "/api/files/rename", token, {"path": a, "new_name": ".."})
        check("rename to '..' → 400", status == 400, f"HTTP {status}")
        status, _ = api("PUT", "/api/files/rename", token, {"path": a, "new_name": "sub/evil.txt"})
        check("rename with slash → 400", status == 400, f"HTTP {status}")
        check("source still exists", os.path.exists(a))

        status, _ = api("PUT", "/api/files/rename", token, {"path": a, "new_name": "c.txt"})
        renamed = os.path.join(fixture, "c.txt")
        check("valid rename → 200", status == 200, f"HTTP {status}")
        check("renamed file exists", os.path.exists(renamed) and not os.path.exists(a))

        print("Step 2: listing exposes can_write and the new name")
        status, listing = api("GET", f"/api/files?path={urllib.parse.quote(fixture)}", token)
        names = {f["name"] for f in (listing or {}).get("files", [])}
        check("can_write present", isinstance((listing or {}).get("can_write"), bool))
        check("renamed entry listed", "c.txt" in names, str(sorted(names)))

        print("Step 3: /api/files/stat powers the Properties dialog")
        status, st = api("GET", f"/api/files/stat?path={urllib.parse.quote(renamed)}", token)
        check("stat → 200", status == 200, f"HTTP {status}")
        check("stat has size/owner/mode",
              bool(st) and st.get("size") == 1 and bool(st.get("owner")) and bool(st.get("permissions")),
              str(st))
        status, dst = api("GET", f"/api/files/stat?path={urllib.parse.quote(fixture)}", token)
        check("dir stat has item_count", bool(dst) and isinstance(dst.get("item_count"), int), str(dst))

        print("Step 4: zero-byte chunked upload")
        status, init = api("POST", "/api/files/chunked/init", token,
                           {"file_id": "empty-" + uuid.uuid4().hex[:8], "filename": "empty.bin",
                            "dest_dir": fixture})
        check("init → 200", status == 200, f"HTTP {status}")
        if status == 200 and init:
            status, res = complete_upload(token, init["upload_id"])
            empty_path = os.path.join(fixture, "empty.bin")
            check("complete without chunks → 200", status == 200, f"HTTP {status} {res}")
            check("0-byte file created", os.path.exists(empty_path) and os.path.getsize(empty_path) == 0)

        print("Step 5: out-of-order chunk is rejected")
        status, init = api("POST", "/api/files/chunked/init", token,
                           {"file_id": "ooo-" + uuid.uuid4().hex[:8], "filename": "ooo.bin",
                            "dest_dir": fixture})
        if status == 200 and init:
            status, _ = upload_chunk(token, init["upload_id"], 1, b"payload")
            check("chunk 1 before 0 → 400", status == 400, f"HTTP {status}")
            status, _ = upload_chunk(token, init["upload_id"], 0, b"payload")
            check("chunk 0 accepted", status == 200, f"HTTP {status}")
            status, _ = complete_upload(token, init["upload_id"])
            ooo = os.path.join(fixture, "ooo.bin")
            check("upload finalized intact",
                  status == 200 and os.path.exists(ooo) and open(ooo, "rb").read() == b"payload",
                  f"HTTP {status}")

        print("Step 6: search skips node_modules/.git")
        os.makedirs(os.path.join(fixture, "node_modules"), exist_ok=True)
        with open(os.path.join(fixture, "node_modules", "needle.txt"), "w") as f:
            f.write("n")
        with open(os.path.join(fixture, "needle-root.txt"), "w") as f:
            f.write("n")
        status, found = api(
            "GET", f"/api/files/search?q=needle&path={urllib.parse.quote(fixture)}", token)
        paths = {r["path"] for r in (found or {}).get("results", [])}
        check("search → 200", status == 200, f"HTTP {status}")
        check("match at root found", os.path.join(fixture, "needle-root.txt") in paths, str(paths))
        check("node_modules not crawled",
              os.path.join(fixture, "node_modules", "needle.txt") not in paths, str(paths))
    finally:
        shutil.rmtree(fixture, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
