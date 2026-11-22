#!/usr/bin/env python3
"""Download URL/path helpers — hoster detection, validation and escapes (no server).

Extracted from `routers/downloads.py` in P2-01.

Run:  .venv/bin/python tests/test_download_urls.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import download_urls as du  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    print("premium hosters")
    check("exact host", du.is_file_host("https://rapidgator.net/file/x"))
    check("subdomain", du.is_file_host("https://cdn.rapidgator.net/x"))
    check("port and case", du.is_file_host("HTTPS://MEGA.NZ:8080/file"))
    check("unknown host", not du.is_file_host("https://example.com/file"))
    check("malformed url", not du.is_file_host("not a url ::"))

    print("name from url")
    check("path basename", du.name_from_url("https://x.test/a/b/file.mkv?x=1#y") == "file.mkv")
    check("trailing slash fallback", du.name_from_url("https://x.test/dir/") == "download.bin")
    check("empty url fallback", du.name_from_url("") == "download.bin")

    print("url validation")
    for bad in ("", "   ", "ftp://x/file", "https://", "magnet:?xt=urn:btih:x"):
        try:
            du.validate_url(bad)
            check(f"rejects {bad!r}", False, "accepted")
        except ValueError:
            check(f"rejects {bad!r}", True)
    check("trims a valid url", du.validate_url("  https://x.test/a  ") == "https://x.test/a")

    print("private targets")
    for blocked in ("127.0.0.1", "10.1.2.3", "192.168.1.10", "169.254.1.1", "::1", "fd00::1", "0.0.0.0"):
        check(f"blocks {blocked}", du.is_blocked_ip(blocked))
    check("allows public v4", not du.is_blocked_ip("8.8.8.8"))
    check("allows public v6", not du.is_blocked_ip("2001:4860:4860::8888"))
    check("ignores non-IP text", not du.is_blocked_ip("example.com"))

    print("safe join")
    tmp = Path(tempfile.mkdtemp(prefix="ethos-urls-test-"))
    try:
        base = tmp / "task"
        base.mkdir()
        check("normal child", du.safe_join(str(base), "sub", "f.bin") == str(base / "sub" / "f.bin"))
        try:
            # No parts means "the base itself" — the symlink guard refuses it.
            du.safe_join(str(base))
            check("no parts is rejected like before", False, "accepted")
        except ValueError:
            check("no parts is rejected like before", True)
        for escape in (("..", "etc", "passwd"), ("/etc/passwd",)):
            try:
                du.safe_join(str(base), *escape)
                check(f"rejects {escape}", False, "accepted")
            except ValueError:
                check(f"rejects {escape}", True)
        (tmp / "outside").mkdir()
        os.symlink(tmp / "outside", base / "link")
        try:
            du.safe_join(str(base), "link", "x.bin")
            check("rejects symlink escape", False, "accepted")
        except ValueError:
            check("rejects symlink escape", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("unique destination")
    tmp2 = Path(tempfile.mkdtemp(prefix="ethos-urls-dest-"))
    try:
        first = du.unique_dest(str(tmp2), "file.bin")
        check("free name unchanged", first.endswith("file.bin"))
        Path(first).write_text("x")
        second = du.unique_dest(str(tmp2), "file.bin")
        check("collision gets a suffix", second.endswith("file (1).bin"), second)
        Path(second).write_text("x")
        third = du.unique_dest(str(tmp2), "file.bin")
        check("second collision increments", third.endswith("file (2).bin"), third)
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1
    print("All download URL checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
