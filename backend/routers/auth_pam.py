"""EthOS NAS — PAM authentication helpers

Validates user credentials against Linux PAM (pluggable authentication modules)
and manages seeding of users.json from system accounts.

Usage:
    pam_authenticate(username, password) -> bool
    seed_admin_from_system() -> dict | None   # returns seeded user or None
"""

import hmac
import logging
import os
import secrets
try:
    import pwd
except ImportError:  # pwd is Linux-only
    pass
from datetime import datetime, timezone
from typing import Optional

from backend.services import users_store

logger = logging.getLogger("ethos-nas.routers.auth_pam")

# PAM service — uses /etc/pam.d/login (system's main auth stack)
PAM_SERVICE = "login"

# Valid interactive shells that should be eligible as EthOS users
VALID_SHELLS: frozenset[str] = frozenset({
    "/bin/bash", "/bin/zsh", "/bin/fish", "/bin/sh", "/usr/bin/bash",
    "/usr/bin/zsh", "/usr/bin/fish", "/usr/bin/sh",
})

def pam_authenticate(username: str, password: str) -> bool:
    """Validate credentials against PAM.

    Returns True if the username/password pair authenticates successfully
    via the system's PAM stack (e.g. /etc/pam.d/login).

    Args:
        username: Linux system username.
        password: Plain-text password to verify.

    Returns:
        True on successful authentication, False otherwise.
    """
    try:
        import pamela  # type: ignore[import-untyped]
        pamela.authenticate(username, password, service=PAM_SERVICE)
        return True
    except ImportError:
        logger.warning("pamela not installed — PAM authentication unavailable")
        return False
    except Exception as exc:
        logger.debug("PAM auth failed for user %s: %s", username, exc)
        return False


def _get_eligible_system_users() -> list[dict]:
    """Return all system users with UID 1000-65534 and a valid interactive shell.

    Excludes system accounts like nobody, gdm-greeter, etc. that happen to have
    UIDs >= 1000 but shouldn't be able to log into EthOS.
    """
    eligible: list[dict] = []
    try:
        for entry in pwd.getpwall():
            uid = entry.pw_uid
            if uid < 1000 or uid >= 65534:
                continue
            shell = entry.pw_shell or ""
            if shell not in VALID_SHELLS:
                continue
            eligible.append({
                "username": entry.pw_name,
                "uid": uid,
                "home": entry.pw_dir,
            })
    except Exception as exc:
        logger.error("Failed to enumerate system users: %s", exc)
    return eligible


def seed_admin_from_system() -> Optional[dict]:
    """Seed the first eligible system user as admin in users.json.

    Only seeds if users.json doesn't exist yet (first boot).
    Returns the seeded user dict or None if nothing was seeded.
    """
    if users_store.load():
        return None  # already has data — don't overwrite

    eligible = _get_eligible_system_users()
    if not eligible:
        logger.warning("No eligible system users found for admin seeding")
        return None

    # Pick the first eligible user (typically 'marcin' or similar)
    sys_user = eligible[0]
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    admin_user = {
        "id": "1",
        "username": sys_user["username"],
        "role": "admin",
        "created_at": now_iso,
        "last_login": None,
        "source": "pam_system_seed",
    }

    users_store.save([admin_user])
    logger.info("Seeded admin user %s from system (UID %d)", sys_user["username"], sys_user["uid"])
    return admin_user


# ── API keys (per-user, used by the EthOS Photos Android app) ────────────────

def _generate_api_key() -> str:
    """Generate a new random API key (ethos_ + 48 hex chars)."""
    return "ethos_" + secrets.token_hex(24)


def _load_users_raw() -> list[dict]:
    """Load the account store (locked + atomic; see services/users_store.py)."""
    return users_store.load()


def ensure_api_key(username: str) -> str:
    """Return the user's API key, generating and persisting one if missing.

    The key is set automatically per user (on first login / registration) so
    the user can copy it from Settings → Security and use it to log in from
    the Android app without a password.
    """
    def _mutate(users: list[dict]) -> str:
        for u in users:
            if u.get("username") == username:
                if not u.get("api_key"):
                    u["api_key"] = _generate_api_key()
                    logger.info("Generated API key for user %s", username)
                return u["api_key"]
        new_user = {
            "id": users_store.new_id(),
            "username": username,
            "role": "user",
            "status": users_store.STATUS_ACTIVE,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_login": None,
            "source": "api_key_register",
            "api_key": _generate_api_key(),
        }
        users.append(new_user)
        logger.info("Registered user %s with API key", username)
        return new_user["api_key"]

    return users_store.update(_mutate)


def find_user_by_api_key(api_key: str) -> Optional[dict]:
    """Look up a registered user by their API key (constant-time compare)."""
    if not api_key or not isinstance(api_key, str):
        return None
    for u in _load_users_raw():
        stored = u.get("api_key")
        if stored and hmac.compare_digest(stored, api_key):
            return u
    return None


def regenerate_api_key(username: str) -> str:
    """Replace the user's API key with a fresh one and persist it."""
    def _mutate(users: list[dict]) -> Optional[str]:
        for u in users:
            if u.get("username") == username:
                u["api_key"] = _generate_api_key()
                logger.info("Regenerated API key for user %s", username)
                return u["api_key"]
        return None

    key = users_store.update(_mutate)
    # Not registered yet — register with a fresh key.
    return key or ensure_api_key(username)


def ensure_user_registered(username: str, role: str) -> dict:
    """Ensure a username exists in users.json. Creates if missing.

    Used for auto-registration after successful PAM authentication.

    Args:
        username: The Linux username to register.
        role: Role to assign ("admin" or "user").

    Returns:
        The user dict from users.json (newly created or existing).
    """
    def _mutate(users: list[dict]) -> dict:
        for user in users:
            if user.get("username") == username:
                return user
        new_user = {
            "id": users_store.new_id(),
            "username": username,
            "role": role,
            "status": users_store.STATUS_ACTIVE,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_login": None,
            "source": "pam_auto_register",
        }
        users.append(new_user)
        logger.info("Auto-registered user %s with role %s", username, role)
        return new_user

    return users_store.update(_mutate)


def update_last_login(username: str) -> None:
    """Update the last_login timestamp for a user in users.json."""
    def _mutate(users: list[dict]) -> None:
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for user in users:
            if user.get("username") == username:
                user["last_login"] = now_iso
                break

    users_store.update(_mutate)
