#!/usr/bin/env python3
"""Memory-pressure guard tests — services/pressure_guard.py.

Run:
    .venv/bin/python tests/test_pressure_guard.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services import pressure_guard  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def snap(level: str = "ok", available: float = 60.0, psi_some: float = 0.0,
         psi_full: float = 0.0) -> dict:
    return {"level": level, "available_pct": available, "available_bytes": int(available * 1e8),
            "total_bytes": 10 ** 10, "used_pct": round(100 - available, 1), "swap_pct": 10.0,
            "psi_some": psi_some, "psi_full": psi_full, "at": 1.0}


def test_classify() -> None:
    check("classify: plenty of memory is ok",
          pressure_guard.classify(60.0, 0.0, 0.0) == pressure_guard.LEVEL_OK)
    check("classify: low available is soft",
          pressure_guard.classify(5.0, 0.0, 0.0) == pressure_guard.LEVEL_SOFT)
    check("classify: critically low available is hard",
          pressure_guard.classify(2.0, 0.0, 0.0) == pressure_guard.LEVEL_HARD)
    check("classify: PSI some above the soft threshold is soft",
          pressure_guard.classify(60.0, 40.0, 0.0) == pressure_guard.LEVEL_SOFT)
    check("classify: PSI full above the hard threshold is hard",
          pressure_guard.classify(60.0, 0.0, 15.0) == pressure_guard.LEVEL_HARD)


def test_read_psi() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        psi = Path(tmp) / "memory"
        psi.write_text("some avg10=12.50 avg60=3.00 avg300=1.00 total=1\n"
                       "full avg10=7.25 avg60=0.50 avg300=0.10 total=1\n",
                       encoding="utf-8")
        some, full = pressure_guard.read_psi(str(psi))
        check("read_psi parses some/full", (some, full) == (12.5, 7.25), f"{some}/{full}")
        missing = Path(tmp) / "nope"
        check("read_psi degrades when PSI is absent",
              pressure_guard.read_psi(str(missing)) == (0.0, 0.0))


async def test_transitions() -> None:
    events: list[tuple[str, str]] = []
    original_collect = pressure_guard._collect
    original_emit = pressure_guard.emit_event
    try:
        pressure_guard.reset()
        pressure_guard.emit_event = lambda kind, title, message, **kw: events.append((kind, title))

        pressure_guard._collect = lambda: snap("ok", 50.0)
        pressure_guard.refresh(force=True)
        check("ok level allows heavy work", pressure_guard.heavy_work_allowed())
        check("no event on the first ok sample", events == [], str(events))

        pressure_guard._collect = lambda: snap("hard", 3.0, 30.0, 20.0)
        pressure_guard.refresh(force=True)
        check("hard level blocks heavy work", not pressure_guard.heavy_work_allowed())
        check("hard level emits one warning",
              events == [("warning", "Low memory — heavy tasks paused")], str(events))

        pressure_guard._collect = lambda: snap("hard", 3.0, 30.0, 20.0)
        pressure_guard.refresh(force=True)
        check("a persisting hard level does not re-notify", len(events) == 1, str(events))

        pressure_guard._collect = lambda: snap("ok", 40.0)
        pressure_guard.refresh(force=True)
        check("recovery emits an info event",
              events[-1] == ("info", "Memory pressure cleared"), str(events))

        pressure_guard._collect = lambda: snap("soft", 6.0, 30.0)
        pressure_guard.refresh(force=True)
        check("soft level is logged only, no notification", len(events) == 2, str(events))
        check("soft level still allows heavy work", pressure_guard.heavy_work_allowed())

        pressure_guard._collect = lambda: snap("hard", 3.0, 30.0, 20.0)
        pressure_guard.refresh(force=True)
        start = asyncio.get_running_loop().time()
        ok = await pressure_guard.wait_until_ok(timeout=0.2)
        waited = asyncio.get_running_loop().time() - start
        check("wait_until_ok returns False when pressure persists", ok is False)
        check("wait_until_ok honours the timeout", 0.1 <= waited < 3.0, f"{waited:.2f}s")

        pressure_guard._collect = lambda: snap("ok", 50.0)
        woke = asyncio.ensure_future(pressure_guard.wait_until_ok(timeout=5))
        await asyncio.sleep(0)
        pressure_guard.refresh(force=True)
        check("wait_until_ok wakes when pressure clears", await woke is True)

        info = pressure_guard.snapshot()
        for key in ("level", "available_pct", "swap_pct", "psi_some", "psi_full", "at"):
            check(f"snapshot exposes {key}", key in info)
    finally:
        pressure_guard._collect = original_collect
        pressure_guard.emit_event = original_emit
        pressure_guard.reset()


def main() -> int:
    test_classify()
    test_read_psi()
    asyncio.run(test_transitions())

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
