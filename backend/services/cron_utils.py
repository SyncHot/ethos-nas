"""Cron parsing, matching and next-run calculation.

Extracted from `routers/backup.py` so the backup scheduler, the `/api/cron`
domain and future jobs can share one implementation (TODO.md P2-07).

Supported syntax: standard 5 fields (minute hour day-of-month month day-of-week),
lists, ranges, `*/step`, and the `@yearly|@annually|@monthly|@weekly|@daily|
@midnight|@hourly` macros. Day-of-month/day-of-week follow the classic cron
rule: when both are restricted, either one matching is enough.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("ethos-nas.services.cron_utils")

_MACROS = {
    "yearly", "annually", "monthly", "weekly", "daily", "midnight", "hourly",
}


def field_matches(field: str, value: int, lo: int, hi: int) -> bool:
    """Match one cron field against a value. Supports *, lists, ranges, steps."""
    for part in field.split(","):
        part = part.strip()
        if not part:
            return False
        step = 1
        rng = part
        if "/" in part:
            rng, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                return False
            if step <= 0:
                return False
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                return False
        else:
            try:
                start = int(rng)
            except ValueError:
                return False
            end = hi if "/" in part else start
        if not (lo <= start <= hi and lo <= end <= hi):
            continue
        if value < start or value > end:
            continue
        if (value - start) % step == 0:
            return True
    return False


def field_valid(field: str, lo: int, hi: int, allow_question: bool = False) -> bool:
    """True when a field parses and can match at least one in-range value."""
    field = (field or "").strip()
    if allow_question and field == "?":
        return True
    return any(field_matches(field, v, lo, hi) for v in range(lo, hi + 1))


def is_valid(expr: str) -> bool:
    """Validate a 5-field cron expression or a supported @macro."""
    expr = (expr or "").strip()
    if not expr:
        return False
    if expr.startswith("@"):
        return expr[1:].strip().lower() in _MACROS
    fields = expr.split()
    if len(fields) != 5:
        return False
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    return all(
        field_valid(f, lo, hi, allow_question=(i in (2, 4)))
        for i, (f, (lo, hi)) in enumerate(zip(fields, bounds))
    )


def cron_dow(day_index: int) -> int:
    """Map 0-6 (Mon-Sun weekday index) to cron dow (0=Sun)."""
    return (day_index + 1) % 7


def matches(expr: str, dt: datetime) -> bool:
    """True when a cron expression fires at minute `dt`."""
    expr = (expr or "").strip()
    if not expr:
        return False
    dow = cron_dow(dt.weekday())
    if expr.startswith("@"):
        macro = expr[1:].strip().lower()
        if macro in ("yearly", "annually"):
            return dt.month == 1 and dt.day == 1 and dt.hour == 0 and dt.minute == 0
        if macro == "monthly":
            return dt.day == 1 and dt.hour == 0 and dt.minute == 0
        if macro == "weekly":
            return dow == 0 and dt.hour == 0 and dt.minute == 0
        if macro in ("daily", "midnight"):
            return dt.hour == 0 and dt.minute == 0
        if macro == "hourly":
            return dt.minute == 0
        return False
    fields = expr.split()
    if len(fields) != 5:
        return False
    m, h, dom, mon, dow_f = fields
    if not (field_matches(m, dt.minute, 0, 59)
            and field_matches(h, dt.hour, 0, 23)
            and field_matches(mon, dt.month, 1, 12)):
        return False
    dom_restricted = dom not in ("*", "?")
    dow_restricted = dow_f not in ("*", "?")
    if dom_restricted and dow_restricted:
        day_ok = field_matches(dom, dt.day, 1, 31) or field_matches(dow_f, dow, 0, 6)
    elif dom_restricted:
        day_ok = field_matches(dom, dt.day, 1, 31)
    elif dow_restricted:
        day_ok = field_matches(dow_f, dow, 0, 6)
    else:
        day_ok = True
    return day_ok


def next_run(expr: str, after: Optional[datetime] = None) -> Optional[str]:
    """Next fire time of a cron expression (ISO string), or None if it never fires.

    Scans day-by-day (<= 400 days) using precomputed field sets — fast enough to
    run per-schedule on every list request.
    """
    expr = (expr or "").strip()
    after = after or datetime.now().astimezone()
    if not expr:
        return None

    def _field_set(field: str, lo: int, hi: int) -> set[int]:
        out = set()
        for v in range(lo, hi + 1):
            try:
                if field_matches(field, v, lo, hi):
                    out.add(v)
            except Exception:
                logger.debug("Cron field %r unparseable — skipped", field, exc_info=True)
        return out

    if expr.startswith("@"):
        macro = expr[1:].strip().lower()

        def _fmt(t: datetime) -> str:
            return t.isoformat(timespec="seconds")

        midnight_today = after.replace(hour=0, minute=0, second=0, microsecond=0)
        if macro in ("daily", "midnight"):
            return _fmt(midnight_today + timedelta(days=1))
        if macro == "hourly":
            return _fmt((after + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
        if macro in ("yearly", "annually"):
            t = midnight_today.replace(month=1, day=1)
            if t <= after:
                t = t.replace(year=t.year + 1)
            return _fmt(t)
        if macro == "monthly":
            t = midnight_today.replace(day=1)
            if t <= after:
                y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
                t = t.replace(year=y, month=m, day=1)
            return _fmt(t)
        if macro == "weekly":
            offset = (6 - after.weekday()) % 7  # next Sunday
            t = midnight_today + timedelta(days=offset)
            if t <= after:
                t += timedelta(days=7)
            return _fmt(t)
        return None

    fields = expr.split()
    if len(fields) != 5:
        return None
    m_f, h_f, dom_f, mon_f, dow_f = fields
    minutes = _field_set(m_f, 0, 59)
    hours = _field_set(h_f, 0, 23)
    months = _field_set(mon_f, 1, 12)
    doms = _field_set(dom_f, 1, 31) if dom_f not in ("*", "?") else set(range(1, 32))
    dows = {cron_dow(d) for d in range(7)} if dow_f in ("*", "?") else \
        {v for v in range(7) if field_matches(dow_f, v, 0, 6)}
    dom_restricted = dom_f not in ("*", "?")
    dow_restricted = dow_f not in ("*", "?")

    start_day = after.replace(hour=0, minute=0, second=0, microsecond=0)
    for off in range(400):
        d = start_day + timedelta(days=off)
        if d.month not in months:
            continue
        cdow = cron_dow(d.weekday())
        dom_ok = d.day in doms
        dow_ok = cdow in dows
        if dom_restricted and dow_restricted:
            day_ok = dom_ok or dow_ok
        elif dom_restricted:
            day_ok = dom_ok
        elif dow_restricted:
            day_ok = dow_ok
        else:
            day_ok = True
        if not day_ok:
            continue
        for h in sorted(hours):
            for mi in sorted(minutes):
                cand = d.replace(hour=h, minute=mi)
                if cand > after:
                    return cand.isoformat(timespec="seconds")
    return None
