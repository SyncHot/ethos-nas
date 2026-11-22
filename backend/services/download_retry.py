"""Downloads — retry policy (TODO.md P2-01).

A network hiccup must not leave a download dead until the user notices; a
permanent failure (dead hoster link, bad debrid key, full disk) must not be
retried, otherwise a package of expired links hammers the provider. The router
keeps the task-state side of a failure; this module owns the decision and the
backoff curve so both are unit-testable.
"""

import re

#: Failure messages another identical attempt cannot fix.
PERMANENT_ERROR_RE = re.compile(
    r"(?i)(not available|not found|404|410|invalid|expired|bad[_ ]?token|"
    r"no debrid provider|no longer configured|not configured|unsupported|forbidden|403|quota|"
    r"permission|no space|disk is full|already exists)"
)

#: Backoff never grows beyond six hours.
MAX_RETRY_DELAY_S = 6 * 3600


def is_retryable(message: str) -> bool:
    """False for failures another identical attempt cannot fix."""
    return not PERMANENT_ERROR_RE.search(str(message or ""))


def retry_delay(cfg: dict, attempt: int) -> int:
    """Exponential base * 2^(attempt-1), capped at six hours."""
    base = max(10, int(cfg.get("retry_delay") or 60))
    return min(MAX_RETRY_DELAY_S, base * (2 ** max(0, attempt - 1)))
