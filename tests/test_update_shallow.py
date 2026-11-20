#!/usr/bin/env python3
"""Update check on an appliance built from an OS image.

The image clones EthOS with `--depth 1`. Two things broke because of that:

  * the updater could not fetch the signed commit (fixed in
    `deploy/ethos-update`, guarded by test_update_helper.py), and
  * `git rev-list --count HEAD` reported 1, so `current_release()` claimed the
    appliance was build 1 while the signed index said build 67 — the Settings
    card advertised an update forever, even immediately after updating.

This checks the second: a shallow checkout must not report a build number, so
`is_newer` compares the commit sha pinned by the signed index instead.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

os.environ.setdefault("ETHOS_DATA_DIR", tempfile.mkdtemp(prefix="ethos-update-shallow-"))

from backend.services import update_service  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


CALLS = []


def fake_git(shallow: bool, sha: str = "2d45dff"):
    def _git(*args):
        CALLS.append(args)
        if args[:1] == ("rev-parse",) and "--is-shallow-repository" in args:
            return "true" if shallow else "false"
        if args[:1] == ("rev-list",):
            # A shallow clone answers with the few commits it has, not history.
            return "1"
        if args[:1] == ("rev-parse",) and "--short" in args:
            return sha
        return ""

    return _git


original = update_service._git
try:
    print("full clone")
    update_service._git = fake_git(shallow=False)
    current = update_service.current_release()
    check("full clone keeps the commit count", current["build"] == 1, str(current))
    check("full clone is not flagged shallow", current["shallow"] is False, str(current))

    print("shallow clone (OS image)")
    update_service._git = fake_git(shallow=True)
    current = update_service.current_release()
    check("shallow clone reports no build number", current["build"] == 0, str(current))
    check("shallow clone is flagged", current["shallow"] is True, str(current))

    release = {"version": "2.6.2", "git_sha": "2d45dff", "build": 67}
    check("no phantom update when the shas match",
          update_service.is_newer(release, current) is False,
          f"{release} vs {current}")

    behind = {"version": "2.5.0", "git_sha": "918444a", "build": 61}
    check("a real update is still offered", update_service.is_newer(behind, current) is True,
          f"{behind} vs {current}")

    print("tarball install (no git)")
    update_service._git = lambda *args: ""
    current = update_service.current_release()
    check("no git means no build number", current["build"] == 0, str(current))
    check("no git still compares versions",
          update_service.is_newer({"version": "9.9.9"}, current) is True, str(current))
finally:
    update_service._git = original

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
