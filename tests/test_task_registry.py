#!/usr/bin/env python3
"""Task registry tests — services/task_registry.py (TODO.md P1-06).

Run:
    .venv/bin/python tests/test_task_registry.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services import task_registry  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    check("spawn outside a loop returns None", task_registry.spawn(asyncio.sleep(0)) is None)

    async def scenario() -> None:
        done: list[str] = []

        async def quick() -> None:
            done.append("quick")

        async def boom() -> None:
            raise RuntimeError("expected test failure")

        async def forever() -> None:
            await asyncio.sleep(3600)

        t1 = task_registry.spawn(quick(), name="quick")
        t2 = task_registry.spawn(boom(), name="boom")
        t3 = task_registry.spawn(forever(), name="forever")
        check("spawn returns a task", all(t is not None for t in (t1, t2, t3)))
        check("pending counts live tasks", task_registry.pending() >= 2, str(task_registry.pending()))

        await asyncio.sleep(0.05)
        check("quick task ran", done == ["quick"], str(done))
        check("failing task finished", t2.done())
        check("finished tasks leave the registry", task_registry.pending() == 1,
              str(task_registry.pending()))

        cancelled = await task_registry.shutdown(timeout=2)
        check("shutdown cancels the remaining task", cancelled == 1, str(cancelled))
        check("cancelled task is done", t3.done() and t3.cancelled())
        check("registry is empty after shutdown", task_registry.pending() == 0)

        # A second shutdown on an empty registry is a no-op.
        check("idle shutdown returns 0", await task_registry.shutdown(timeout=1) == 0)

    asyncio.run(scenario())

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        for name, _ok, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
