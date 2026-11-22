#!/usr/bin/env python3
"""Disk Cleaner API tests — routers/disk_clean.py + services/disk_clean_service.py.

Boots its own isolated instance (throwaway port + ETHOS_DATA_DIR + test JWT
secret, no package source, no login), so it never touches production data.
Covers the scan jobs (start/poll/cancel), the cached suggestion sweep, the
protected-path policy, trash-safe and permanent deletes, history and the
per-area retention sweep.

Run:
    .venv/bin/python tests/test_disk_clean.py
"""

import base64
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "disk-clean-test-secret"
DATA = Path(tempfile.mkdtemp(prefix="ethos-disk-clean-"))
FIXTURE = Path(tempfile.mkdtemp(prefix="ethos-disk-clean-fixture-"))
os.environ.update({
    "ETHOS_DATA_DIR": str(DATA),
    "ETHOS_JWT_SECRET": SECRET,
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
    # Keep the suggestion sweep fast in CI — it walks the real /tmp, /var/cache…
    "ETHOS_DISK_CLEAN_SUGGESTION_BUDGET": "3",
})

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _token(role: str, username: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": "1", "username": username, "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base: str, method: str, path: str, token: str | None = None, body=None):
    """Return (status, parsed JSON or {})."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw or b"{}")
            except ValueError:
                return response.status, {}
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except OSError:
        return 0, {}


def _poll(base: str, path: str, token: str, predicate, timeout: float = 30.0, interval: float = 0.15):
    """Poll a JSON endpoint until `predicate(payload)` is true (or time out)."""
    deadline = time.time() + timeout
    payload: dict = {}
    while time.time() < deadline:
        status, payload = _call(base, "GET", path, token)
        if status == 200 and predicate(payload):
            return payload
        time.sleep(interval)
    return payload


def main() -> int:
    # ── Fixture tree ───────────────────────────────────────────────────────
    (FIXTURE / "sub").mkdir()
    (FIXTURE / "small").mkdir()
    (FIXTURE / "small" / "note.txt").write_bytes(b"x" * 1024)
    (FIXTURE / "sub" / "deep.bin").write_bytes(b"d" * (1024 * 1024))
    (FIXTURE / "big.bin").write_bytes(b"b" * (2 * 1024 * 1024))
    (FIXTURE / "keep.txt").write_bytes(b"k" * 2048)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "ETHOS_PORT": str(port)}
    log_path = DATA / "uvicorn.log"
    log_file = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT, text=True,
    )
    admin = _token("admin", "disk-clean")
    user = _token("user", "disk-clean")

    try:
        ready = False
        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            if _call(base, "GET", "/api/system/status")[0] == 401:
                ready = True
                break
            time.sleep(0.3)
        check("instance ready", ready)
        if not ready:
            print(log_path.read_text(encoding="utf-8", errors="replace")[-3000:])
            return 1

        # ── Auth ───────────────────────────────────────────────────────────
        check("non-admin cannot scan",
              _call(base, "GET", f"/api/disk-clean/scan?path={FIXTURE}", user)[0] == 403)
        check("non-admin cannot start a job",
              _call(base, "POST", "/api/disk-clean/scans", user, {"path": str(FIXTURE)})[0] == 403)
        check("non-admin cannot delete",
              _call(base, "POST", "/api/disk-clean/delete", user,
                    {"paths": [str(FIXTURE / "keep.txt")]})[0] == 403)
        check("no token → 401",
              _call(base, "GET", f"/api/disk-clean/scan?path={FIXTURE}")[0] == 401)

        # ── Scan job: folder tree ─────────────────────────────────────────
        status, started = _call(base, "POST", "/api/disk-clean/scans", admin,
                                {"path": str(FIXTURE), "depth": 2, "mode": "tree"})
        job_id = started.get("job_id", "")
        check("scan job accepted", status == 200 and bool(job_id), str(started))
        job = _poll(base, f"/api/disk-clean/scans/{job_id}", admin,
                    lambda payload: payload.get("state") != "running")
        check("scan job finished", job.get("state") == "done", str(job.get("state")))
        result = job.get("result") or {}
        children = (result.get("tree") or {}).get("children") or []
        names = {item.get("name") for item in children}
        check("scan reports the fixture total", int(result.get("total_size") or 0) >= 3 * 1024 * 1024,
              str(result.get("total_size")))
        check("scan tree lists the top-level entries",
              {"big.bin", "sub", "small", "keep.txt"} <= names, str(names))
        check("scan progress counts entries", int((job.get("progress") or {}).get("entries") or 0) >= 5,
              str(job.get("progress")))

        # ── Scan job: biggest files ──────────────────────────────────────
        _, started = _call(base, "POST", "/api/disk-clean/scans", admin,
                           {"path": str(FIXTURE), "mode": "files", "limit": 5})
        files_job = _poll(base, f"/api/disk-clean/scans/{started.get('job_id', '')}", admin,
                          lambda payload: payload.get("state") != "running")
        files = (files_job.get("result") or {}).get("files") or []
        sizes = [item.get("size") for item in files]
        check("biggest-files job finished", files_job.get("state") == "done")
        check("biggest file is first", bool(files) and files[0].get("name") == "big.bin", str(files[:2]))
        check("biggest files are sorted", sizes == sorted(sizes, reverse=True), str(sizes))

        # ── Cancel ─────────────────────────────────────────────────────────
        _, started = _call(base, "POST", "/api/disk-clean/scans", admin, {"path": "/usr"})
        cancel_id = started.get("job_id", "")
        cancel_status, cancel_body = _call(
            base, "POST", f"/api/disk-clean/scans/{cancel_id}/cancel", admin, {})
        check("cancel endpoint answers", cancel_status == 200, str(cancel_body))
        cancelled = _poll(base, f"/api/disk-clean/scans/{cancel_id}", admin,
                          lambda payload: payload.get("state") != "running", timeout=30)
        # The walk may finish before the flag is checked — both outcomes are valid.
        check("cancelled job settles", cancelled.get("state") in ("cancelled", "done"),
              str(cancelled.get("state")))

        # ── Suggestions (cached sweep) ────────────────────────────────────
        status, suggestions = _call(base, "GET", "/api/disk-clean/suggestions", admin)
        categories = suggestions.get("categories") or {}
        summary = suggestions.get("summary") or {}
        check("suggestions respond", status == 200, str(suggestions)[:200])
        check("suggestions carry every category",
              set(categories) == {"trash", "temp", "cache", "logs", "var_cache"}, str(list(categories)))
        check("suggestions carry a summary",
              "reclaimable_bytes" in summary and "by_category" in summary, str(summary)[:200])
        check("suggestions are timestamped", bool(suggestions.get("generated_at")))
        _, cached = _call(base, "GET", "/api/disk-clean/suggestions", admin)
        check("second sweep is served from cache", cached.get("cached") is True)
        _, refreshed = _call(base, "GET", "/api/disk-clean/suggestions?refresh=1", admin)
        check("refresh bypasses the cache", refreshed.get("cached") is False)

        # ── Protected paths ───────────────────────────────────────────────
        status, body = _call(base, "POST", "/api/disk-clean/delete", admin,
                             {"paths": [str(DATA)], "mode": "permanent"})
        check("EthOS state directory is protected", status == 403, str(body))
        status, _body = _call(base, "POST", "/api/disk-clean/delete", admin,
                              {"paths": ["/"], "mode": "permanent"})
        check("filesystem root is protected", status == 403)
        status, _body = _call(base, "POST", "/api/disk-clean/delete", admin,
                              {"paths": [str(ROOT)], "mode": "permanent"})
        check("EthOS checkout is protected", status == 403)

        # ── Trash-safe delete ─────────────────────────────────────────────
        victim = FIXTURE / "victim.txt"
        victim.write_bytes(b"v" * (1024 * 1024))
        status, started = _call(base, "POST", "/api/disk-clean/delete", admin,
                                {"paths": [str(victim)], "mode": "trash", "async": True})
        op_id = started.get("op_id", "")
        check("trash delete starts an operation", status == 200 and bool(op_id), str(started))
        op = _poll(base, f"/api/files/ops/{op_id}", admin,
                   lambda payload: payload.get("status") == "done")
        check("trash operation finished", op.get("status") == "done", str(op.get("status")))
        check("trashed file left its original path", not victim.exists())
        _, trash = _call(base, "GET", "/api/files/trash", admin)
        items = trash.get("items") or []
        entry = next((item for item in items if item.get("name") == "victim.txt"), None)
        check("trashed file is listed", entry is not None, str(items[:3]))
        if entry:
            status, _restored = _call(base, "POST", "/api/files/trash/restore", admin,
                                      {"name": entry["name"]})
            check("trashed file can be restored", status == 200 and victim.exists(), str(status))

        # ── Permanent delete of junk ─────────────────────────────────────
        junk = FIXTURE / "junk.bin"
        junk.write_bytes(b"j" * (512 * 1024))
        status, started = _call(base, "POST", "/api/disk-clean/delete", admin,
                                {"paths": [str(junk)], "mode": "permanent", "async": True})
        op_id = started.get("op_id", "")
        op = _poll(base, f"/api/files/ops/{op_id}", admin,
                   lambda payload: payload.get("status") == "done")
        check("permanent delete finished", status == 200 and op.get("status") == "done",
              str(started))
        check("permanently deleted file is gone", not junk.exists())

        # ── History (written by the background watcher) ───────────────────
        history = _poll(base, "/api/disk-clean/history?limit=10", admin,
                        lambda payload: len(payload.get("items") or []) >= 2, timeout=10)
        rows = history.get("items") or []
        actions = {row.get("action") for row in rows}
        check("history records both deletes", {"trash", "delete"} <= actions, str(actions))
        check("history entries carry bytes", all("bytes" in row for row in rows))

        # ── Managed cache purge ─────────────────────────────────────────────
        thumbnails = DATA / "thumbnails"
        thumbnails.mkdir(parents=True, exist_ok=True)
        (thumbnails / "fake.webp").write_bytes(b"t" * 4096)
        status, cleaned = _call(base, "POST", "/api/disk-clean/caches/clean", admin,
                                {"areas": ["thumbnails"]})
        area = (cleaned.get("areas") or {}).get("thumbnails") or {}
        check("thumbnail cache purge runs", status == 200, str(cleaned))
        check("thumbnail cache file is removed", not (thumbnails / "fake.webp").exists())
        check("cache purge reports freed bytes", int(area.get("bytes") or 0) >= 4096, str(area))
        status, _bad = _call(base, "POST", "/api/disk-clean/caches/clean", admin,
                             {"areas": ["video_exports"]})
        check("render-managed cache is refused", status == 400)
        status, _bad = _call(base, "POST", "/api/disk-clean/caches/clean", admin,
                             {"areas": []})
        check("empty cache selection is rejected", status == 422)

        # ── Per-area retention sweep ─────────────────────────────────────
        status, sweep = _call(base, "POST", "/api/system/retention/sweep", admin,
                              {"areas": ["thumbnails"]})
        check("single-area sweep works", status == 200 and sweep.get("ok") is True, str(sweep))
        status, _bad = _call(base, "POST", "/api/system/retention/sweep", admin,
                             {"areas": ["nope"]})
        check("unknown retention area is rejected", status == 400)
        status, sweep_all = _call(base, "POST", "/api/system/retention/sweep", admin)
        check("body-less sweep still works", status == 200 and sweep_all.get("ok") is True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_file.close()
        shutil.rmtree(DATA, ignore_errors=True)
        shutil.rmtree(FIXTURE, ignore_errors=True)

    failed = [name for name, ok, _detail in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
