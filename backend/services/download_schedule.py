"""Downloads — schedule window math (TODO.md P2-01).

The pump starts waiting tasks only inside the configured daily window. The
window can cross midnight (22:00 → 06:00), and a disabled or malformed
schedule means "always open". This module is the single, testable source for
that decision; the router keeps only the timer that wakes the pump when the
window opens.
"""

from datetime import datetime
from typing import Optional

from backend.services.download_config import parse_hhmm


def in_window(cfg: dict, now: Optional[datetime] = None) -> bool:
    """True when transfers may start now (no schedule = always true)."""
    sched = cfg.get("schedule") or {}
    if not sched.get("enabled"):
        return True
    start = parse_hhmm(sched.get("start"))
    end = parse_hhmm(sched.get("end"))
    if start is None or end is None or start == end:
        return True
    current = now or datetime.now()
    minute = current.hour * 60 + current.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # overnight window


def seconds_until_window(cfg: dict, now: Optional[datetime] = None) -> float:
    """Seconds until the next window start (minimum 5 s)."""
    sched = cfg.get("schedule") or {}
    start = parse_hhmm(sched.get("start"))
    if start is None:
        return 60.0
    current = now or datetime.now()
    minute = current.hour * 60 + current.minute + current.second / 60.0
    delta = (start - minute) % 1440.0 or 1440.0
    return max(5.0, delta * 60.0)
