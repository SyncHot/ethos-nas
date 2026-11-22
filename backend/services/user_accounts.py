"""Account lifecycle: create, edit, lock and delete EthOS users.

EthOS identity is the Linux account: PAM authenticates it, ``pwd`` supplies its
home, and Samba reuses the same name. The web service runs unprivileged, so
every privileged step goes through one root-owned helper
(``deploy/ethos-user-accounts``, argument-free sudo rule) that validates the
JSON request itself.

This module owns:

* the ``users.json`` registry side (role, status, API key, timestamps),
* the guards the UI must enforce but the API must not trust (no self-delete,
  never remove the last active admin, reserved/system names),
* purging the per-user state under ``DATA_DIR`` when an account is deleted.
"""

import json
import logging
import pwd
import re
import shutil
import subprocess
from datetime import datetime, timezone

from backend.config import settings
from backend.services import json_store, share_links_service, shares_service, user_data, users_store

logger = logging.getLogger("ethos-nas.services.user_accounts")

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
MIN_PASSWORD = 10
MAX_PASSWORD = 200
VALID_ROLES = frozenset({"admin", "user"})
VALID_STATUSES = frozenset({users_store.STATUS_ACTIVE, users_store.STATUS_DISABLED})

RESERVED_USERNAMES = frozenset({
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "gnats",
    "nobody", "systemd-network", "systemd-resolve", "messagebus", "sshd",
    "avahi", "ethos-nas", "ethosadmin",
})

HELPER_TIMEOUT = 45


class UserAccountError(RuntimeError):
    """A failed account operation (mapped to HTTP 500 by the router)."""


class UserAccountNotFound(UserAccountError):
    """The account is not registered or its Linux account is gone."""


class UserAccountConflict(UserAccountError):
    """Duplicate name or a guard (last admin, self-target) rejected the change."""


class UserPolicyError(UserAccountError):
    """Invalid username, password, role or status."""


class UserAccountUnavailable(UserAccountError):
    """The root helper is missing; setup_service.sh must be re-run once."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _helper_command() -> list[str]:
    if settings.USER_ACCOUNTS_VIA_SUDO:
        return ["sudo", "-n", settings.USER_ACCOUNTS_HELPER]
    return [settings.USER_ACCOUNTS_HELPER]


def _call(request: dict) -> dict:
    """Run one helper request; never logs or returns the password field."""
    action = str(request.get("action") or "unknown")
    try:
        result = subprocess.run(
            _helper_command(),
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=HELPER_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.error("Account helper timed out (action=%s)", action, exc_info=True)
        raise UserAccountUnavailable("Account helper timed out") from None
    except FileNotFoundError:
        logger.error("Account helper is unavailable (action=%s)", action, exc_info=True)
        raise UserAccountUnavailable(
            "Account helper is unavailable; rerun setup_service.sh once on this server") from None

    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()[-1:] or []
        message = detail[0] if detail else "Account operation failed"
        logger.error("Account helper failed (action=%s, exit=%s): %s",
                     action, result.returncode, message)
        lowered = message.lower()
        if ("command not found" in lowered or "no such file" in lowered
                or "a password is required" in lowered
                or "interactive authentication is required" in lowered):
            raise UserAccountUnavailable(
                "Account helper is unavailable; rerun setup_service.sh once on this server")
        if "already taken" in message or "already exists" in message:
            raise UserAccountConflict(message)
        if "not found" in message:
            raise UserAccountNotFound(message)
        if ("Invalid" in message or "must" in message or "reserved" in message
                or "System accounts" in message or "cannot be managed" in message):
            raise UserPolicyError(message)
        raise UserAccountError(message)

    try:
        response = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        logger.error("Account helper returned invalid data (action=%s)", action, exc_info=True)
        raise UserAccountError("Account helper returned an invalid response") from None
    if not isinstance(response, dict):
        logger.error("Account helper returned an invalid response (action=%s)", action)
        raise UserAccountError("Account helper returned an invalid response")
    return response


# ── Validation ───────────────────────────────────────────────────────────────

def validate_username(username) -> str:
    name = str(username or "").strip()
    if not USERNAME_RE.fullmatch(name):
        raise UserPolicyError(
            "Use lowercase letters, digits, '-' and '_' (max 32, not a trailing '-')")
    if name.endswith("-"):
        raise UserPolicyError("The username must not end with '-'")
    if name in RESERVED_USERNAMES:
        raise UserPolicyError("That username is reserved")
    return name


def validate_password(password) -> str:
    """Mirror the helper policy so the UI gets an actionable 422, not a 500."""
    if not isinstance(password, str):
        raise UserPolicyError("Password must be a string")
    if len(password) < MIN_PASSWORD:
        raise UserPolicyError(f"Password must be at least {MIN_PASSWORD} characters")
    if len(password) > MAX_PASSWORD:
        raise UserPolicyError("Password is too long")
    if password != password.strip():
        raise UserPolicyError("Password must not start or end with whitespace")
    if password.isdigit() or password.isalpha():
        raise UserPolicyError("Use letters and digits, not just one kind of character")
    if ":" in password or "\n" in password or "\r" in password:
        raise UserPolicyError("Password must not contain ':' or a line break")
    if any(ord(char) < 32 or ord(char) == 127 for char in password):
        raise UserPolicyError("Password must not contain control characters")
    return password


def _validate_role(role) -> str:
    value = str(role or "").strip().lower()
    if value not in VALID_ROLES:
        raise UserPolicyError("Role must be 'admin' or 'user'")
    return value


def _registered(username: str) -> dict:
    record = users_store.find_username(username)
    if record is None or users_store.status_of(record) == users_store.STATUS_DELETED:
        raise UserAccountNotFound("Account not found")
    return record


def _active_admin_count() -> int:
    return sum(
        1 for user in users_store.load()
        if users_store.is_active(user) and str(user.get("role") or "") == "admin"
    )


def _public(user: dict) -> dict:
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "role": user.get("role") or "user",
        "status": users_store.status_of(user),
        "created_at": user.get("created_at"),
        "last_login": user.get("last_login"),
        "source": user.get("source"),
    }


# ── Listing ──────────────────────────────────────────────────────────────────

def list_accounts(include_deleted: bool = False) -> dict:
    """Registry records merged with Linux (uid/home) and Samba state."""
    users = users_store.load_cached()
    samba_available = True
    samba_users: set[str] = set()
    try:
        passdb = shares_service.get_samba_accounts()
        samba_users = {
            str(item.get("username"))
            for item in (passdb.get("users") or [])
            if item.get("samba_enabled")
        }
    except Exception:  # noqa: BLE001 — Samba is optional and must not break the page
        samba_available = False
        logger.debug("Samba accounts unavailable while listing users", exc_info=True)

    out: list[dict] = []
    for user in users:
        status = users_store.status_of(user)
        if status == users_store.STATUS_DELETED and not include_deleted:
            continue
        name = str(user.get("username") or "")
        try:
            account = pwd.getpwnam(name)
            uid, home = account.pw_uid, account.pw_dir
        except KeyError:
            uid, home = None, None
        record = _public(user)
        record.update({
            "uid": uid,
            "home": home,
            "linux": uid is not None,
            "samba": (name in samba_users) if samba_available else None,
        })
        out.append(record)
    out.sort(key=lambda item: str(item.get("username") or "").casefold())
    return {"users": out, "total": len(out), "samba_available": samba_available}


# ── Create / edit ────────────────────────────────────────────────────────────

def create_user(username: str, password: str, role: str = "user",
                samba_password: str = "", actor: str = "") -> dict:
    name = validate_username(username)
    role = _validate_role(role)
    validate_password(password)

    existing = users_store.find_username(name)
    if existing is not None and users_store.status_of(existing) != users_store.STATUS_DELETED:
        raise UserAccountConflict(f"Account '{name}' already exists")
    try:
        pwd.getpwnam(name)
    except KeyError:  # intentional: the name is free, which is what create needs
        pass
    else:
        raise UserAccountConflict(
            "A Linux account with this name already exists — finish deleting it first")

    _call({"action": "create", "username": name, "password": password})

    def _mutate(users: list[dict]) -> dict:
        record = next((u for u in users if u.get("username") == name), None)
        if record is None:
            record = {
                "id": users_store.new_id(),
                "username": name,
                "created_at": _now(),
                "last_login": None,
            }
            users.append(record)
        record.update({
            "role": role,
            "status": users_store.STATUS_ACTIVE,
            "source": "admin_created",
            "api_key": "",
        })
        record.pop("disabled_at", None)
        record.pop("deleted_at", None)
        return record

    record = users_store.update(_mutate)

    samba_error = ""
    if samba_password:
        try:
            shares_service.set_samba_password(name, samba_password)
        except Exception as exc:  # noqa: BLE001 — the Linux account is already usable
            logger.error("Created %s but could not set its Samba password", name, exc_info=True)
            samba_error = str(exc) or "Samba password could not be set"

    logger.info("Created account %s (role=%s, samba=%s, actor=%s)",
                name, role, bool(samba_password), actor or "system")
    return {"user": _public(record), "samba_error": samba_error}


def set_password(username: str, password: str, actor: str = "") -> dict:
    name = validate_username(username)
    validate_password(password)
    _registered(name)
    _call({"action": "set_password", "username": name, "password": password})
    logger.info("Password reset for %s (actor=%s)", name, actor or "system")
    return {"ok": True, "username": name}


def set_role(username: str, role: str, actor: str = "") -> dict:
    name = validate_username(username)
    role = _validate_role(role)
    record = _registered(name)
    current = str(record.get("role") or "user")
    if current == role:
        return _public(record)
    if name == actor and current == "admin" and role != "admin":
        raise UserPolicyError("You cannot remove your own admin role")
    if (users_store.is_active(record) and current == "admin" and role != "admin"
            and _active_admin_count() <= 1):
        raise UserAccountConflict("At least one active admin account is required")

    def _mutate(users: list[dict]) -> dict:
        target = next(u for u in users if u.get("username") == name)
        target["role"] = role
        return target

    updated = users_store.update(_mutate)
    logger.info("Role of %s changed %s → %s (actor=%s)", name, current, role, actor or "system")
    return _public(updated)


def set_status(username: str, status: str, actor: str = "") -> dict:
    name = validate_username(username)
    wanted = str(status or "").strip().lower()
    if wanted not in VALID_STATUSES:
        raise UserPolicyError("Status must be 'active' or 'disabled'")
    record = _registered(name)
    if name == actor and wanted == users_store.STATUS_DISABLED:
        raise UserPolicyError("You cannot disable your own account")
    if (wanted == users_store.STATUS_DISABLED and users_store.is_active(record)
            and str(record.get("role") or "") == "admin" and _active_admin_count() <= 1):
        raise UserAccountConflict("At least one active admin account is required")

    _call({"action": "lock" if wanted == users_store.STATUS_DISABLED else "unlock",
           "username": name})

    def _mutate(users: list[dict]) -> dict:
        target = next(u for u in users if u.get("username") == name)
        target["status"] = wanted
        if wanted == users_store.STATUS_DISABLED:
            target["disabled_at"] = _now()
        else:
            target.pop("disabled_at", None)
        return target

    updated = users_store.update(_mutate)
    logger.info("Account %s is now %s (actor=%s)", name, wanted, actor or "system")
    return _public(updated)


# ── Delete ───────────────────────────────────────────────────────────────────

def _drop_owner(tasks: list, owner: str) -> int:
    if not isinstance(tasks, list):
        return 0
    before = len(tasks)
    tasks[:] = [
        task for task in tasks
        if not (isinstance(task, dict) and str(task.get("owner") or "") == owner)
    ]
    return before - len(tasks)


def purge_user_state(username: str) -> list[str]:
    """Remove every EthOS-owned slice for an account (never touches its home)."""
    safe = user_data.safe_username(username)
    removed: list[str] = []

    for target in (
        user_data.user_dir(safe),
        settings.DATA_DIR / "trash" / safe,
        settings.DATA_DIR / "agent-sessions" / safe,
        settings.DATA_DIR / "agent-cache" / safe,
    ):
        if not target.exists():
            continue
        try:
            shutil.rmtree(target)
            removed.append(str(target))
        except OSError:
            logger.error("Could not remove %s", target, exc_info=True)

    for filename in ("wallpapers.json", "desktop_layouts.json", "favorites.json"):
        store_path = settings.DATA_DIR / filename
        if not store_path.exists():
            continue
        try:
            store = json_store.get(store_path)
            if store.update(lambda data: data.pop(safe, None) is not None):
                removed.append(filename)
        except Exception:  # noqa: BLE001 — one broken store must not block deletion
            logger.error("Could not purge %s for %s", filename, safe, exc_info=True)

    wallpaper_dir = settings.DATA_DIR / "wallpapers"
    if wallpaper_dir.is_dir():
        for path in wallpaper_dir.glob(f"{safe}.*"):
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                logger.error("Could not remove %s", path, exc_info=True)

    try:
        tasks_path = settings.DATA_DIR / "downloads_tasks.json"
        if tasks_path.exists():
            tasks = json_store.get(tasks_path, list)
            if tasks.update(lambda items: _drop_owner(items, safe)):
                removed.append("downloads_tasks.json")
    except Exception:  # noqa: BLE001
        logger.error("Could not purge download tasks for %s", safe, exc_info=True)

    try:
        for link in share_links_service.list_links():
            if str(link.get("owner") or "") not in {safe, username}:
                continue
            if share_links_service.delete_link(str(link.get("id") or "")):
                removed.append(f"share-link:{link.get('id')}")
    except Exception:  # noqa: BLE001
        logger.error("Could not revoke share links for %s", safe, exc_info=True)

    logger.info("Purged EthOS state for %s (%s)", username, ", ".join(removed) or "nothing to remove")
    return removed


def delete_user(username: str, remove_home: bool = False,
                remove_ethos_state: bool = True, actor: str = "") -> dict:
    name = validate_username(username)
    record = users_store.find_username(name)
    if record is None:
        raise UserAccountNotFound("Account not found")

    already_deleted = users_store.status_of(record) == users_store.STATUS_DELETED
    if not already_deleted:
        if name == actor:
            raise UserPolicyError("You cannot delete your own account")
        if (users_store.is_active(record) and str(record.get("role") or "") == "admin"
                and _active_admin_count() <= 1):
            raise UserAccountConflict("At least one active admin account is required")

        # Tombstone first: the user's JWT and API key stop working immediately,
        # even if userdel fails because the account still has running processes.
        def _tombstone(users: list[dict]) -> None:
            target = next(u for u in users if u.get("username") == name)
            target["status"] = users_store.STATUS_DELETED
            target["deleted_at"] = _now()
            target.pop("api_key", None)

        users_store.update(_tombstone)

    samba_error = ""
    try:
        shares_service.delete_samba_account(name)
    except Exception as exc:  # noqa: BLE001 — Samba credentials are optional
        message = str(exc)
        if "not found" not in message.lower():
            logger.warning("Could not remove Samba credentials for %s: %s", name, message)
            samba_error = message

    _call({"action": "delete", "username": name, "remove_home": bool(remove_home)})

    purged = purge_user_state(name) if remove_ethos_state else []
    logger.info("Deleted account %s (home=%s, state entries=%d, actor=%s)",
                name, "removed" if remove_home else "kept", len(purged), actor or "system")
    return {
        "ok": True,
        "username": name,
        "status": users_store.STATUS_DELETED,
        "home_removed": bool(remove_home),
        "purged": purged,
        "samba_error": samba_error,
    }
