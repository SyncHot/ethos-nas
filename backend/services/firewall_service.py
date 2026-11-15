"""EthOS NAS — Firewall Management Service

Provides low-level iptables/nftables operations for the Firewall router.
Handles rule parsing, execution, and status queries.
"""

import hashlib
import logging
import os
import shutil
import subprocess

logger = logging.getLogger("ethos-nas.services.firewall_service")

SUDO_HINT = "Run setup_scripts/setup_service.sh as root to install the NOPASSWD rules."

_NFTABLES_AVAILABLE = None


def _with_sudo(cmd: list[str]) -> list[str]:
    """Prefix `sudo -n` when running as a non-root service user."""
    if os.geteuid() != 0 and cmd[0] != "sudo":
        return ["sudo", "-n"] + cmd
    return cmd


def _fail(result: subprocess.CompletedProcess, name: str) -> None:
    err = (result.stderr or result.stdout or "").strip()
    if "password is required" in err.lower() or "interactive authentication" in err.lower():
        raise RuntimeError(f"{name} error: {err} — {SUDO_HINT}")
    raise RuntimeError(f"{name} error: {err}")


def run_iptables(args: list[str]) -> str:
    """Run an iptables command and return stdout."""
    try:
        result = subprocess.run(
            _with_sudo([shutil.which("iptables") or "iptables"] + args),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            _fail(result, "iptables")
        return result.stdout.strip()
    except FileNotFoundError as exc:
        raise RuntimeError("iptables not found on system") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("iptables command timed out") from exc


def run_iptables_save() -> str:
    """Run iptables-save and return full ruleset."""
    try:
        result = subprocess.run(
            _with_sudo([shutil.which("iptables-save") or "iptables-save"]),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            _fail(result, "iptables-save")
        return result.stdout.strip()
    except FileNotFoundError as exc:
        raise RuntimeError("iptables-save not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("iptables-save command timed out") from exc


def check_nftables() -> bool:
    """Check if nftables is the primary firewall (instead of iptables)."""
    global _NFTABLES_AVAILABLE
    if _NFTABLES_AVAILABLE is not None:
        return _NFTABLES_AVAILABLE
    try:
        result = subprocess.run(
            ["which", "nft"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            result2 = subprocess.run(
                ["nft", "list", "ruleset"], capture_output=True, text=True, timeout=10
            )
            _NFTABLES_AVAILABLE = result2.returncode == 0
        else:
            _NFTABLES_AVAILABLE = False
    except Exception:
        _NFTABLES_AVAILABLE = False
    return _NFTABLES_AVAILABLE


def parse_rule_line(line: str) -> dict | None:
    """Parse a single iptables-save rule line into a dict."""
    if not line.startswith("-A "):
        return None

    rule = {
        # Stable id derived from the raw line so DELETE can match it.
        "id": hashlib.md5(line.encode()).hexdigest()[:8],
        "chain": "",
        "protocol": "all",
        "source": "0.0.0.0/0",
        "destination": "0.0.0.0/0",
        "dport": "",
        "sport": "",
        "action": "",
        "comment": "",
        "enabled": True,
        "raw": line,
    }

    parts = line.split()
    i = 0
    while i < len(parts):
        p = parts[i]
        if p == "-A" and i + 1 < len(parts):
            rule["chain"] = parts[i + 1]
            i += 2
        elif p in ("-p", "--protocol") and i + 1 < len(parts):
            rule["protocol"] = parts[i + 1]
            i += 2
        elif p in ("-s", "--source") and i + 1 < len(parts):
            rule["source"] = parts[i + 1]
            i += 2
        elif p in ("-d", "--destination") and i + 1 < len(parts):
            rule["destination"] = parts[i + 1]
            i += 2
        elif p == "--dport" and i + 1 < len(parts):
            rule["dport"] = parts[i + 1]
            i += 2
        elif p == "--sport" and i + 1 < len(parts):
            rule["sport"] = parts[i + 1]
            i += 2
        elif p in ("-j", "--jump") and i + 1 < len(parts):
            rule["action"] = parts[i + 1]
            i += 2
        elif p == "-m":
            i += 1
        elif p == "--comment" and i + 1 < len(parts):
            comment = parts[i + 1].strip('"')
            rule["comment"] = comment
            i += 2
        else:
            i += 1

    return rule
