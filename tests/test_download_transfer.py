#!/usr/bin/env python3
"""Unit tests for backend/services/download_transfer.py (local HTTP server).

Run:  .venv/bin/python tests/test_download_transfer.py
"""

import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.download_transfer import (  # noqa: E402
    check_disk_space,
    finalize_part,
    part_path,
    resume_source,
    stream_to_file,
)

_failures = 0


def check(name, ok, detail=""):
    global _failures
    if not ok:
        _failures += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    print("download_transfer")
    tmp = tempfile.mkdtemp(prefix="ethos-transfer-test-")
    try:
        # ── Part-file helpers ────────────────────────────────────────────────
        check("part_path appends .part", part_path("/a/b.mkv") == "/a/b.mkv.part")

        dest = os.path.join(tmp, "final.bin")
        with open(part_path(dest), "wb") as fh:
            fh.write(b"abc")
        finalize_part(dest)
        check("finalize moves .part to the final name",
              os.path.exists(dest) and not os.path.exists(part_path(dest)))
        check("resume_source ignores a missing file", resume_source(os.path.join(tmp, "none.bin")) == 0)

        legacy = os.path.join(tmp, "legacy.bin")
        with open(legacy, "wb") as fh:
            fh.write(b"0123456789")
        check("resume_source adopts a legacy partial", resume_source(legacy) == 10)
        check("adoption renames the legacy file to .part",
              os.path.exists(part_path(legacy)) and not os.path.exists(legacy))

        # ── Disk guard ───────────────────────────────────────────────────────
        check("disk guard allows a small task",
              check_disk_space({"path": os.path.join(tmp, "x.bin"), "size": 1}, {"download_dir": tmp}) == "")

        # ── Streaming against a real HTTP server ─────────────────────────────
        payload = os.urandom(200_000)
        served = os.path.join(tmp, "served.bin")
        with open(served, "wb") as fh:
            fh.write(payload)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=tmp, **kwargs)

            def log_message(self, *args):
                pass

        httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]

        target = os.path.join(tmp, "downloaded.bin")
        state = {"dest": target}
        positions = []
        written = stream_to_file(f"http://127.0.0.1:{port}/served.bin", state, 0,
                                 threading.Event(), on_chunk=positions.append)
        httpd.shutdown()

        check("streamer returns the byte count", written == len(payload), str(written))
        check("streamer writes the served bytes",
              os.path.exists(target) and Path(target).read_bytes() == payload)
        check("streamer leaves no .part behind", not os.path.exists(part_path(target)))
        check("on_chunk reports monotonic positions",
              positions and positions[-1] == len(payload)
              and all(b >= a for a, b in zip(positions, positions[1:])))

        # ── Cancellation ─────────────────────────────────────────────────────
        stop = threading.Event()
        stop.set()
        state2 = {"dest": os.path.join(tmp, "cancelled.bin")}
        with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv2:
            port2 = srv2.server_address[1]
            threading.Thread(target=srv2.serve_forever, daemon=True).start()
            started = time.time()
            out = stream_to_file(f"http://127.0.0.1:{port2}/served.bin", state2, 0, stop)
            srv2.shutdown()
        check("a pre-set stop event returns immediately without writing",
              out == 0 and not os.path.exists(state2["dest"]) and time.time() - started < 5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'FAIL' if _failures else 'OK'} — {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
