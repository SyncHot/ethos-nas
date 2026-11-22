#!/usr/bin/env python3
"""Hoster-link provider fallback (offline).

When the hoster-link provider in `debrid.link_provider` cannot unlock a URL,
the download worker retries with every other configured link provider that
supports the host — e.g. tb7.pl fails on a RapidGator page but the stored
AllDebrid account can still fetch it. This also covers a task created before
the fix (its provider is no longer usable), so a reset can recover it.

The provider calls themselves are stubbed and the resolved link is served by a
local HTTP server, so no network is used.

Run:  .venv/bin/python tests/test_download_link_fallback.py
"""

import http.server
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before backend.config is imported.
_DATA = tempfile.mkdtemp(prefix="ethos-link-fallback-")
os.environ.setdefault("ETHOS_DATA_DIR", _DATA)
os.environ.setdefault("ETHOS_JWT_SECRET", "test-secret")

import backend.routers.downloads as downloads  # noqa: E402
import backend.services.download_config as download_config  # noqa: E402
import backend.services.download_providers as providers  # noqa: E402
from backend.services import path_scope  # noqa: E402

_failures = 0
FILE_BYTES = b"fallback-payload-" * 512
_RAPIDGATOR_URL = (
    "https://rapidgator.net/file/01c2f90394aaa63e0d40aa05e7d1a71b/"
    "Perfect.Liars.2026.1080p.WEB.h264-GRACE.mkv.html"
)


def check(name: str, ok: bool, detail: str = "") -> None:
    global _failures
    if not ok:
        _failures += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _FileHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(FILE_BYTES)))
        self.end_headers()
        self.wfile.write(FILE_BYTES)


def _task(download_dir: str, provider: str) -> dict:
    return {
        "id": "dl_fallback",
        "url": _RAPIDGATOR_URL,
        "name": "Perfect.Liars.2026.1080p.WEB.h264-GRACE.mkv.html",
        "path": download_dir,
        "kind": "debrid_link",
        "provider": provider,
        "_debrid_key": "tb7-pw",
        "_debrid_id": 0,
        "_debrid_status": "",
        "_debrid_progress": 0,
        "_files": [],
        "status": "downloading",
        "size": 0,
        "downloaded": 0,
        "speed": 0,
        "error": "",
        "created_at": 1,
        "started_at": 1,
        "finished_at": 0,
    }


def main() -> int:
    port = _free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _FileHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    download_dir = tempfile.mkdtemp(prefix="ethos-link-fallback-dl-")

    original_hosters = providers.hosters
    original_resolve = providers.resolve_link
    calls: list[str] = []
    try:
        # No network: tb7 lists RapidGator, AllDebrid accepts any host.
        providers.hosters = lambda pid: ["rapidgator.net"] if pid == "tb7" else []

        def fake_resolve(provider, creds, url, stop=None):
            calls.append(provider)
            if provider == "tb7":
                raise RuntimeError("tb7.pl reports the link as inactive or removed")
            return {
                "url": f"http://127.0.0.1:{port}/file.bin",
                "name": "Perfect.Liars.2026.1080p.WEB.h264-GRACE.mkv",
                "size": len(FILE_BYTES),
                "headers": {},
            }

        providers.resolve_link = fake_resolve

        with path_scope.system_scope():
            download_config.save_config({
                "download_dir": download_dir,
                "debrid": {
                    "provider": "",
                    "link_provider": "tb7",
                    "tb7_username": "fallback-user",
                    "tb7_password": "tb7-pw",
                    "alldebrid_api_key": "ADKEY-1234567890",
                },
            })

        print("Candidate order")
        cfg = download_config.load_config()
        order = [pid for pid, _c in providers.link_candidates(cfg, _RAPIDGATOR_URL)]
        check("primary slot is tried first", order == ["tb7", "alldebrid"], str(order))

        print("\nWorker falls back to AllDebrid")
        task = _task(download_dir, "tb7")
        downloads._debrid_worker(task, threading.Event())
        check("tb7 failed, then AllDebrid unlocked", calls == ["tb7", "alldebrid"], str(calls))
        check("task now records the provider that worked", task.get("provider") == "alldebrid",
              str(task.get("provider")))
        check("provider file name replaced the URL name",
              task.get("name") == "Perfect.Liars.2026.1080p.WEB.h264-GRACE.mkv", str(task.get("name")))
        dest = Path(download_dir) / task["name"]
        check("file downloaded through the fallback", dest.is_file()
              and dest.read_bytes() == FILE_BYTES, str(dest))
        check("the task is not in an error state", not task.get("error"), str(task.get("error")))

        print("\nLegacy task whose provider is no longer configured")
        calls.clear()
        with path_scope.system_scope():
            download_config.save_config({
                "debrid": {"link_provider": "alldebrid", "tb7_username": "", "tb7_password": "",
                           "alldebrid_api_key": "ADKEY-1234567890"},
            })
        legacy = _task(download_dir, "tb7")
        legacy["name"] = "legacy.mkv.html"
        downloads._debrid_worker(legacy, threading.Event())
        check("skips the unconfigured provider", calls == ["alldebrid"], str(calls))
        check("legacy task recovers via AllDebrid", legacy.get("provider") == "alldebrid",
              str(legacy.get("provider")))

        print("\nNo configured provider can unlock the host")
        calls.clear()
        with path_scope.system_scope():
            download_config.save_config({
                "debrid": {"link_provider": "", "alldebrid_api_key": ""},
            })
        orphan = _task(download_dir, "tb7")
        try:
            downloads._debrid_worker(orphan, threading.Event())
        except RuntimeError as exc:
            check("a clear error is raised", "link provider" in str(exc), str(exc))
        else:
            check("a clear error is raised", False)
        check("nothing was attempted", calls == [], str(calls))

        print("\nStop event is honoured")
        calls.clear()
        with path_scope.system_scope():
            download_config.save_config({
                "debrid": {"link_provider": "tb7", "tb7_username": "u", "tb7_password": "p",
                           "alldebrid_api_key": "ADKEY-1234567890"},
            })

        def stopping_resolve(provider, creds, url, stop=None):
            calls.append(provider)
            if provider == "tb7":
                raise RuntimeError("tb7.pl is unreachable")
            return {"url": "", "name": "", "size": 0, "headers": {}}

        providers.resolve_link = stopping_resolve
        stopped = threading.Event()
        stopped.set()
        paused = _task(download_dir, "tb7")
        downloads._debrid_worker(paused, stopped)
        check("no further provider is tried after a stop", calls == ["tb7"], str(calls))
        check("the task keeps its state", paused.get("provider") == "tb7", str(paused.get("provider")))
    finally:
        providers.hosters = original_hosters
        providers.resolve_link = original_resolve
        server.shutdown()
        server.server_close()

    print(f"\n{'FAIL' if _failures else 'OK'} — {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
