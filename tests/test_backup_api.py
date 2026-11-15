#!/usr/bin/env python3
"""Backup Manager v2 — API E2E on an isolated instance (no PAM login).

Boots its own uvicorn on a free port with a temp ETHOS_DATA_DIR, mints an
admin JWT with the same secret, then exercises schedule → run → generations →
verify → restore → authorization.

Run: .venv/bin/python tests/test_backup_api.py
"""
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

DATA = tempfile.mkdtemp(prefix="ethos-backup-api-data-")
os.environ.update({
    "ETHOS_DATA_DIR": DATA,
    "ETHOS_JWT_SECRET": "backup-api-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.routers.auth import _create_token  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(base: str, method: str, path: str, token: str = "", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            return exc.code, json.loads(payload or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"detail": payload.decode(errors="replace")}


def wait_ready(base: str) -> bool:
    end = time.time() + 45
    while time.time() < end:
        try:
            urllib.request.urlopen(base + "/api/system/status", timeout=2)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def wait_job(base: str, token: str, timeout: float = 60.0):
    end = time.time() + timeout
    while time.time() < end:
        status, payload = request(base, "GET", "/api/backup/status", token)
        if status == 200 and not payload.get("running") and not payload.get("queue"):
            return payload
        time.sleep(0.4)
    return None


def main() -> int:
    source = tempfile.mkdtemp(prefix="bm-api-src-")
    destination = tempfile.mkdtemp(prefix="bm-api-dst-")
    restore_target = tempfile.mkdtemp(prefix="bm-api-restore-")
    for index in range(3):
        with open(os.path.join(source, f"file{index}.txt"), "w", encoding="utf-8") as handle:
            handle.write(f"backup api fixture {index}\n")

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    admin = _create_token("admin", "admin", "admin")
    user = _create_token("user", "user", "user")

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=os.environ.copy(),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        if not wait_ready(base):
            print((proc.stderr.read() if proc.stderr else "")[-2000:])
            return 1

        print("\n[1] authorization")
        status, _ = request(base, "GET", "/api/backup/schedules")
        check("unauthenticated request is 401", status == 401, f"HTTP {status}")
        status, _ = request(base, "GET", "/api/backup/schedules", user)
        check("non-admin request is 403", status == 403, f"HTTP {status}")

        print("\n[2] meta")
        status, meta = request(base, "GET", "/api/backup/meta", admin)
        check("meta reports API v2", status == 200 and meta.get("api_version") == 2, str(meta))

        print("\n[3] create schedule")
        payload = {
            "name": "API test", "type": "folder", "source": source,
            "destination": destination, "cron": "0 4 1 1 *",
        }
        status, created = request(base, "POST", "/api/backup/schedules", admin, payload)
        check("create returns 201", status == 201, f"HTTP {status} {created}")
        schedule = created.get("schedule") or {}
        schedule_id = schedule.get("id")
        check("schedule id returned", bool(schedule_id))
        check("retention default applied", schedule.get("retention", {}).get("keep_daily") == 7,
              str(schedule.get("retention")))

        status, bad = request(base, "POST", "/api/backup/schedules", admin,
                              {**payload, "destination": source})
        check("source==destination rejected with 400", status == 400, f"HTTP {status} {bad}")

        status, listing = request(base, "GET", "/api/backup/schedules", admin)
        check("schedule listed", status == 200 and len(listing.get("schedules") or []) == 1)

        print("\n[4] run now")
        status, run = request(base, "POST", f"/api/backup/schedules/{schedule_id}/run", admin)
        check("run accepted with 202", status == 202, f"HTTP {status} {run}")
        final = wait_job(base, admin)
        check("job finished", final is not None, "timeout")
        history = request(base, "GET", "/api/backup/history?limit=5", admin)[1]
        entry = (history.get("items") or [{}])[0]
        check("history records success", entry.get("status") == "success", str(entry)[:300])

        print("\n[5] generations + verify")
        status, generations = request(base, "GET", f"/api/backup/schedules/{schedule_id}/generations", admin)
        items = generations.get("generations") or []
        complete = [item for item in items if item.get("status") == "complete"]
        check("generation listed", status == 200 and len(complete) == 1, str(items)[:300])
        generation = complete[0]["generation"] if complete else ""
        check("post-run verify ok", complete and complete[0]["verified"].get("status") == "ok",
              str(complete[0].get("verified") if complete else ""))

        status, queued = request(base, "POST",
                                 f"/api/backup/schedules/{schedule_id}/generations/{generation}/verify",
                                 admin, {"deep": False})
        check("verify accepted", status == 202, f"HTTP {status} {queued}")
        check("verify job finished", wait_job(base, admin) is not None)

        print("\n[6] restore to another folder")
        status, restored = request(base, "POST", f"/api/backup/schedules/{schedule_id}/restore",
                                   admin, {"generation": generation, "mode": "to_folder",
                                           "target": restore_target})
        check("restore accepted", status == 202, f"HTTP {status} {restored}")
        check("restore job finished", wait_job(base, admin) is not None)
        check("restored file exists", os.path.isfile(os.path.join(restore_target, "file1.txt")))

        status, bad_restore = request(base, "POST", f"/api/backup/schedules/{schedule_id}/restore",
                                      admin, {"mode": "nope"})
        check("invalid restore mode rejected", status == 400, f"HTTP {status} {bad_restore}")

        print("\n[7] status shape")
        status, state = request(base, "GET", "/api/backup/status", admin)
        check("idle status has op=null", status == 200 and state.get("op") is None, str(state))

        status, checked = request(base, "GET",
                                  f"/api/backup/destinations/check?path={destination}", admin)
        check("destination check works", status == 200 and checked.get("exists") is True, str(checked))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(source, ignore_errors=True)
        shutil.rmtree(destination, ignore_errors=True)
        shutil.rmtree(restore_target, ignore_errors=True)

    print(f"\n{len(FAILURES)} failure(s)")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
