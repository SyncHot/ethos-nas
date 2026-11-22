#!/usr/bin/env python3
"""Container-health monitor for compose packages (no server, no Docker).

`docker compose ps` is faked: the test drives the parse/aggregate helpers and the
transition logic in `package_health_service.check_once` (one event per change,
none for a persisting condition, one recovery event, state forgets uninstalled
packages).
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ethos-package-health-"))
os.environ["ETHOS_DATA_DIR"] = str(TMP / "data")
os.environ["ETHOS_PACKAGES_DEFAULT_SOURCE"] = "0"
sys.path.insert(0, str(ROOT))

from backend.services import package_health_service as health  # noqa: E402
from backend.services import package_installer  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    # ── Parsing -------------------------------------------------------------
    array = ('[{"Name":"ethos-demo-app-1","Service":"app","State":"running",'
             '"Health":"healthy","ExitCode":0}]')
    rows = package_installer._parse_compose_ps(array)
    check("parses a JSON array", len(rows) == 1 and rows[0]["Name"] == "ethos-demo-app-1", str(rows))
    ndjson = ('{"Name":"a","Service":"app","State":"running"}\n'
              '{"Name":"b","Service":"db","State":"exited","ExitCode":1}\n')
    rows = package_installer._parse_compose_ps(ndjson)
    check("parses one object per line", len(rows) == 2 and rows[1]["Service"] == "db", str(rows))
    check("empty output is empty", package_installer._parse_compose_ps("") == [])

    view = package_installer._container_view(rows[1])
    check("container view keeps exit code", view["exit_code"] == 1 and view["state"] == "exited",
          str(view))

    # ── Aggregation ---------------------------------------------------------
    check("unhealthy wins", package_installer.aggregate_health(
        [{"health": "healthy"}, {"health": "unhealthy"}]) == "unhealthy")
    check("starting beats healthy", package_installer.aggregate_health(
        [{"health": "healthy"}, {"health": "starting"}]) == "starting")
    check("all healthy", package_installer.aggregate_health(
        [{"health": "healthy"}, {"health": "healthy"}]) == "healthy")
    check("no healthchecks is none", package_installer.aggregate_health(
        [{"health": ""}, {"health": ""}]) == "none")
    check("no containers is none", package_installer.aggregate_health([]) == "none")

    # ── Transitions ---------------------------------------------------------
    events = []
    health.emit_event = lambda kind, title, message, **kw: events.append(
        {"kind": kind, "title": title, "message": message, **kw})
    state: dict[str, str] = {}
    health._load_state = lambda: dict(state)

    def _save(new: dict) -> None:
        state.clear()
        state.update(new)

    health._save_state = _save
    health.packages_store.load_installed = lambda: [
        {"id": "demo", "name": "Demo", "runtime": "compose"},
        {"id": "notes", "name": "Notes", "runtime": "files"},
    ]
    current = {"demo": [{"name": "ethos-demo-app-1", "service": "app",
                         "state": "running", "health": "healthy", "exit_code": 0}]}

    async def fake_ps(pkg_id, **_kw):
        return list(current.get(pkg_id, []))

    health.package_installer.compose_ps = fake_ps

    asyncio.run(health.check_once())
    check("healthy containers stay quiet", events == [] and state == {"demo/ethos-demo-app-1": "ok"},
          f"{events} {state}")

    current["demo"] = [{"name": "ethos-demo-app-1", "service": "app",
                        "state": "running", "health": "unhealthy", "exit_code": 0}]
    asyncio.run(health.check_once())
    check("unhealthy emits one warning",
          len(events) == 1 and events[0]["kind"] == "warning"
          and events[0]["dedupe_key"] == "pkg-health:demo/ethos-demo-app-1", str(events))

    asyncio.run(health.check_once())
    check("a persisting condition does not repeat", len(events) == 1, str(events))

    current["demo"] = [{"name": "ethos-demo-app-1", "service": "app",
                        "state": "running", "health": "healthy", "exit_code": 0}]
    asyncio.run(health.check_once())
    check("recovery emits one info event",
          len(events) == 2 and events[1]["kind"] == "info", str(events))
    check("recovery is not a duplicate key",
          events[1]["dedupe_key"].startswith("pkg-health-ok:demo/ethos-demo-app-1:"),
          events[1]["dedupe_key"])

    current["demo"] = [{"name": "ethos-demo-app-1", "service": "app",
                        "state": "restarting", "health": "", "exit_code": 0}]
    asyncio.run(health.check_once())
    check("a restart loop is a problem",
          len(events) == 3 and "restarting" in events[2]["title"], str(events[-1]))

    # A crash-looping container recovers, then the package is uninstalled: the
    # state must forget it so a reinstall starts clean.
    current["demo"] = [{"name": "ethos-demo-app-1", "service": "app",
                        "state": "running", "health": "healthy", "exit_code": 0}]
    asyncio.run(health.check_once())
    health.packages_store.load_installed = lambda: []
    current.clear()
    asyncio.run(health.check_once())
    check("uninstalled packages are forgotten", state == {}, str(state))

    import shutil
    shutil.rmtree(TMP, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
