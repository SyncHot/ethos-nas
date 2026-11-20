"""EthOS NAS — first-run setup ("claim this appliance").

A flashed EthOS OS appliance boots with one locked admin account and a pending
setup state written by ``ethos-firstboot``:

    /etc/ethos-nas/setup.json
    {"user": "ethosadmin", "code": "ABCD-EFGH", "created_at": "…"}

While that file exists without ``claimed_at`` the web UI shows the setup wizard
instead of the login form. The operator reads the code off the appliance's
screen (HDMI), picks a password, and this router hands the password change to
``deploy/ethos-set-password`` — the only process allowed to run ``chpasswd``
(``/etc/sudoers.d/ethos-nas`` grants it argument-free).

Why the code: the box is reachable at ``ethos-nas.local`` before it is claimed.
Without a secret that only someone physically at the device can read, the first
browser to hit a fresh appliance would be able to set its admin password.

It is not a tax on the person who flashed the image, though. The image records a
policy in the state file (``claim``), and ``local`` — the default — lets callers
on the appliance's own network claim with just a password; the code is only
demanded when the request arrives through a reverse proxy or from a public
address, i.e. when the peer address says nothing about who is knocking.

Hard rules applied here:
  * ``/api/setup/status`` never returns the code or the provisioned password.
  * ``/api/setup/claim`` is rate-limited per client address and dies with the
    state file (the helper marks it claimed — the router cannot be tricked into
    provisioning twice).
  * Neither the code nor the password is ever logged.
"""

import asyncio
import json
import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.config import settings
from backend.routers.auth import _LoginRequest, login as auth_login

logger = logging.getLogger("ethos-nas.routers.setup")
router = APIRouter()

# Five failures per address per ten minutes; the code has ~57 bits of entropy,
# so this is about keeping a LAN scanner from making noise, not about brute
# force. A success clears the counter.
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 600
_ATTEMPTS: dict[str, list[float]] = {}

# How much proof a first-run claim needs. The image writes this into the state
# file (ETHOS_SETUP_CLAIM in /etc/ethos-os/firstboot.env), so the policy travels
# with the appliance instead of depending on the service environment.
#
#   code  — always require the code printed on the appliance's screen
#   local — LAN visitors claim straight away; anything coming through a reverse
#           proxy or from a public address needs the code
#   open  — never require a code (a home appliance that is never exposed)
CLAIM_MODES = ("code", "local", "open")
DEFAULT_CLAIM_MODE = "local"

# Mirrors deploy/ethos-set-password. The helper is the authority (it runs as
# root) — checking here as well keeps a rejected password from spawning a
# privileged process and gives the wizard a readable message.
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200


def _password_problem(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Use at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > MAX_PASSWORD_LENGTH:
        return "That password is too long"
    if password != password.strip():
        return "The password must not start or end with a space"
    if password.isdigit() or password.isalpha():
        return "Mix letters and digits — a single character class is too weak"
    if "\n" in password or ":" in password:
        return "The password must not contain ':' or a line break"
    return ""


class ClaimRequest(BaseModel):
    code: str = ""
    password: str = ""
    # Optional: the login name the operator wants instead of the image default.
    username: str = ""
    model_config = {"extra": "forbid"}


# Same rule as deploy/ethos-set-password: useradd's portable alphabet.
USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def _state() -> Optional[dict]:
    """Pending setup state, or None when the appliance is already claimed."""
    path = settings.SETUP_STATE_FILE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.error("Unreadable setup state %s: %s", path, exc, exc_info=True)
        return None
    if not isinstance(state, dict) or state.get("claimed_at"):
        return None
    return state


def _normalise(code: str) -> str:
    return "".join(str(code or "").upper().split()).replace("-", "")


def _claim_mode(state: dict) -> str:
    mode = str(state.get("claim") or DEFAULT_CLAIM_MODE).strip().lower()
    return mode if mode in CLAIM_MODES else DEFAULT_CLAIM_MODE


def _is_private_host(host: str) -> bool:
    """True for loopback, link-local and RFC1918/ULA addresses."""
    import ipaddress

    try:
        address = ipaddress.ip_address(str(host or "").strip("[]"))
    except ValueError:
        return False
    return bool(address.is_private or address.is_loopback or address.is_link_local)


def _code_required(state: dict, request: Request) -> bool:
    """Does this caller have to present the printed claim code?

    In ``local`` mode a LAN visitor does not: they are already inside the
    network the appliance was plugged into, and asking them to read a code off a
    screen is friction the person who just flashed the image should not pay.

    Behind a reverse proxy (any X-Forwarded-For / X-Real-IP) the peer address is
    the proxy, so a public caller would look private — such requests always need
    the code.
    """
    mode = _claim_mode(state)
    if mode == "open":
        return False
    if mode == "code":
        return True
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") \
            or request.headers.get("forwarded"):
        return True
    client = request.client.host if request.client else ""
    return not _is_private_host(client)


def _rate_limited(client: str) -> bool:
    now = time.monotonic()
    recent = [stamp for stamp in _ATTEMPTS.get(client, []) if now - stamp < ATTEMPT_WINDOW_SECONDS]
    _ATTEMPTS[client] = recent
    return len(recent) >= MAX_ATTEMPTS


def _record_failure(client: str) -> None:
    _ATTEMPTS.setdefault(client, []).append(time.monotonic())


def _clear_failures(client: str) -> None:
    _ATTEMPTS.pop(client, None)


def _helper_command() -> list[str]:
    if settings.SET_PASSWORD_VIA_SUDO:
        return ["sudo", "-n", settings.SET_PASSWORD_HELPER]
    return [settings.SET_PASSWORD_HELPER]


def _call_helper(username: str, password: str, new_username: str = "") -> tuple[bool, str]:
    """Run the root helper on a thread; returns (ok, detail)."""
    request = json.dumps({
        "action": "setup-password",
        "username": username,
        "password": password,
        **({"new_username": new_username} if new_username else {}),
    }).encode()

    def _run() -> tuple[bool, str]:
        import subprocess

        try:
            result = subprocess.run(
                _helper_command(), input=request, capture_output=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error("Setup helper could not run: %s", exc, exc_info=True)
            return False, "The setup helper is unavailable on this system"
        if result.returncode == 0:
            return True, ""
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        return False, (detail[-1] if detail else "Setup failed")[:200]

    return _run()


@router.get("/status")
async def setup_status(request: Request) -> dict:
    """Is this appliance still waiting to be claimed? (public, no secrets)"""
    state = _state()
    if not state:
        return {"required": False}
    return {
        "required": True,
        "user": str(state.get("user") or ""),
        "hostname": request.url.hostname or "",
        # The wizard hides the code field when the caller does not need it.
        "code_required": _code_required(state, request),
    }


@router.post("/claim")
async def setup_claim(req: ClaimRequest, request: Request) -> dict:
    """Set the admin password from the browser and sign the operator in."""
    state = _state()
    if not state:
        raise HTTPException(status_code=409, detail="This appliance is already set up")

    client = request.client.host if request.client else "unknown"
    if _rate_limited(client):
        logger.warning("Setup claim rate-limited for %s", client)
        raise HTTPException(
            status_code=429,
            detail="Too many attempts — wait a few minutes and try again",
        )

    expected = _normalise(state.get("code"))
    supplied = _normalise(req.code)
    # compare_digest keeps the comparison constant-time and only accepts ASCII;
    # anything else is a non-match by definition. The code is only demanded when
    # the caller's origin warrants it (see _code_required).
    import hmac

    if _code_required(state, request):
        matched = (
            bool(expected)
            and expected.isascii()
            and supplied.isascii()
            and hmac.compare_digest(expected, supplied)
        )
    else:
        matched = True
    if not matched:
        _record_failure(client)
        logger.warning("Setup claim rejected for %s (bad code)", client)
        raise HTTPException(
            status_code=403,
            detail="That setup code does not match this appliance",
        )

    username = str(state.get("user") or "")
    if not username:
        logger.error("Setup state has no user: %s", settings.SETUP_STATE_FILE)
        raise HTTPException(status_code=500, detail="Setup state is incomplete")

    problem = _password_problem(req.password)
    if problem:
        # A typo is not a guess: do not count it towards the rate limit.
        raise HTTPException(status_code=400, detail=problem)

    # The helper renames the image's account to this name. Validating here keeps
    # a typo from spawning a privileged process and gives a readable message.
    chosen = str(req.username or "").strip() or username
    if chosen != username:
        if not USERNAME_RE.fullmatch(chosen) or chosen.endswith("-"):
            raise HTTPException(
                status_code=400,
                detail="Use lowercase letters, digits, '-' and '_' (max 32)",
            )

    ok, detail = await asyncio.to_thread(_call_helper, username, req.password, chosen)
    if not ok:
        _record_failure(client)
        raise HTTPException(status_code=400, detail=detail or "Could not set the password")

    _clear_failures(client)
    logger.info("First-run setup completed for account %s", chosen)
    try:
        from backend.services.notifications_service import emit_event

        emit_event(
            kind="success",
            title="EthOS is set up",
            message=f"The admin account {username} is ready.",
            source="setup",
            dedupe_key="setup-claimed",
        )
    except Exception:  # notification must never fail the claim
        logger.debug("Setup notification failed", exc_info=True)

    # Same code path as a normal login: PAM verifies the password we just set,
    # the account is registered in users.json and the response carries a JWT.
    return await auth_login(_LoginRequest(username=chosen, password=req.password))
