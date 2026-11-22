#!/usr/bin/env python3
"""Downloads — API contract, authz and path-scope tests.

Boots its own isolated instance (throwaway port + ETHOS_DATA_DIR + test JWT
secret) so it can mint admin/user tokens without a PAM login and never touches
production state.

Covers:
  * /api/downloads requires a JWT
  * GET /config masks the qBittorrent key for non-admins
  * POST /config, /verify and key rotation are admin-only
  * non-admin destinations are path-scoped; private/loopback URLs are blocked
  * the qBittorrent shim accepts a magnet without the old dict→model 500
  * provider-supplied torrent paths cannot escape the task directory

Run:  .venv/bin/python tests/test_downloads.py
"""

import base64
import hashlib
import hmac
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "downloads-test-secret"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _token(role: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": "1", "username": f"dl-{role}", "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base: str, method: str, path: str, token: str | None = None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw
        return exc.code, parsed, dict(exc.headers)


def _wait_ready(base: str, proc: subprocess.Popen, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            status, _body, _h = _call(base, "GET", "/api/system/status")
        except OSError:
            status = 0
        if status == 401:
            return True
        time.sleep(0.3)
    return False


def _qb_login(base: str, password: str):
    """POST the qB login form; returns (status, sid_cookie)."""
    form = urllib.parse.urlencode({"username": "admin", "password": password}).encode()
    req = urllib.request.Request(base + "/api/downloads/qb/api/auth/login", data=form, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            cookie = resp.headers.get("Set-Cookie", "")
            sid = ""
            for part in cookie.split(";"):
                if part.strip().startswith("SID="):
                    sid = part.strip()[4:]
            return resp.status, sid
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def _qb_call(base: str, method: str, path: str, sid: str, form: dict | None = None):
    data = urllib.parse.urlencode(form or {}).encode() if form is not None else None
    req = urllib.request.Request(base + "/api/downloads/qb/api" + path, data=data, method=method)
    if sid:
        req.add_header("Cookie", f"SID={sid}")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


def main() -> int:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-downloads-test-"))
    download_dir = Path(tempfile.mkdtemp(prefix="ethos-dl-target-"))
    log_path = data_dir / "server.log"
    env = {
        **os.environ,
        "ETHOS_PORT": str(port),
        "ETHOS_DATA_DIR": str(data_dir),
        "ETHOS_JWT_SECRET": SECRET,
    }
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    admin = _token("admin")
    user = _token("user")

    try:
        if not _wait_ready(base, proc):
            print("Instance failed to start. Log tail:")
            print(log_path.read_text()[-3000:])
            return 1

        print("Auth")
        check("unauthenticated task list → 401", _call(base, "GET", "/api/downloads/tasks")[0] == 401)
        check("unauthenticated summary → 401", _call(base, "GET", "/api/downloads/summary")[0] == 401)
        check("user can list tasks → 200", _call(base, "GET", "/api/downloads/tasks", user)[0] == 200)

        print("\nConfig exposure")
        status, cfg, _h = _call(base, "GET", "/api/downloads/config", admin)
        admin_key = (cfg or {}).get("qb_api_key", "") if isinstance(cfg, dict) else ""
        check("admin GET /config → 200", status == 200)
        check("admin sees the full qB key", admin_key.startswith("ethos_"), str(admin_key)[:16])
        check("admin config reports the key is set", bool((cfg or {}).get("qb_api_key_set")))

        status, ucfg, _h = _call(base, "GET", "/api/downloads/config", user)
        user_key = (ucfg or {}).get("qb_api_key", "") if isinstance(ucfg, dict) else ""
        check("user GET /config → 200", status == 200)
        check("user sees a masked qB key", user_key.startswith("****") and admin_key not in str(ucfg))
        check("user still learns the key exists", bool((ucfg or {}).get("qb_api_key_set")))

        print("\nUser settings + admin-only engine fields")
        status, _body, _h = _call(base, "POST", "/api/downloads/config", user,
                                  {"max_concurrent": 5, "duplicate_policy": "skip"})
        check("user POST /config → 200 (own slice)", status == 200, f"HTTP {status}")
        status, ucfg2, _h = _call(base, "GET", "/api/downloads/config", user)
        check("user cannot change engine settings",
              (ucfg2 or {}).get("max_concurrent") == 3, str(ucfg2)[:120])
        check("user can change their own policy",
              (ucfg2 or {}).get("duplicate_policy") == "skip", str(ucfg2)[:120])
        check("user POST /verify → 403",
              _call(base, "POST", "/api/downloads/verify", user,
                    {"provider": "alldebrid", "key": "x"})[0] == 403)
        check("user rotate key → 403",
              _call(base, "POST", "/api/downloads/config/qb-key/rotate", user)[0] == 403)

        print("\nConfig validation")
        status, cfg2, _h = _call(base, "POST", "/api/downloads/config", admin, {"max_concurrent": 99})
        check("max_concurrent is clamped to 16",
              status == 200 and (cfg2 or {}).get("max_concurrent") == 16, f"HTTP {status}")
        status, body, _h = _call(base, "POST", "/api/downloads/config", admin, {"max_concurrent": "many"})
        check("non-numeric max_concurrent → 400",
              status == 400 and isinstance(body, dict) and "detail" in body, f"HTTP {status}")
        status, body, _h = _call(base, "POST", "/api/downloads/config", admin,
                                 {"download_dir": str(ROOT / "does-not-exist")})
        check("missing download_dir → 400", status == 400, f"HTTP {status}")
        status, cfg3, _h = _call(base, "POST", "/api/downloads/config", admin,
                                 {"download_dir": str(download_dir)})
        check("writable download_dir → 200", status == 200, f"HTTP {status}")

        print("\nSchedule + bandwidth")
        status, cfg4, _h = _call(base, "POST", "/api/downloads/config", admin,
                                 {"schedule": {"enabled": True, "start": "01:00", "end": "06:00"}})
        check("schedule is saved and echoed",
              status == 200 and ((cfg4 or {}).get("schedule") or {}).get("enabled") is True,
              f"HTTP {status}")
        status, body, _h = _call(base, "POST", "/api/downloads/config", admin,
                                 {"schedule": {"start": "25:00"}})
        check("invalid schedule time → 400", status == 400, f"HTTP {status}")
        status, cfg5, _h = _call(base, "POST", "/api/downloads/config", admin,
                                 {"speed_limit": {"global_bps": 1048576, "per_task_bps": 2097152}})
        check("speed limits are saved",
              status == 200 and ((cfg5 or {}).get("speed_limit") or {}).get("per_task_bps") == 2097152,
              f"HTTP {status}")
        # Leave the throwaway instance in a neutral state for later checks.
        _call(base, "POST", "/api/downloads/config", admin,
              {"schedule": {"enabled": False}, "speed_limit": {"global_bps": 0, "per_task_bps": 0}})

        print("\nCategories")
        movie_dir = str(download_dir / "movies")
        status, cfg6, _h = _call(base, "POST", "/api/downloads/config", admin, {"categories": [
            {"name": "movies", "save_path": movie_dir},
            {"name": "MOVIES", "save_path": "/tmp/ignored"},   # duplicate, dropped
            {"name": "tv", "save_path": ""},
        ]})
        cats = (cfg6 or {}).get("categories") if isinstance(cfg6, dict) else None
        check("categories roundtrip → 200", status == 200, f"HTTP {status}")
        check("duplicate category names are dropped",
              isinstance(cats, list) and len(cats) == 2, str(cats))
        check("category save_path is stored absolute",
              isinstance(cats, list) and cats[0].get("save_path") == movie_dir, str(cats))

        print("\nRSS feeds")
        status, cfg7, _h = _call(base, "POST", "/api/downloads/config", admin, {"rss_feeds": [
            {"id": "f1", "url": "http://127.0.0.1:1/rss", "enabled": True, "pattern": "1080p"},
            {"id": "bad", "url": "ftp://example.com/rss", "enabled": True, "pattern": "x"},
            {"id": "f2", "url": "https://example.com/rss", "enabled": False, "pattern": ""},
        ]})
        feeds = (cfg7 or {}).get("rss_feeds") if isinstance(cfg7, dict) else None
        check("non-http RSS URLs are dropped", isinstance(feeds, list) and len(feeds) == 2, str(feeds))
        check("RSS defaults are normalized",
              isinstance(feeds, list) and feeds[0].get("interval_minutes") == 15
              and feeds[0].get("enabled") is True and feeds[1].get("enabled") is False, str(feeds))
        status, listing, _h = _call(base, "GET", "/api/downloads/rss", admin)
        check("GET /rss returns the feeds", status == 200 and len((listing or {}).get("feeds") or []) == 2)
        check("user can read RSS rules", _call(base, "GET", "/api/downloads/rss", user)[0] == 200)
        check("user cannot run a feed → 403",
              _call(base, "POST", "/api/downloads/rss/f1/run", user)[0] == 403)
        check("running an unknown feed → 404",
              _call(base, "POST", "/api/downloads/rss/nope/run", admin)[0] == 404)
        status, run_out, _h = _call(base, "POST", "/api/downloads/rss/f1/run", admin)
        check("run tolerates an unreachable feed",
              status == 200 and (run_out or {}).get("feeds") == 1, f"HTTP {status}: {run_out}")
        _call(base, "POST", "/api/downloads/config", admin, {"rss_feeds": []})

        print("\nPath scope + SSRF")
        status, body, _h = _call(base, "POST", "/api/downloads/tasks", user,
                                 {"url": "https://example.com/f.bin", "folder": "/etc"})
        check("user destination outside /home,/mnt,/media → 403", status == 403, f"HTTP {status}")
        status, body, _h = _call(base, "POST", "/api/downloads/tasks", user,
                                 {"url": "http://127.0.0.1:1/f.bin"})
        check("user loopback URL → 403", status == 403, f"HTTP {status}")

        status, body, _h = _call(base, "POST", "/api/downloads/tasks", admin,
                                 {"url": "http://127.0.0.1:1/f.bin", "folder": str(download_dir)})
        created_id = (body or {}).get("id") if isinstance(body, dict) else None
        check("admin loopback URL is accepted → 200 with id", status == 200 and bool(created_id),
              f"HTTP {status}")
        if created_id:
            _call(base, "DELETE", f"/api/downloads/tasks/{created_id}?delete_file=false", admin)

        status, cat_task, _h = _call(base, "POST", "/api/downloads/tasks", admin,
                                     {"url": "http://127.0.0.1:1/cat.bin", "category": "movies"})
        cat_id = (cat_task or {}).get("id") if isinstance(cat_task, dict) else None
        check("task keeps its category",
              status == 200 and (cat_task or {}).get("category") == "movies", f"HTTP {status}")
        check("category chooses the destination directory",
              str((cat_task or {}).get("path") or "").startswith(movie_dir),
              str((cat_task or {}).get("path")))
        if cat_id:
            _call(base, "DELETE", f"/api/downloads/tasks/{cat_id}?delete_file=false", admin)

        print("\nHTTP transfer + part file")
        serve_dir = tempfile.mkdtemp(prefix="ethos-dl-serve-")
        payload = os.urandom(300_000)
        with open(os.path.join(serve_dir, "payload.bin"), "wb") as fh:
            fh.write(payload)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=serve_dir, **kwargs)

            def log_message(self, *args):
                pass

        httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        status, created, _h = _call(base, "POST", "/api/downloads/tasks", admin,
                                    {"url": f"http://127.0.0.1:{port}/payload.bin",
                                     "folder": str(download_dir)})
        dl_id = (created or {}).get("id") if isinstance(created, dict) else None
        final_path = (created or {}).get("path") if isinstance(created, dict) else ""
        check("HTTP task created", status == 200 and bool(dl_id), f"HTTP {status}")
        state = None
        deadline = time.time() + 20
        while dl_id and time.time() < deadline:
            _s, task, _h = _call(base, "GET", f"/api/downloads/tasks/{dl_id}", admin)
            state = task
            if isinstance(task, dict) and task.get("status") in ("completed", "error"):
                break
            time.sleep(0.2)
        check("HTTP download completes", isinstance(state, dict) and state.get("status") == "completed",
              str((state or {}).get("error") or (state or {}).get("status")))
        check("final file has the served bytes",
              bool(final_path) and os.path.isfile(final_path)
              and os.path.getsize(final_path) == len(payload), str(final_path))
        check("no .part file is left behind", not os.path.exists(final_path + ".part"))
        if dl_id:
            _call(base, "DELETE", f"/api/downloads/tasks/{dl_id}?delete_file=true", admin)
        httpd.shutdown()
        shutil.rmtree(serve_dir, ignore_errors=True)

        print("\nqBittorrent shim")
        status, sid = _qb_login(base, admin_key)
        check("login with the real key → 200 + SID", status == 200 and bool(sid), f"HTTP {status}")
        status, _sid = _qb_login(base, "definitely-wrong")
        check("login with a wrong key → 403", status == 403, f"HTTP {status}")

        status, info = _qb_call(base, "GET", "/torrents/info", sid)
        check("torrents/info → 200 with a torrents array",
              status == 200 and isinstance(info, list),
              f"HTTP {status}")

        status, body = _qb_call(base, "POST", "/torrents/add", sid,
                                {"urls": "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=test"})
        # No debrid provider is configured in the throwaway instance, so the API
        # must answer 400. A 500 here means the old dict→AddTaskRequest crash.
        check("torrents/add with no provider → 400 (not 500)", status == 400,
              f"HTTP {status}: {str(body)[:120]}")

        status, body = _qb_call(base, "POST", "/torrents/add", sid,
                                {"urls": "magnet:?xt=urn:btih:" + "b" * 40, "savePath": "/etc"})
        check("torrents/add savePath outside download_dir → 403", status == 403, f"HTTP {status}")

        print("\nKey rotation")
        status, body, _h = _call(base, "POST", "/api/downloads/config/qb-key/rotate", admin)
        new_key = (body or {}).get("qb_api_key", "") if isinstance(body, dict) else ""
        check("rotate returns a new key", status == 200 and new_key.startswith("ethos_") and new_key != admin_key)
        status, _sid = _qb_login(base, admin_key)
        check("old key no longer logs in", status == 403, f"HTTP {status}")
        status, sid2 = _qb_login(base, new_key)
        check("new key logs in", status == 200 and bool(sid2), f"HTTP {status}")

        print("\nQueue priority")
        status, body, _h = _call(base, "POST", "/api/downloads/tasks", admin,
                                 {"url": "http://127.0.0.1:1/queue.bin", "folder": str(download_dir)})
        queue_id = (body or {}).get("id") if isinstance(body, dict) else None
        check("queue test task created", status == 200 and bool(queue_id), f"HTTP {status}")
        if queue_id:
            status, updated, _h = _call(base, "POST", f"/api/downloads/tasks/{queue_id}/priority",
                                        admin, {"priority": 7})
            check("set task priority → 200 and echoes it",
                  status == 200 and isinstance(updated, dict) and updated.get("priority") == 7,
                  f"HTTP {status}")
            status, _body, _h = _call(base, "POST", "/api/downloads/tasks/reorder",
                                      admin, {"order": [queue_id]})
            check("reorder accepts a known id → 200", status == 200, f"HTTP {status}")
            status, _body, _h = _call(base, "POST", "/api/downloads/tasks/reorder",
                                      admin, {"order": ["dl_missing"]})
            check("reorder with an unknown id → 404", status == 404, f"HTTP {status}")
            status, _body, _h = _call(base, "POST", f"/api/downloads/tasks/{queue_id}/priority",
                                      admin, {"priority": 9999})
            check("priority outside -1000..1000 → 422", status == 422, f"HTTP {status}")
            _call(base, "DELETE", f"/api/downloads/tasks/{queue_id}?delete_file=false", admin)

        os.environ["ETHOS_DATA_DIR"] = str(data_dir)
        sys.path.insert(0, str(ROOT))
        from datetime import datetime  # noqa: E402
        from backend.services.download_schedule import in_window as _in_schedule_window  # noqa: E402
        day = {"schedule": {"enabled": True, "start": "01:00", "end": "06:00"}}
        night = {"schedule": {"enabled": True, "start": "23:00", "end": "02:00"}}
        check("day window open at 02:00", _in_schedule_window(day, datetime(2026, 1, 1, 2, 0)))
        check("day window closed at 12:00", not _in_schedule_window(day, datetime(2026, 1, 1, 12, 0)))
        check("overnight window open at 23:30", _in_schedule_window(night, datetime(2026, 1, 1, 23, 30)))
        check("overnight window open at 01:00", _in_schedule_window(night, datetime(2026, 1, 1, 1, 0)))
        check("overnight window closed at noon", not _in_schedule_window(night, datetime(2026, 1, 1, 12, 0)))
        check("disabled schedule always allows transfers",
              _in_schedule_window({"schedule": {"enabled": False}}, datetime(2026, 1, 1, 12, 0)))

        from backend.routers.downloads import _queue_order  # noqa: E402
        fake = [
            {"id": "a", "status": "waiting", "priority": 0, "created_at": 2},
            {"id": "b", "status": "waiting", "priority": 5, "created_at": 1},
            {"id": "c", "status": "downloading", "priority": 9, "created_at": 0},
        ]
        check("_queue_order sorts by priority then FIFO and skips active tasks",
              [t["id"] for t in _queue_order(fake)] == ["b", "a"])

        print("\nTorrent path containment")
        os.environ["ETHOS_DATA_DIR"] = str(data_dir)
        sys.path.insert(0, str(ROOT))
        from backend.routers.downloads import _safe_join  # noqa: E402
        ok = False
        try:
            _safe_join("/tmp/base", "../etc/passwd")
        except ValueError:
            ok = True
        check("_safe_join rejects ../ escapes", ok)
        ok = False
        try:
            _safe_join("/tmp/base", "/etc/passwd")
        except ValueError:
            ok = True
        check("_safe_join rejects absolute paths", ok)
        check("_safe_join keeps normal relative paths",
              _safe_join("/tmp/base", "Show/Episode.mkv") == "/tmp/base/Show/Episode.mkv")

        print("\nReal-Debrid contract (offline)")
        import backend.services.debrid as debrid  # noqa: E402

        original_http = debrid._http_json

        def fake_status(_url, _method="GET", _headers=None, _data=None, **_kw):
            return {
                "progress": 100,
                "status": "downloaded",
                "filename": "Show",
                "files": [
                    {"id": 1, "path": "/Show/a.mkv", "bytes": 10, "selected": True},
                    {"id": 2, "path": "/Show/b.srt", "bytes": 20, "selected": False},
                    {"id": 3, "path": "/Show/c.mkv", "bytes": 30, "selected": True},
                ],
                # One URL per *selected* file, in files[] order.
                "links": ["https://rd.example/a", "https://rd.example/c"],
            }

        try:
            debrid._http_json = fake_status
            st = debrid._rd_torrent_status("token", 42)
            by_id = {f["id"]: f for f in st["files"]}
            check("RD links are mapped to selected files in order",
                  by_id[1].get("_link_url") == "https://rd.example/a"
                  and by_id[3].get("_link_url") == "https://rd.example/c")
            check("RD does not attach a link to unselected files",
                  "_link_url" not in by_id[2])

            captured = {}

            def fake_select(_url, _method="GET", _headers=None, data=None, **_kw):
                captured["url"] = _url
                captured["body"] = (data or b"").decode()
                return {}

            debrid._http_json = fake_select
            debrid._rd_select_files("token", 42, [1, 2])
            check("RD selectFiles sends files=, not ids=",
                  "files=1%2C2" in captured.get("body", "") and "ids=" not in captured.get("body", ""),
                  captured.get("body", ""))
        finally:
            debrid._http_json = original_http

        failed = [r for r in _results if not r[1]]
        print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
        return 1 if failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(download_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
