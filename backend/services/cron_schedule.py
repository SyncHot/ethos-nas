"""EthOS NAS — cron schedule parsing, validation and preview.

The Cron app used to accept any non-empty string as a schedule: a typo like
``0 2 * *`` (four fields) was written to the crontab and silently never ran.
This module is the single source of truth for "is this a real schedule?" and
"when will it run next?" — used by the router on write and by the preview
endpoint the UI calls while the user types.

Standard 5-field crontab syntax plus the usual macros. ``@reboot`` is accepted
by crontab but has no predictable future run, so previews report ``None``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

# @reboot is valid in a crontab but cannot be previewed.
REBOOT = "@reboot"

_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_DAYS = {name: index for index, name in enumerate(
    ("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}

FIELDS = (
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day of month", 1, 31, None),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _DAYS),
)

# Minute-by-minute search bound: a yearly job at a fixed minute is found within
# a year; anything further is reported as "unknown" rather than hanging.
_MAX_LOOKAHEAD = timedelta(days=366)


def _parse_value(token: str, lo: int, hi: int, names: dict[str, int] | None) -> int:
    text = token.strip().lower()
    if names and text in names:
        return names[text]
    if not text.isdigit():
        raise ValueError(f"invalid value {token!r}")
    value = int(text)
    if value < lo or value > hi:
        raise ValueError(f"value {value} out of range {lo}-{hi}")
    return value


def _parse_field(field: str, index: int) -> tuple[set[int], bool]:
    name, lo, hi, names = FIELDS[index]
    field = field.strip()
    if not field:
        raise ValueError(f"empty {name} field")
    values: set[int] = set()
    restricted = field != "*"
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty item in {name} field")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise ValueError(f"invalid step in {name} field")
            step = int(step_text)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            start = _parse_value(start_text, lo, hi, names)
            end = _parse_value(end_text, lo, hi, names)
            if end < start:
                raise ValueError(f"reversed range in {name} field")
        else:
            start = _parse_value(part, lo, hi, names)
            end = hi if step > 1 else start
        values.update(range(start, end + 1, step))
    # Cron treats day-of-week 7 as Sunday.
    if index == 4:
        if 7 in values:
            values.discard(7)
            values.add(0)
        values = {value % 7 for value in values}
    return values, restricted


def parse(expr: str) -> dict:
    """Parse a crontab expression into fields; raises ``ValueError``.

    Returns ``{"fields": [set, …], "restricted": [bool, …], "reboot": bool}``.
    """
    text = (expr or "").strip()
    if not text:
        raise ValueError("Schedule is required")
    if text == REBOOT:
        return {"fields": [], "restricted": [], "reboot": True}
    text = MACROS.get(text, text)
    parts = text.split()
    if len(parts) != 5:
        raise ValueError(f"expected 5 fields (minute hour day month weekday), got {len(parts)}")
    fields = []
    restricted = []
    for index, part in enumerate(parts):
        values, is_restricted = _parse_field(part, index)
        fields.append(values)
        restricted.append(is_restricted)
    return {"fields": fields, "restricted": restricted, "reboot": False}


def _matches(spec: dict, moment: datetime) -> bool:
    minutes, hours, days, months, weekdays = spec["fields"]
    if moment.minute not in minutes or moment.hour not in hours or moment.month not in months:
        return False
    day_ok = moment.day in days
    weekday_ok = ((moment.weekday() + 1) % 7) in weekdays
    # Vixie cron: when both day fields are restricted, either may match.
    if spec["restricted"][2] and spec["restricted"][4]:
        return day_ok or weekday_ok
    return day_ok and weekday_ok


def next_runs(expr: str, count: int = 3, start: datetime | None = None) -> list[datetime]:
    """The next ``count`` run times, or ``[]`` for ``@reboot``/unparseable input."""
    try:
        spec = parse(expr)
    except ValueError:
        return []
    if spec["reboot"]:
        return []
    count = max(1, min(10, int(count or 3)))
    moment = (start or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = moment + _MAX_LOOKAHEAD
    found: list[datetime] = []
    while moment <= limit and len(found) < count:
        if _matches(spec, moment):
            found.append(moment)
        moment += timedelta(minutes=1)
    return found


def validate(expr: str) -> None:
    """Raise ``ValueError`` with a user-facing message when the schedule is invalid."""
    parse(expr)


_DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
_DESCRIBED_MACROS = {
    "@hourly": "every hour, on the hour",
    "@daily": "every day at 00:00",
    "@midnight": "every day at 00:00",
    "@weekly": "every Sunday at 00:00",
    "@monthly": "on the 1st of every month at 00:00",
    "@yearly": "on 1 January at 00:00",
    "@annually": "on 1 January at 00:00",
    "@reboot": "at boot",
}


def describe(expr: str) -> str:
    """Short human description for the common shapes; falls back to the expression."""
    text = (expr or "").strip()
    if text in _DESCRIBED_MACROS:
        return _DESCRIBED_MACROS[text]
    parts = text.split()
    if len(parts) != 5:
        return text or "—"
    minute, hour, dom, month, dow = parts
    if text == "* * * * *":
        return "every minute"
    simple_minute = minute.isdigit()
    simple_hour = hour.isdigit()
    if simple_minute and hour.startswith("*/") and hour[2:].isdigit():
        return f"every {hour[2:]} hours at :{int(minute):02d}"
    if simple_minute and simple_hour and dom == "*" and month == "*":
        when = f"at {int(hour):02d}:{int(minute):02d}"
        if dow == "*":
            return f"every day {when}"
        if dow.isdigit() and int(dow) <= 6:
            return f"every {_DAY_NAMES[int(dow)]} {when}"
    if simple_minute and simple_hour and dom.isdigit() and month == "*" and dow == "*":
        return f"on day {int(dom)} of every month at {int(hour):02d}:{int(minute):02d}"
    return text
