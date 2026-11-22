#!/usr/bin/env python3
"""OS package update service — parsing, guards, job state, reboot detection.

Runs in-process with a fake root helper (no apt, no sudo, no network):

    .venv/bin/python tests/test_os_update_service.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-os-update-test-"))
os.environ["ETHOS_DATA_DIR"] = str(DATA_DIR)
os.environ.setdefault("ETHOS_JWT_SECRET", "os-update-service-test-secret")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.services import os_update_service  # noqa: E402

_results: list[tuple[str, bool, str]] = []
HELPERS = DATA_DIR / "helpers"
HELPERS.mkdir(parents=True, exist_ok=True)
CALLS = DATA_DIR / "helper-calls.log"
STATE = DATA_DIR / "os-update"

FAKE_HELPER = """#!/usr/bin/env python3
import json, os, sys, time

calls = __CALLS__
action = sys.argv[1] if len(sys.argv) > 1 else ""
with open(calls, "a", encoding="utf-8") as fh:
    fh.write(action + "\\n")

state = os.path.join(os.environ["ETHOS_DATA_DIR"], "os-update")
os.makedirs(state, exist_ok=True)
if action == "check":
    payload = {
        "checked_at": "2026-11-22T10:00:00+00:00",
        "checked_at_epoch": int(time.time()),
        "count": 2,
        "security_count": 1,
        "kept_back": 0,
        "packages": [
            {"name": "libc6", "from": "1.0", "to": "1.1", "origin": "resolute-security", "security": True},
            {"name": "vim", "from": "9.0", "to": "9.1", "origin": "resolute-updates", "security": False},
        ],
    }
    with open(os.path.join(state, "check.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
"""

GOOD = HELPERS / "ethos-os-update-good"
GOOD.write_text(FAKE_HELPER.replace("__CALLS__", repr(str(CALLS))), encoding="utf-8")
GOOD.chmod(0o755)

BAD = HELPERS / "ethos-os-update-bad"
BAD.write_text("#!/bin/sh\necho 'apt-get update failed: no network' >&2\nexit 4\n", encoding="utf-8")
BAD.chmod(0o755)


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def _calls() -> list[str]:
    try:
        return CALLS.read_text(encoding="utf-8").split()
    except OSError:
        return []


def _write_state(name: str, payload: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o644)


def _expect_error(name: str, coro, status: int, needle: str = "") -> None:
    try:
        asyncio.run(coro)
        check(name, False, "no exception")
    except os_update_service.OsUpdateError as exc:
        ok = exc.status == status and (needle in str(exc) if needle else True)
        check(name, ok, f"status={exc.status} detail={exc}")


def main() -> int:
    settings.OS_UPDATE_ENABLED = True
    settings.OS_UPDATE_HELPER = str(GOOD)
    settings.OS_UPDATE_VIA_SUDO = False
    settings.OS_UPDATE_TTL_SECONDS = 3600
    settings.OS_UPDATE_CHECK_TIMEOUT = 30

    print("[status]")
    initial = os_update_service.status()
    check("helper is detected", initial["helper"] is True)
    check("identity has a kernel", bool(initial["os"]["kernel"]))
    check("no cached result yet", initial["update_count"] == 0 and initial["checked_at"] is None)
    check("no error yet", initial["error"] == "")

    print("\n[apply guard: no check]")
    _expect_error("refuses apply without a cached check", os_update_service.request_apply(),
                  409, "run a check")

    print("\n[check]")
    first = asyncio.run(os_update_service.check(force=True))
    check("reports two upgrades", first["update_count"] == 2, str(first["update_count"]))
    check("reports the security count", first["security_count"] == 1, str(first["security_count"]))
    check("keeps the package rows", len(first["packages"]) == 2)
    check("marks the security pocket", first["packages"][0]["security"] is True)
    check("no error", first["error"] == "")
    check("checked_at is set", bool(first["checked_at"]))
    check("helper ran once", _calls() == ["check"], str(_calls()))

    asyncio.run(os_update_service.check())
    check("cached result does not re-run the helper", _calls() == ["check"], str(_calls()))
    asyncio.run(os_update_service.check(force=True))
    check("force re-runs the helper", _calls() == ["check", "check"], str(_calls()))

    print("\n[fingerprint]")
    left = os_update_service._fingerprint([
        {"name": "vim", "to": "9.1"}, {"name": "libc6", "to": "1.1"}])
    right = os_update_service._fingerprint([
        {"name": "libc6", "to": "1.1"}, {"name": "vim", "to": "9.1"}])
    check("fingerprint ignores order", left == right and len(left) == 12, f"{left} {right}")
    check("empty list has no fingerprint", os_update_service._fingerprint([]) == "")

    print("\n[apply]")
    started = asyncio.run(os_update_service.request_apply())
    check("apply reports updating", started.get("status") == "updating", str(started))
    deadline = time.time() + 5
    while "apply" not in _calls() and time.time() < deadline:
        time.sleep(0.1)
    check("spawned the allowlisted apply action", "apply" in _calls(), str(_calls()))

    print("\n[job state]")
    _write_state("job.json", {"status": "running", "step": "upgrade", "percent": 42,
                              "pid": os.getpid(), "at": "2026-11-22T10:00:00+00:00"})
    live = os_update_service.apply_status()
    check("live helper job counts as running", live["running"] is True and live["stale"] is False)

    _write_state("job.json", {"status": "running", "step": "upgrade", "percent": 42,
                              "pid": 999999, "at": "2026-11-22T10:00:00+00:00"})
    stale = os_update_service.apply_status()
    check("dead helper pid is stale", stale["stale"] is True and stale["running"] is False)
    check("stale job gets a synthesized failure", (stale["result"] or {}).get("status") == "failed")
    stale_again = os_update_service.apply_status()
    check("stale result timestamp is stable (dedupe safe)",
          (stale_again["result"] or {}).get("at") == (stale["result"] or {}).get("at"),
          f"{stale['result']} vs {stale_again['result']}")

    _write_state("job.json", {"status": "ok", "step": "done", "percent": 100,
                              "pid": os.getpid(), "at": "2026-11-22T10:00:00+00:00"})
    _write_state("result.json", {"status": "ok", "upgraded": 2, "at": "2026-11-22T10:00:00+00:00"})
    done = os_update_service.apply_status()
    check("finished job is not running", done["running"] is False)
    check("result is surfaced", (done["result"] or {}).get("upgraded") == 2)

    print("\n[reboot detection]")
    flag = DATA_DIR / "reboot-required"
    pkgs = DATA_DIR / "reboot-required.pkgs"
    flag.write_text("", encoding="utf-8")
    pkgs.write_text("linux-image-generic\nlibc6\nlibc6\n", encoding="utf-8")
    old_flags, old_pkgs = os_update_service.REBOOT_FLAG_PATHS, os_update_service.REBOOT_PKGS_PATH
    os_update_service.REBOOT_FLAG_PATHS = (flag,)
    os_update_service.REBOOT_PKGS_PATH = pkgs
    try:
        reboot = os_update_service.reboot_required()
        check("flag is reported", reboot["required"] is True)
        check("package list is deduped", reboot["packages"] == ["linux-image-generic", "libc6"],
              str(reboot["packages"]))
        flag.unlink()
        check("missing flag means no reboot", os_update_service.reboot_required()["required"] is False)
    finally:
        os_update_service.REBOOT_FLAG_PATHS, os_update_service.REBOOT_PKGS_PATH = old_flags, old_pkgs

    print("\n[bounds]")
    packages = os_update_service._coerce_packages([
        {"name": "x" * 400, "from": "a", "to": "b", "origin": "c", "security": True},
        {"name": ""},
        "not-a-dict",
        {"name": "ok", "security": "yes"},
    ])
    check("drops nameless entries", len(packages) == 2, str(len(packages)))
    check("bounds names", len(packages[0]["name"]) == 120, str(len(packages[0]["name"])))
    check("security must be a bool", packages[1]["security"] is False)

    print("\n[missing helper]")
    settings.OS_UPDATE_HELPER = str(DATA_DIR / "missing-helper")
    check("helper is not detected", os_update_service.helper_available() is False)
    _expect_error("check refuses without the helper", os_update_service.check(force=True),
                  409, "setup_scripts")
    _expect_error("apply refuses without the helper", os_update_service.request_apply(),
                  409, "setup_scripts")

    print("\n[helper failure]")
    settings.OS_UPDATE_HELPER = str(BAD)
    failed = asyncio.run(os_update_service.check(force=True))
    check("apt failure is surfaced", "apt-get update failed" in failed["error"], failed["error"])
    check("previous package list is preserved", failed["update_count"] == 2,
          str(failed["update_count"]))

    print("\n[up-to-date guard]")
    settings.OS_UPDATE_HELPER = str(GOOD)
    _write_state("check.json", {"checked_at": "2026-11-22T10:00:00+00:00",
                                "checked_at_epoch": int(time.time()), "count": 0,
                                "security_count": 0, "kept_back": 0, "packages": []})
    check("status reads the empty list", os_update_service.status()["update_count"] == 0)
    _expect_error("apply refuses when nothing is pending", os_update_service.request_apply(),
                  409, "up to date")

    print("\n[disabled]")
    settings.OS_UPDATE_ENABLED = False
    try:
        disabled = asyncio.run(os_update_service.check(force=True))
        check("disabled check reports the kill switch", "disabled" in disabled["error"].lower())
        _expect_error("disabled apply is refused", os_update_service.request_apply(), 409, "disabled")
    finally:
        settings.OS_UPDATE_ENABLED = True

    failed_results = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed_results)}/{len(_results)} checks passed")
    return 1 if failed_results else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
