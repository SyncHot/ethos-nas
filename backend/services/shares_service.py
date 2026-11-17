"""EthOS NAS — Samba Share Management Service

Manages Samba shares through /etc/samba/smb.conf.
Uses configparser for safe parsing and testparm for validation.
"""

import configparser
import grp
import json
import logging
import os
import pwd
import re
import subprocess
from typing import Optional

from backend.services import system_deps

logger = logging.getLogger("ethos-nas.services.shares_service")

SMB_CONF = "/etc/samba/smb.conf"
SAMBA_ACCOUNTS_HELPER = "/usr/local/bin/ethos-samba-accounts"

# Share names become [section] headers in smb.conf — anything beyond plain
# alphanumerics/underscore/dash/dot can corrupt the file or inject directives.
_SHARE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,64}$")
_SAMBA_USERNAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}$")
_SHARE_MASK_RE = re.compile(r"^0?[0-7]{3}$")
_VALID_USER_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.$%@+&/\\-]{1,128}$")
_SPECIAL_SECTIONS = {"global", "homes", "printers", "print$"}


class SambaUnavailable(RuntimeError):
    """Samba is not installed on this host — nothing can be validated or applied.

    Carries an operator-facing message with the exact install command, so the
    router can answer 503 instead of leaking a raw ``[Errno 2]`` from subprocess.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class SambaAccountError(RuntimeError):
    """A safe, non-sensitive failure from the Samba passdb helper."""


def _samba_status() -> dict:
    """Probe the host for the Samba binaries before doing anything else."""
    probed = system_deps.probe_one("samba")
    return {
        "installed": bool(probed["ok"]),
        "missing_tools": list(probed.get("missing_tools") or []),
        "install_hint": probed.get("hint") or "",
    }


def require_samba() -> dict:
    """Raise :class:`SambaUnavailable` when Samba is missing; else return its state."""
    status = _samba_status()
    if not status["installed"]:
        tools = ", ".join(status["missing_tools"]) or "smbd"
        logger.error("Samba is not installed (missing: %s)", tools)
        raise SambaUnavailable(
            f"Samba is not installed on this server (missing: {tools}). "
            f"Run `{status['install_hint']}` on the NAS, then retry."
        )
    return status


def _owner_names(path: str) -> tuple[str, str]:
    """Return (user, group) names owning *path*."""
    st = os.stat(path)
    return pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name


def _apply_guest_write_fix(config: configparser.ConfigParser, name: str) -> None:
    """Ensure a guest-writable share can actually write.

    Guest sessions run as the Samba 'nobody' user, which typically has no
    permission on the shared directory (e.g. /mnt/Backup1 owned by ethos-nas).
    Pointing `force user`/`force group` at the directory owner makes every SMB
    file operation run with write access — public shares then just work.
    """
    guest_ok = config.get(name, "guest ok", fallback="no").lower() == "yes"
    read_only = _share_is_read_only(config, name)
    if not (guest_ok and not read_only):
        return
    path = config.get(name, "path", fallback="")
    try:
        user, group = _owner_names(path)
    except (OSError, KeyError):
        logger.warning("Cannot resolve owner of %s — skipping force user for [%s]", path, name)
        return
    config[name]["force user"] = user
    config[name]["force group"] = group


def _share_is_read_only(config: configparser.ConfigParser, name: str) -> bool:
    read_only = config.get(name, "read only", fallback=None)
    if read_only is not None:
        return read_only.strip().lower() not in {"no", "false", "0"}
    writable = config.get(name, "writable", fallback=None)
    if writable is None:
        writable = config.get(name, "writeable", fallback=None)
    return writable is None or writable.strip().lower() not in {"yes", "true", "1"}


def _run_smb_command(args: list[str]) -> subprocess.CompletedProcess:
    """Run a Samba-related command (testparm, smbpasswd, etc.)."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return result
    except subprocess.TimeoutExpired:
        logger.error("Samba command timed out: %s", " ".join(args))
        raise
    except FileNotFoundError:
        logger.error("Samba tool not found: %s", args[0])
        raise


def _load_config() -> configparser.ConfigParser:
    """Load smb.conf safely."""
    config = configparser.ConfigParser(interpolation=None)
    if os.path.exists(SMB_CONF):
        config.read(SMB_CONF)
    return config


def get_shares() -> list[dict]:
    """List all Samba shares (excluding special [global], [homes], [printers], [print$])."""
    config = _load_config()
    shares = []
    for section in config.sections():
        if section.casefold() in _SPECIAL_SECTIONS:
            continue
        share = {
            "name": section,
            "path": config.get(section, "path", fallback=""),
            "comment": config.get(section, "comment", fallback=""),
            "read_only": _share_is_read_only(config, section),
            "browsable": config.get(section, "browseable", fallback="yes").lower() == "yes",
            "guest_ok": config.get(section, "guest ok", fallback="no").lower() == "yes",
            "valid_users": config.get(section, "valid users", fallback=""),
            "read_users": _principal_option(config, section, "read list"),
            "write_users": _principal_option(config, section, "write list"),
            "writable": not _share_is_read_only(config, section),
            "create_mask": config.get(section, "create mask", fallback=""),
            "directory_mask": config.get(section, "directory mask", fallback=""),
        }
        shares.append(share)
    shares.sort(key=lambda s: s["name"])
    return shares


def _principal_option(config: configparser.ConfigParser, section: str, option: str) -> list[str]:
    value = config.get(section, option, fallback="")
    return [item for item in re.split(r"[\s,]+", value.strip()) if item]


def _validate_principal(principal: str) -> str:
    is_group = principal.startswith("@")
    name = principal[1:] if is_group else principal
    if not _SAMBA_USERNAME_RE.fullmatch(name) or name in {"root", "ethos-nas", "nobody"}:
        raise ValueError(f"Invalid share account or group: {principal}")
    try:
        account_id = grp.getgrnam(name).gr_gid if is_group else pwd.getpwnam(name).pw_uid
    except KeyError:
        raise ValueError(f"Share account or group not found: {principal}") from None
    if account_id < 1000:
        raise ValueError(f"System accounts and groups cannot be shared: {principal}")
    return principal


def _parse_valid_users(value: str) -> list[str]:
    if not value:
        return []
    if len(value) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid valid users value")
    principals = [item for item in re.split(r"[\s,]+", value.strip()) if item]
    if any(item in {".", ".."} or not _VALID_USER_TOKEN_RE.fullmatch(item) for item in principals):
        raise ValueError("Invalid valid users value")
    return list(dict.fromkeys(principals))


def _validate_acl_list(values: Optional[list[str]]) -> Optional[list[str]]:
    if values is None:
        return None
    if len(values) > 256:
        raise ValueError("A share can have at most 256 user or group ACL entries")
    return list(dict.fromkeys(_validate_principal(item) for item in values))


def _apply_user_acl(
    config: configparser.ConfigParser,
    name: str,
    read_users: Optional[list[str]],
    write_users: Optional[list[str]],
) -> None:
    if read_users is None and write_users is None:
        return
    current_read = _principal_option(config, name, "read list")
    current_write = _principal_option(config, name, "write list")
    readers = _validate_acl_list(current_read if read_users is None else read_users) or []
    writers = _validate_acl_list(current_write if write_users is None else write_users) or []
    overlap = set(readers) & set(writers)
    if overlap:
        raise ValueError("An account or group cannot have both read-only and write access")
    allowed = list(dict.fromkeys(readers + writers))
    if allowed:
        config[name]["valid users"] = " ".join(allowed)
    else:
        config[name].pop("valid users", None)
    if readers:
        config[name]["read list"] = " ".join(readers)
    else:
        config[name].pop("read list", None)
    if writers:
        config[name]["write list"] = " ".join(writers)
    else:
        config[name].pop("write list", None)
    config[name]["read only"] = "yes"


def _validate_share_directory(path: str) -> None:
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise ValueError("Share path must be an absolute directory path")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("Invalid share path")
    if not os.path.isdir(path):
        raise ValueError(f"Path is not an existing directory: {path}")


def _validate_mask(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SHARE_MASK_RE.fullmatch(value):
        raise ValueError(f"{label} must be an octal permission mask, for example 0755")
    return value if len(value) == 4 else "0" + value


def _run_samba_accounts(request: dict) -> dict:
    """Call the fixed-operation root helper without exposing credentials."""
    action = request.get("action", "unknown")
    try:
        result = subprocess.run(
            ["sudo", "-n", SAMBA_ACCOUNTS_HELPER],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("Samba account helper timed out (action=%s)", action, exc_info=True)
        raise SambaAccountError("Samba account operation timed out") from None
    except FileNotFoundError as exc:
        logger.error("Samba account helper is unavailable (action=%s)", action, exc_info=True)
        raise SambaAccountError("Samba account helper is unavailable; rerun setup_service.sh") from None
    if result.returncode != 0:
        logger.error("Samba account helper failed (action=%s, exit=%s)", action, result.returncode)
        raise SambaAccountError("Samba account operation failed")
    try:
        response = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        logger.error("Samba account helper returned invalid data (action=%s)", action)
        raise SambaAccountError("Samba account helper returned an invalid response") from None
    if not isinstance(response, dict):
        logger.error("Samba account helper returned an invalid response (action=%s)", action)
        raise SambaAccountError("Samba account helper returned an invalid response")
    return response


def get_samba_accounts() -> dict:
    """List eligible existing Linux users/groups and their Samba account state."""
    require_samba()
    passdb = _run_samba_accounts({"action": "list"})
    samba_users = set(passdb.get("users") or [])
    users = [
        {"username": account.pw_name, "samba_enabled": account.pw_name in samba_users}
        for account in pwd.getpwall()
        if account.pw_uid >= 1000 and account.pw_name not in {"ethos-nas", "nobody"}
    ]
    groups = [
        {"name": group.gr_name}
        for group in grp.getgrall()
        if group.gr_gid >= 1000 and group.gr_name not in {"ethos-nas", "nogroup"}
    ]
    users.sort(key=lambda item: item["username"].casefold())
    groups.sort(key=lambda item: item["name"].casefold())
    return {"users": users, "groups": groups}


def set_samba_password(username: str, password: str) -> dict:
    """Create or update a Samba password for an existing non-system account."""
    require_samba()
    _validate_samba_username(username)
    response = _run_samba_accounts({
        "action": "set_password", "username": username, "password": password,
    })
    return {"username": response.get("username", username), "status": "updated"}


def delete_samba_account(username: str) -> dict:
    """Remove a user's Samba credentials without affecting its Linux account."""
    require_samba()
    _validate_samba_username(username)
    accounts = _run_samba_accounts({"action": "list"}).get("users", [])
    if username not in accounts:
        raise ValueError(f"Samba account '{username}' not found")
    response = _run_samba_accounts({"action": "delete", "username": username})
    return {"username": response.get("username", username), "status": "deleted"}


def _validate_samba_username(username: str) -> None:
    if not isinstance(username, str) or not _SAMBA_USERNAME_RE.fullmatch(username):
        raise ValueError("Invalid Samba username")
    if username in {"root", "ethos-nas"}:
        raise ValueError("This account cannot be managed by Samba Shares")
    try:
        account = pwd.getpwnam(username)
    except KeyError:
        raise ValueError("Linux account not found") from None
    if account.pw_uid < 1000:
        raise ValueError("System accounts cannot be managed by Samba Shares")


def add_share(
    name: str,
    path: str,
    comment: str = "",
    read_only: bool = False,
    browsable: bool = True,
    guest_ok: bool = False,
    valid_users: str = "",
    create_mask: str = "0755",
    directory_mask: str = "0755",
    read_users: Optional[list[str]] = None,
    write_users: Optional[list[str]] = None,
) -> dict:
    """Add a new Samba share.

    Args:
        name: Share name (section header in smb.conf).
        path: Directory path to share.
        comment: Human-readable description.
        read_only: If True, share is read-only.
        browsable: If True, appears in network browse list.
        guest_ok: If True, allows guest access.
        valid_users: Comma-separated list of allowed users.
        create_mask: Octal permission mask for new files.
        directory_mask: Octal permission mask for new directories.

    Returns:
        {name, path, status}

    Raises:
        ValueError: If name/path missing, path doesn't exist, or share already exists.
    """
    if not name or not path:
        raise ValueError("Share name and path are required")
    if not _SHARE_NAME_RE.match(name):
        raise ValueError(
            "Invalid share name — use letters, digits, '_', '-' or '.' (max 64 chars)"
        )
    _validate_share_directory(path)
    create_mask = _validate_mask(create_mask, "Create mask")
    directory_mask = _validate_mask(directory_mask, "Directory mask")
    parsed_valid_users = _parse_valid_users(valid_users)
    read_users = _validate_acl_list(read_users)
    write_users = _validate_acl_list(write_users)
    if read_only and write_users:
        raise ValueError("A read-only share cannot grant write access to selected users or groups")
    if guest_ok and (parsed_valid_users or read_users or write_users):
        raise ValueError("Guest access cannot be combined with a user/group access list")
    require_samba()

    config = _load_config()
    if any(section.casefold() == name.casefold() for section in config.sections()):
        raise ValueError(f"Share '{name}' already exists")

    config[name] = {
        "path": path,
        "comment": comment or name,
        "read only": "yes" if read_only else "no",
        "browseable": "yes" if browsable else "no",
        "guest ok": "yes" if guest_ok else "no",
        "create mask": create_mask,
        "directory mask": directory_mask,
    }
    if parsed_valid_users:
        config[name]["valid users"] = " ".join(parsed_valid_users)

    _apply_user_acl(config, name, read_users, write_users)

    _apply_guest_write_fix(config, name)

    _apply_config(config)
    return {"name": name, "path": path, "status": "added"}


def update_share(
    name: str,
    path: Optional[str] = None,
    comment: Optional[str] = None,
    read_only: Optional[bool] = None,
    browsable: Optional[bool] = None,
    guest_ok: Optional[bool] = None,
    valid_users: Optional[str] = None,
    create_mask: Optional[str] = None,
    directory_mask: Optional[str] = None,
    read_users: Optional[list[str]] = None,
    write_users: Optional[list[str]] = None,
) -> dict:
    """Update an existing Samba share.

    Only provided fields are changed; None leaves the existing value unchanged.

    Args:
        name: Share name to update.
        path: New directory path.
        comment: New description.
        read_only: New read-only flag.
        browsable: New browsable flag.
        guest_ok: New guest access flag.
        valid_users: New valid users list.
        create_mask: New create mask.
        directory_mask: New directory mask.

    Returns:
        {name, status}

    Raises:
        ValueError: If share not found or path doesn't exist.
    """
    require_samba()
    config = _load_config()
    if name.casefold() in _SPECIAL_SECTIONS:
        raise ValueError("Special Samba sections cannot be managed as shares")
    if name not in config.sections():
        raise ValueError(f"Share '{name}' not found")

    if path is not None:
        _validate_share_directory(path)
        config[name]["path"] = path
    if comment is not None:
        config[name]["comment"] = comment
    if read_only is not None:
        config[name]["read only"] = "yes" if read_only else "no"
    if browsable is not None:
        config[name]["browseable"] = "yes" if browsable else "no"
    if guest_ok is not None:
        config[name]["guest ok"] = "yes" if guest_ok else "no"
    if valid_users is not None:
        parsed_valid_users = _parse_valid_users(valid_users)
        if parsed_valid_users:
            config[name]["valid users"] = " ".join(parsed_valid_users)
        else:
            config[name].pop("valid users", None)
    if create_mask is not None:
        config[name]["create mask"] = _validate_mask(create_mask, "Create mask")
    if directory_mask is not None:
        config[name]["directory mask"] = _validate_mask(directory_mask, "Directory mask")

    read_users = _validate_acl_list(read_users)
    write_users = _validate_acl_list(write_users)
    current_write_users = _principal_option(config, name, "write list")
    effective_write_users = current_write_users if write_users is None else write_users
    effective_read_only = _share_is_read_only(config, name)
    if effective_read_only and effective_write_users:
        raise ValueError("A read-only share cannot grant write access to selected users or groups")
    effective_guest = config.get(name, "guest ok", fallback="no").lower() == "yes"
    access_changed = any(value is not None for value in (guest_ok, valid_users, read_users, write_users))
    has_user_restrictions = bool(
        config.get(name, "valid users", fallback="").strip()
        or _principal_option(config, name, "read list")
        or _principal_option(config, name, "write list")
    )
    if effective_guest and access_changed and has_user_restrictions:
        raise ValueError("Guest access cannot be combined with a user/group access list")
    _apply_user_acl(config, name, read_users, write_users)

    # Self-heal: re-apply force user/group to the (possibly new) path owner so
    # public writable shares keep working after edits.
    _apply_guest_write_fix(config, name)

    _apply_config(config)
    return {"name": name, "status": "updated"}


def delete_share(name: str) -> dict:
    """Delete a Samba share.

    Args:
        name: Share name to remove.

    Returns:
        {name, status}

    Raises:
        ValueError: If share not found.
    """
    require_samba()
    config = _load_config()
    if name.casefold() in _SPECIAL_SECTIONS:
        raise ValueError("Special Samba sections cannot be managed as shares")
    if name not in config.sections():
        raise ValueError(f"Share '{name}' not found")

    config.remove_section(name)
    _apply_config(config)
    return {"name": name, "status": "deleted"}


def _validate_smb_content(content: str) -> None:
    """Run testparm against `content` WITHOUT touching SMB_CONF.

    Raises ValueError when the config is invalid, so callers can refuse to
    commit a broken file. Skipped (with a warning) when testparm is missing.
    """
    import tempfile

    tmp = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    try:
        tmp.write(content)
        tmp.close()
        result = subprocess.run(
            ["testparm", "-s", tmp.name],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ValueError(f"Invalid Samba config (testparm): {detail}")
    except FileNotFoundError:
        logger.warning("testparm not found — skipping pre-write validation")
    finally:
        os.unlink(tmp.name)


def _serialize_config(config: configparser.ConfigParser) -> str:
    """Return a validated Samba config serialization."""
    # Ensure [global] section exists with sensible defaults
    if "global" not in config.sections():
        config["global"] = {}
    if "workgroup" not in config["global"]:
        config["global"]["workgroup"] = "WORKGROUP"
    if "server string" not in config["global"]:
        config["global"]["server string"] = "EthOS NAS"
    if "netbios name" not in config["global"]:
        import socket

        config["global"]["netbios name"] = socket.gethostname().upper()
    if "security" not in config["global"]:
        config["global"]["security"] = "user"
    if "map to guest" not in config["global"]:
        config["global"]["map to guest"] = "bad user"

    import io

    buf = io.StringIO()
    config.write(buf, space_around_delimiters=True)
    content = buf.getvalue()
    _validate_smb_content(content)
    return content


def _write_smb_content(content: str) -> None:
    """Write raw config content using the narrow direct-write/tee paths."""

    try:
        with open(SMB_CONF + ".tmp", "w") as f:
            f.write(content)
        os.rename(SMB_CONF + ".tmp", SMB_CONF)
        logger.info("Samba config written to %s (direct)", SMB_CONF)
        return
    except PermissionError:
        pass  # Fall through to sudo tee

    try:
        result = subprocess.run(
            ["sudo", "-n", "tee", SMB_CONF],
            input=content,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.error("sudo tee failed to write %s: %s", SMB_CONF, result.stderr)
            raise PermissionError(
                f"Cannot write {SMB_CONF}. Run setup_scripts/setup_service.sh as root "
                "to install the NOPASSWD rules."
            )
        logger.info("Samba config written to %s via sudo tee", SMB_CONF)
    except FileNotFoundError:
        raise RuntimeError("sudo not found — cannot write Samba config")


def _write_config(config: configparser.ConfigParser) -> None:
    """Validate and write a serialized Samba config."""
    if "global" not in config.sections():
        config["global"] = {}
    if "workgroup" not in config["global"]:
        config["global"]["workgroup"] = "WORKGROUP"
    if "server string" not in config["global"]:
        config["global"]["server string"] = "EthOS NAS"
    if "netbios name" not in config["global"]:
        import socket

        config["global"]["netbios name"] = socket.gethostname().upper()
    if "security" not in config["global"]:
        config["global"]["security"] = "user"
    if "map to guest" not in config["global"]:
        config["global"]["map to guest"] = "bad user"
    _write_smb_content(_serialize_config(config))


def _apply_config(config: configparser.ConfigParser) -> None:
    """Write and reload a config, restoring the old file if reload fails."""
    try:
        with open(SMB_CONF, encoding="utf-8") as source:
            previous = source.read()
    except FileNotFoundError:
        previous = None
    except OSError as exc:
        logger.error("Cannot snapshot Samba config before update", exc_info=True)
        raise RuntimeError("Cannot safely update Samba config") from None

    _write_config(config)
    try:
        _reload_samba()
    except Exception:
        logger.error("Samba config reload failed; restoring prior config state", exc_info=True)
        if previous is not None:
            try:
                _write_smb_content(previous)
                _reload_samba()
            except Exception:
                logger.error("Failed to restore previous Samba config", exc_info=True)
                raise RuntimeError("Samba reload failed and previous config could not be restored") from None
        else:
            try:
                defaults = configparser.ConfigParser()
                defaults["global"] = {}
                _write_config(defaults)
                _reload_samba()
            except Exception:
                logger.error("Failed to restore Samba defaults after reload failure", exc_info=True)
                raise RuntimeError("Samba reload failed and prior config state could not be restored") from None
        raise RuntimeError("Samba config reload failed; prior config state was restored") from None


def _reload_samba() -> None:
    """Reload Samba configuration without restarting the service.

    Tries direct systemctl first (works when running as root), falls back
    to `sudo systemctl` for unprivileged users. Requires NOPASSWD sudo rule
    — see setup_scripts/setup_service.sh.
    """
    try:
        result = subprocess.run(
            ["testparm", "-s", SMB_CONF], capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.error("Could not validate Samba config before reload", exc_info=True)
        raise RuntimeError("Could not validate Samba config") from None
    if result.returncode != 0:
        logger.error("Samba config validation failed (testparm exit=%s)", result.returncode)
        raise ValueError("Invalid Samba config; testparm rejected the change")

    service_files = (
        ("samba", "/usr/lib/systemd/system/samba.service", "/lib/systemd/system/samba.service"),
        ("smbd", "/usr/lib/systemd/system/smbd.service", "/lib/systemd/system/smbd.service"),
    )
    candidates = [
        unit for unit, *paths in service_files if any(os.path.exists(path) for path in paths)
    ]
    for unit in candidates or ["smbd", "samba"]:
        for command in (["systemctl", "reload", unit], ["sudo", "-n", "systemctl", "reload", unit]):
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                logger.error("Samba reload command failed (unit=%s)", unit, exc_info=True)
                continue
            if result.returncode == 0:
                logger.info("Samba configuration reloaded (unit=%s)", unit)
                return
    logger.error("Could not reload Samba using available service units")
    raise RuntimeError("Samba config was not applied; could not reload the Samba service")


def get_status() -> dict:
    """Get Samba service status (active, enabled, config path) plus host preflight."""
    samba = _samba_status()
    units = ("samba", "smbd")
    active_unit = None
    active = enabled = False
    for unit in units:
        try:
            state = subprocess.run(
                ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5,
            )
            if state.stdout.strip() == "active":
                active = True
                active_unit = unit
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("Could not read Samba active state (unit=%s)", unit, exc_info=True)
        try:
            state = subprocess.run(
                ["systemctl", "is-enabled", unit], capture_output=True, text=True, timeout=5,
            )
            enabled = enabled or state.stdout.strip() == "enabled"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("Could not read Samba enabled state (unit=%s)", unit, exc_info=True)

    return {
        "service": active_unit or "smbd",
        "active": active,
        "enabled": enabled,
        "config_path": SMB_CONF if os.path.exists(SMB_CONF) else None,
        # Host preflight: without these the app can neither validate nor apply
        # anything, so the UI must say so instead of showing a dead toggle.
        "samba_installed": samba["installed"],
        "missing_tools": samba["missing_tools"],
        "install_hint": samba["install_hint"],
    }
