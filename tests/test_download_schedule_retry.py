#!/usr/bin/env python3
"""Downloads engine helpers — schedule window and retry policy (no server).

Extracted from `routers/downloads.py` in P2-01.

Run:  .venv/bin/python tests/test_download_schedule_retry.py
"""

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import download_retry, download_schedule  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 22, hour, minute)


def main() -> int:
    print("schedule window")
    check("disabled schedule is always open",
          download_schedule.in_window({"schedule": {"enabled": False, "start": "08:00", "end": "18:00"}},
                                      _at(3)))
    day = {"schedule": {"enabled": True, "start": "08:00", "end": "18:00"}}
    check("inside daytime window", download_schedule.in_window(day, _at(9)))
    check("start is inclusive", download_schedule.in_window(day, _at(8)))
    check("end is exclusive", not download_schedule.in_window(day, _at(18)))
    check("before window is closed", not download_schedule.in_window(day, _at(7, 59)))

    night = {"schedule": {"enabled": True, "start": "22:00", "end": "06:00"}}
    check("overnight after start", download_schedule.in_window(night, _at(23)))
    check("overnight before end", download_schedule.in_window(night, _at(2)))
    check("overnight midday closed", not download_schedule.in_window(night, _at(12)))

    check("malformed start keeps the window open",
          download_schedule.in_window({"schedule": {"enabled": True, "start": "x", "end": "06:00"}}, _at(12)))
    check("equal start/end keeps the window open",
          download_schedule.in_window({"schedule": {"enabled": True, "start": "06:00", "end": "06:00"}}, _at(12)))
    check("no schedule key means open", download_schedule.in_window({}, _at(12)))

    print("seconds until window")
    one_hour = download_schedule.seconds_until_window(
        {"schedule": {"enabled": True, "start": "22:00", "end": "06:00"}}, _at(21))
    check("counts to the start", one_hour == 3600.0, str(one_hour))
    full_day = download_schedule.seconds_until_window(
        {"schedule": {"enabled": True, "start": "22:00", "end": "06:00"}}, _at(22))
    check("at the start waits a full day", full_day == 86400.0, str(full_day))
    check("malformed start waits a minute",
          download_schedule.seconds_until_window({"schedule": {"start": "nope"}}) == 60.0)
    tiny = download_schedule.seconds_until_window(
        {"schedule": {"start": "22:00"}}, datetime(2026, 11, 22, 21, 59, 59, 900000))
    check("minimum delay is 5 s", tiny >= 5.0, str(tiny))

    print("retry policy")
    check("timeout is retryable", download_retry.is_retryable("read timed out"))
    check("connection reset is retryable", download_retry.is_retryable("connection reset by peer"))
    check("empty message is retryable", download_retry.is_retryable(""))
    for message in ("404 Not Found", "403 forbidden", "bad token", "no space left",
                    "disk is full", "quota exceeded", "file already exists", "expired link"):
        check(f"permanent: {message!r}", not download_retry.is_retryable(message))

    check("default base 60 s", download_retry.retry_delay({}, 1) == 60)
    check("doubles per attempt", download_retry.retry_delay({}, 3) == 240)
    check("custom base", download_retry.retry_delay({"retry_delay": 10}, 2) == 20)
    check("caps at six hours", download_retry.retry_delay({"retry_delay": 3600}, 12) == 6 * 3600)
    check("base is floored at 10 s", download_retry.retry_delay({"retry_delay": 1}, 1) == 10)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1
    print("All download schedule/retry checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
