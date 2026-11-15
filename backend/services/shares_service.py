"""EthOS NAS — Samba Share Management Service

Manages Samba shares through /etc/samba/smb.conf.
Uses configparser for safe parsing and testparm for validation.
"""

import configparser
import grp
import logging
import os
import pwd
import re
import subprocess
from typing import Optional

from backend.services import system_deps

logger = logging.getLogger("ethos-nas.services.shares_service")

SMB_CONF = "/etc/samba/smb.conf"

# Share names become [section] headers in smb.conf — anything beyond plain
# alphanumerics/underscore/dash/dot can corrupt the file or inject directives.
_SHARE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,64}$")


class SambaUnavailable(RuntimeError):
    """Samba is not installed on this host — nothing can be validated or applied.

    Carries an operator-facing message with the exact install command, so the
    router can answer 503 instead of leaking a raw ``[Errno 2]`` from subprocess.
    """

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


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
    read_only = config.get(name, "read only", fallback="no").lower() == "yes"
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
    config = configparser.ConfigParser()
    if os.path.exists(SMB_CONF):
        config.read(SMB_CONF)
    return config


def get_shares() -> list[dict]:
    """List all Samba shares (excluding special [global], [homes], [printers], [print$])."""
    config = _load_config()
    shares = []
    skip_sections = {"global", "homes", "printers", "print$"}
    for section in config.sections():
        if section.lower() in skip_sections:
            continue
        share = {
            "name": section,
            "path": config.get(section, "path", fallback=""),
            "comment": config.get(section, "comment", fallback=""),
            "read_only": config.get(section, "read only", fallback="no").lower() == "yes",
            "browsable": config.get(section, "browseable", fallback="yes").lower() == "yes",
            "guest_ok": config.get(section, "guest ok", fallback="no").lower() == "yes",
            "valid_users": config.get(section, "valid users", fallback=""),
            "writable": config.get(section, "writable", fallback="yes").lower() == "yes",
            "create_mask": config.get(section, "create mask", fallback=""),
            "directory_mask": config.get(section, "directory mask", fallback=""),
        }
        shares.append(share)
    shares.sort(key=lambda s: s["name"])
    return shares


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
    if not os.path.exists(path):
        raise ValueError(f"Path does not exist: {path}")
    require_samba()

    config = _load_config()
    if name in config.sections():
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
    if valid_users:
        config[name]["valid users"] = valid_users

    _apply_guest_write_fix(config, name)

    _write_config(config)
    _reload_samba()
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
    if name not in config.sections():
        raise ValueError(f"Share '{name}' not found")

    if path is not None:
        if not os.path.exists(path):
            raise ValueError(f"Path does not exist: {path}")
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
        config[name]["valid users"] = valid_users
    if create_mask is not None:
        config[name]["create mask"] = create_mask
    if directory_mask is not None:
        config[name]["directory mask"] = directory_mask

    # Self-heal: re-apply force user/group to the (possibly new) path owner so
    # public writable shares keep working after edits.
    _apply_guest_write_fix(config, name)

    _write_config(config)
    _reload_samba()
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
    if name not in config.sections():
        raise ValueError(f"Share '{name}' not found")

    config.remove_section(name)
    _write_config(config)
    _reload_samba()
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


def _write_config(config: configparser.ConfigParser) -> None:
    """Write config to smb.conf.

    The serialized content is validated with testparm BEFORE the live file is
    replaced, so a rejected change can never leave a broken smb.conf behind.
    Tries direct write first (works when running as root), falls back to
    `sudo tee` for unprivileged users. Requires NOPASSWD sudo rule for the
    fallback path — see setup_scripts/setup_service.sh.
    """
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

    # Serialize config to string
    import io

    buf = io.StringIO()
    config.write(buf, space_around_delimiters=True)
    content = buf.getvalue()

    # Validate BEFORE committing — a failed testparm must not corrupt the live file.
    _validate_smb_content(content)

    # Strategy 1: Try direct write (works when running as root)
    try:
        with open(SMB_CONF + ".tmp", "w") as f:
            f.write(content)
        os.rename(SMB_CONF + ".tmp", SMB_CONF)
        logger.info("Samba config written to %s (direct)", SMB_CONF)
        return
    except PermissionError:
        pass  # Fall through to sudo tee

    # Strategy 2: Use sudo tee for unprivileged users
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


def _reload_samba() -> None:
    """Reload Samba configuration without restarting the service.

    Tries direct systemctl first (works when running as root), falls back
    to `sudo systemctl` for unprivileged users. Requires NOPASSWD sudo rule
    — see setup_scripts/setup_service.sh.
    """
    try:
        # First validate config with testparm (read-only, no sudo needed)
        result = subprocess.run(
            ["testparm", "-s", SMB_CONF],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.error("Samba config validation failed: %s", result.stderr)
            raise ValueError(f"Invalid Samba config: {result.stderr}")

        # Reload Samba — prefer samba.service (unified), fall back to smbd
        service_name = "samba" if os.path.exists("/usr/lib/systemd/system/samba.service") else "smbd"

        # Strategy 1: Try direct systemctl (works when running as root)
        result = subprocess.run(
            ["systemctl", "reload", service_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info("Samba configuration reloaded (direct)")
            return

        # Strategy 2: Fall back to sudo systemctl for unprivileged users
        result = subprocess.run(
            ["sudo", "-n", "systemctl", "reload", service_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info("Samba configuration reloaded (via sudo)")
            return

        # Both failed — warn but don't raise (config is still saved)
        logger.warning(
            "Failed to reload Samba via systemctl or sudo: %s. Config saved but not applied.",
            result.stderr,
        )
    except FileNotFoundError:
        logger.warning("Samba tools not found — config saved but not applied")
    except Exception as e:
        logger.warning("Failed to reload Samba: %s", e)


def get_status() -> dict:
    """Get Samba service status (active, enabled, config path) plus host preflight."""
    samba = _samba_status()
    smbd_active = False
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "smbd"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        smbd_active = result.stdout.strip() == "active"
    except Exception:
        logger.warning("Could not read smbd active state", exc_info=True)

    smbd_enabled = False
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", "smbd"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        smbd_enabled = result.stdout.strip() == "enabled"
    except Exception:
        logger.warning("Could not read smbd enabled state", exc_info=True)

    return {
        "service": "smbd",
        "active": smbd_active,
        "enabled": smbd_enabled,
        "config_path": SMB_CONF if os.path.exists(SMB_CONF) else None,
        # Host preflight: without these the app can neither validate nor apply
        # anything, so the UI must say so instead of showing a dead toggle.
        "samba_installed": samba["installed"],
        "missing_tools": samba["missing_tools"],
        "install_hint": samba["install_hint"],
    }
