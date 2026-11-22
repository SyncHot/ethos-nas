#!/usr/bin/env python3
"""Downloads queue/retry scheduling helpers (no server).

Extracted from `routers/downloads.py` in P2-01.

Run:  .venv/bin/python tests/test_download_engine_queue.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import download_engine  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    print("queue order")
    tasks = [
        {"id": "a", "status": "waiting", "priority": 0, "created_at": 20},
        {"id": "b", "status": "waiting", "priority": 5, "created_at": 30},
        {"id": "c", "status": "waiting", "priority": 0, "created_at": 10},
        {"id": "d", "status": "waiting", "priority": -2, "created_at": 1},
        {"id": "e", "status": "downloading", "priority": 99, "created_at": 2},
        {"id": "f", "status": "completed", "priority": 50, "created_at": 3},
    ]
    order = [task["id"] for task in download_engine.queue_order(tasks)]
    check("priority desc, then FIFO", order == ["b", "c", "a", "d"], str(order))
    check("non-waiting tasks excluded", "e" not in order and "f" not in order)
    check("missing priority is zero",
          [t["id"] for t in download_engine.queue_order([
              {"id": "x", "status": "waiting", "created_at": 2},
              {"id": "y", "status": "waiting", "priority": 1, "created_at": 1},
          ])] == ["y", "x"])
    check("custom status filter",
          [t["id"] for t in download_engine.queue_order(tasks, status="downloading")] == ["e"])
    check("empty input", download_engine.queue_order([]) == [])

    print("next retry delay")
    now = 1_000.0
    waiting = [
        {"status": "waiting", "_next_retry_at": now + 60},
        {"status": "waiting", "_next_retry_at": now + 30},
        {"status": "downloading", "_next_retry_at": now + 5},
        {"status": "waiting", "_next_retry_at": now - 10},
        {"status": "waiting"},
    ]
    check("earliest future retry wins", download_engine.next_retry_delay(waiting, now) == 30.0,
          str(download_engine.next_retry_delay(waiting, now)))
    check("nothing waiting returns None",
          download_engine.next_retry_delay([{"status": "completed"}], now) is None)
    check("only due retries returns None",
          download_engine.next_retry_delay([{"status": "waiting", "_next_retry_at": now - 1}], now) is None)
    check("minimum delay is 1 s",
          download_engine.next_retry_delay([{"status": "waiting", "_next_retry_at": now + 0.2}], now) == 1.0)
    check("custom minimum",
          download_engine.next_retry_delay([{"status": "waiting", "_next_retry_at": now + 0.2}], now, minimum=2.5) == 2.5)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1
    print("All download engine queue checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
