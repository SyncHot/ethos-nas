#!/usr/bin/env python3
"""EthOS NAS — test runner.

Starts ONE isolated instance (free port, throwaway ETHOS_DATA_DIR, no shared
state) and runs the standalone test scripts in `tests/` against it, then tears
everything down. See TODO.md P4-01.

Usage:
    .venv/bin/python run_tests.py                 # run everything
    .venv/bin/python run_tests.py --list          # show what would run
    .venv/bin/python run_tests.py --filter music  # substring match on filename
    .venv/bin/python run_tests.py --keep-data     # keep the temp data dir
    .venv/bin/python run_tests.py --timeout 600   # per-test timeout (seconds)

Environment:
    ETHOS_TEST_PASSWORD   required by most tests (never hardcoded in the repo)
    ETHOS_TEST_BASE       if set, the runner does NOT start a server and targets
                          this URL instead
    ETHOS_TEST_PORT       fixed port for the shared instance (default: free port)

Tests that boot their own instance (e.g. test_security_invariants) ignore
ETHOS_TEST_BASE and are unaffected.
"""

import argparse
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

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"
PY = sys.executable


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _discover(filters: list[str]) -> list[Path]:
    files = sorted(TESTS.glob("test_*.py"))
    if filters:
        files = [f for f in files if any(flt in f.name for flt in filters)]
    return files


def _upstream(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/api/system/status", timeout=2):
            return True
    except urllib.error.HTTPError as exc:
        return exc.code == 401  # server is up, auth required
    except OSError:
        return False


def _wait_up(base: str, proc: subprocess.Popen, deadline: float) -> bool:
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if _upstream(base):
            return True
        time.sleep(0.3)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the EthOS NAS test suite.")
    ap.add_argument("--list", action="store_true", help="list tests and exit")
    ap.add_argument("--filter", action="append", default=[], metavar="SUBSTR",
                    help="only run files whose name contains SUBSTR (repeatable)")
    ap.add_argument("--keep-data", action="store_true", help="keep the temp DATA_DIR")
    ap.add_argument("--timeout", type=int, default=900, help="per-test timeout in seconds")
    args = ap.parse_args()

    files = _discover(args.filter)
    if not files:
        print("No matching tests in tests/.")
        return 1
    if args.list:
        for f in files:
            print(f.relative_to(ROOT))
        return 0

    if not os.environ.get("ETHOS_TEST_PASSWORD"):
        print("WARNING: ETHOS_TEST_PASSWORD is not set — tests that log in will fail.")

    # Tests must never depend on the public package catalog.
    os.environ.setdefault("ETHOS_PACKAGES_DEFAULT_SOURCE", "0")

    external = os.environ.get("ETHOS_TEST_BASE", "").strip()
    proc = None
    data_dir = None
    base = external

    if external:
        if not _upstream(external):
            print(f"ETHOS_TEST_BASE={external} is not answering /api/system/status")
            return 1
        print(f"Using existing instance at {external}")
    else:
        port = int(os.environ.get("ETHOS_TEST_PORT") or _free_port())
        data_dir = Path(tempfile.mkdtemp(prefix="ethos-tests-"))
        production = (ROOT / "backend" / "data").resolve()
        if data_dir.resolve() == production:  # defensive: never touch live state
            print("Refusing to run against the production data directory.")
            return 1
        base = f"http://127.0.0.1:{port}"
        log_path = data_dir / "server.log"
        env = {
            **os.environ,
            "ETHOS_PORT": str(port),
            "ETHOS_DATA_DIR": str(data_dir),
        }
        log = open(log_path, "w")
        proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "backend.main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        print(f"Started isolated instance at {base} (data: {data_dir})")
        if not _wait_up(base, proc, time.time() + 40):
            print("Instance failed to start. Log tail:")
            print(log_path.read_text()[-3000:])
            proc.terminate()
            return 1

    env = {**os.environ, "ETHOS_TEST_BASE": base}
    results: list[tuple[str, bool, float]] = []
    try:
        for path in files:
            name = path.relative_to(ROOT)
            print(f"\n=== {name} ===", flush=True)
            started = time.time()
            try:
                rc = subprocess.run(
                    [PY, str(path)], cwd=str(ROOT), env=env,
                    timeout=args.timeout,
                ).returncode
                ok = rc == 0
            except subprocess.TimeoutExpired:
                print(f"TIMEOUT after {args.timeout}s")
                ok = False
            results.append((str(name), ok, time.time() - started))
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        if data_dir is not None and not args.keep_data:
            shutil.rmtree(data_dir, ignore_errors=True)

    passed = [r for r in results if r[1]]
    failed = [r for r in results if not r[1]]
    print("\n" + "=" * 60)
    print(f"{len(passed)}/{len(results)} test files passed")
    for name, ok, secs in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}  ({secs:.0f}s)")
    if failed:
        print("\nFailed:")
        for name, _ok, _s in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
