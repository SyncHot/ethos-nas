#!/usr/bin/env python3
"""Graceful shutdown tests — services/shutdown_state.py and the unit contract.

Regression for 2026-11-22: a core-update restart was SIGKILLed after systemd's
default 90 s ``TimeoutStopSec`` because uvicorn never finished draining open
streams, the package supervisor respawned children systemd had just signalled
and the render queue started a fresh ffmpeg mid-shutdown.

Run:
    .venv/bin/python tests/test_graceful_shutdown.py
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services import package_runtime, shutdown_state, video_render  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def test_unit_contract() -> None:
    text = (ROOT / "deploy" / "ethos-nas.service").read_text(encoding="utf-8")
    check("unit stops with KillMode=mixed", "KillMode=mixed" in text)
    check("unit bounds the stop with TimeoutStopSec", "TimeoutStopSec=" in text)
    check("uvicorn drain is bounded", "--timeout-graceful-shutdown" in text)

    provision = json.loads((ROOT / "deploy" / "provision.json").read_text(encoding="utf-8"))
    sources = json.dumps(provision)
    check("provisioning ships the unit on update", "deploy/ethos-nas.service" in sources)


def test_flag() -> None:
    check("flag starts clear", not shutdown_state.is_set())
    shutdown_state.request()
    check("request() sets the flag", shutdown_state.is_set())
    shutdown_state.request()
    check("request() is idempotent", shutdown_state.is_set())
    shutdown_state.reset()
    check("reset() clears the flag (tests only)", not shutdown_state.is_set())


def test_stream_guards() -> None:
    for rel in ("backend/main.py", "backend/services/package_runtime.py",
                "backend/services/video_render.py", "backend/routers/music.py",
                "backend/routers/surveillance.py", "backend/routers/video_station.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        check(f"{rel} honours shutdown_state", "shutdown_state" in text)

    lifespan = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    yield_at = lifespan.index("    yield\n")
    teardown = lifespan[yield_at:]
    check("lifespan sets the flag before stopping children",
          teardown.index("_shutdown_state.request()") < teardown.index("shutdown_all()"),
          "request() must come first")


class _FakeProc:
    def __init__(self, code: int = -15) -> None:
        self.returncode = code

    async def wait(self) -> int:
        return self.returncode


async def test_supervisor() -> None:
    handle = package_runtime._Handle(
        package_id="pkg-shutdown-test", process=_FakeProc(), port=1)
    package_runtime._HANDLES["pkg-shutdown-test"] = handle
    shutdown_state.request()
    try:
        await asyncio.wait_for(package_runtime._supervise("pkg-shutdown-test"), timeout=5)
        check("shutdown: supervisor does not respawn the exited child",
              package_runtime._HANDLES.get("pkg-shutdown-test") is handle)
    finally:
        package_runtime._HANDLES.pop("pkg-shutdown-test", None)
        shutdown_state.reset()

    # Positive control: without the flag the supervisor does try to restart.
    started: list[str] = []
    orig_start = package_runtime.start
    orig_get = package_runtime.packages_store.get_package

    async def fake_start(pkg_id, record=None, job_id=None, *, supervise=True):
        started.append(pkg_id)
        return {"package": pkg_id, "status": "running", "port": 1}

    package_runtime.start = fake_start
    package_runtime.packages_store.get_package = lambda pkg_id: {
        "id": pkg_id, "name": "Shutdown test", "runtime": "venv"}
    handle = package_runtime._Handle(package_id="pkg-shutdown-test", process=_FakeProc(), port=1)
    package_runtime._HANDLES["pkg-shutdown-test"] = handle
    try:
        await asyncio.wait_for(package_runtime._supervise("pkg-shutdown-test"), timeout=10)
        check("no flag: supervisor restarts the child", started == ["pkg-shutdown-test"],
              str(started))
    finally:
        package_runtime._HANDLES.pop("pkg-shutdown-test", None)
        package_runtime.start = orig_start
        package_runtime.packages_store.get_package = orig_get


async def test_render_queue() -> None:
    base = Path(tempfile.mkdtemp(prefix="ethos-shutdown-"))
    mgr = video_render.RenderManager(base_dir=base)
    job = video_render.RenderJob(
        id="a" * 32, owner="tester", path="/tmp/movie.mkv", quality="tiny",
        audio_idx=0, source_key="key")
    mgr._jobs[job.id] = job

    shutdown_state.request()
    try:
        try:
            await mgr.start(owner="tester", abs_path="/tmp/movie.mkv", info={},
                            quality="tiny", audio_idx=0, source_key="key2",
                            cmd_factory=lambda out: ["true"])
            refused = False
        except video_render.RenderBusy:
            refused = True
        check("shutdown: new renders are refused", refused)

        calls: list[str] = []

        def factory(out: str) -> list[str]:
            calls.append(out)
            return ["true"]

        try:
            await mgr._run(job, "/tmp/movie.mkv", {}, factory)
        except asyncio.CancelledError:
            pass
        check("shutdown: queued job aborts before spawning ffmpeg", calls == [], str(calls))
        check("shutdown: aborted job is cancelled, not failed", job.status == "cancelled",
              job.status)
    finally:
        shutdown_state.reset()

    await mgr.stop_all()
    check("stop_all keeps queued work out of 'failed'", job.status in ("cancelled", "consumed"),
          job.status)


def main() -> int:
    test_unit_contract()
    test_flag()
    test_stream_guards()
    asyncio.run(test_supervisor())
    asyncio.run(test_render_queue())

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
