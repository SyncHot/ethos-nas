#!/usr/bin/env python3
"""Unit tests — cron schedule parsing, validation and next-run preview.

Pure logic, no server, no data dir: `backend/services/cron_schedule.py`.

Run:  .venv/bin/python tests/test_cron_schedule.py
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services import cron_schedule  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="minutes")


def main() -> int:
    print("cron schedule — parsing")
    for expr in ("* * * * *", "*/15 * * * *", "0 2 * * 1-5", "0 0 1 jan sun", "@daily", "@reboot"):
        try:
            cron_schedule.validate(expr)
            check(f"valid: {expr}", True)
        except ValueError as exc:
            check(f"valid: {expr}", False, str(exc))

    for expr in ("", "0 2 * *", "61 * * * *", "0 25 * * *", "*/0 * * * *",
                 "0 2 * * 9", "0 0 0 * *", "a b c d e", "0 2 * * 1-0"):
        try:
            cron_schedule.validate(expr)
            check(f"invalid: {expr!r}", False, "accepted")
        except ValueError:
            check(f"invalid: {expr!r}", True)

    print("cron schedule — next runs")
    runs = cron_schedule.next_runs("*/15 * * * *", 2, datetime(2026, 1, 1, 10, 7))
    check("step minutes", [_iso(run) for run in runs] == ["2026-01-01T10:15", "2026-01-01T10:30"],
          str([_iso(run) for run in runs]))

    runs = cron_schedule.next_runs("@daily", 1, datetime(2026, 1, 1, 10, 7))
    check("daily macro", [_iso(run) for run in runs] == ["2026-01-02T00:00"],
          str([_iso(run) for run in runs]))

    runs = cron_schedule.next_runs("0 2 * * 1", 1, datetime(2026, 1, 1, 12, 0))
    check("weekly Monday", [_iso(run) for run in runs] == ["2026-01-05T02:00"],
          str([_iso(run) for run in runs]))

    runs = cron_schedule.next_runs("0 0 * jan sun", 1, datetime(2026, 1, 1, 0, 1))
    check("month + weekday names", [_iso(run) for run in runs] == ["2026-01-04T00:00"],
          str([_iso(run) for run in runs]))

    # Vixie cron: day-of-month OR day-of-week when both are restricted.
    runs = cron_schedule.next_runs("0 0 13 * 5", 1, datetime(2026, 1, 1, 0, 1))
    check("dom OR dow", [_iso(run) for run in runs] == ["2026-01-02T00:00"],
          str([_iso(run) for run in runs]))

    check("@reboot has no future run", cron_schedule.next_runs("@reboot", 3) == [])
    check("unparseable has no future run", cron_schedule.next_runs("nonsense", 3) == [])

    print("cron schedule — description")
    check("every day", cron_schedule.describe("0 2 * * *") == "every day at 02:00",
          cron_schedule.describe("0 2 * * *"))
    check("weekday", cron_schedule.describe("30 6 * * 1") == "every Mon at 06:30",
          cron_schedule.describe("30 6 * * 1"))
    check("macro", cron_schedule.describe("@weekly") == "every Sunday at 00:00",
          cron_schedule.describe("@weekly"))
    check("fallback keeps the expression", cron_schedule.describe("5 4 3 2 1") == "5 4 3 2 1",
          cron_schedule.describe("5 4 3 2 1"))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1
    print("All cron schedule checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
