"""EthOS NAS — Access Control service (UFW + failed logins + manual bans).

Wraps `ufw` and the system auth logs for the "Firewall & Access Control"
dashboard.

Privilege model
---------------
The uvicorn server runs as a dedicated unprivileged service user (e.g.
`ethos-nas`, see setup_scripts/setup_service.sh).
UFW refuses *all* commands (including `ufw status`) when not root, so
privileged commands are executed via `sudo -n` and require a restricted
NOPASSWD sudoers rule — see `setup_scripts/setup_service.sh`.

Manual bans
-----------
Manual IP bans are stored as UFW `deny from <ip>` rules tagged with the
comment `ethos-ban` so they survive reboots and are easy to list/remove
without touching the user's own rules.
"""

import gzip
import ipaddress
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("ethos-nas.services.access_control_service")

UFW_BIN = shutil.which("ufw") or "/usr/sbin/ufw"

# Comment tag marking rules created by the "Ban IP" feature
BAN_COMMENT = "ethos-ban"

SUDO_HINT = "Run setup_scripts/setup_service.sh as root to install the NOPASSWD rules."

_SYSLOG_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# udf status verbose: tokens that identify the action column
_UFW_VERBS = {"ALLOW", "DENY", "DROP", "REJECT", "LIMIT"}
_UFW_DIRS = {"IN", "OUT", "FWD"}

_ISO_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_SYSLOG_TS_RE = re.compile(
    r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})"
)

# Auth-failure patterns. Every pattern exposes named groups `user` and `ip`
# (ssh uses the client address, PAM uses `rhost` which is empty for local
# console / web-UI logins).
_LOGIN_PATTERNS = (
    {"type": "password", "re": re.compile(
        r"Failed password for (?:invalid user )?(?P<user>\S+) from (?P<ip>\S+) port")},
    {"type": "publickey", "re": re.compile(
        r"Failed publickey for (?P<user>\S+) from (?P<ip>\S+) port")},
    {"type": "invalid_user", "re": re.compile(
        r"Invalid user (?P<user>\S+) from (?P<ip>\S+)")},
    {"type": "max_attempts", "re": re.compile(
        r"maximum authentication attempts exceeded for (?:invalid user )?"
        r"(?P<user>\S+) from (?P<ip>\S+)")},
    # PAM failures cover web-UI / sudo / console logins, not just sshd.
    # Example: pam_unix(login:auth): authentication failure; logname= uid=0
    #          euid=0 tty= ruser= rhost=192.168.1.5  user=admin
    {"type": "pam", "re": re.compile(
        r"pam_unix\([^)]*\): authentication failure;.*?rhost=(?P<ip>\S*)\s+user=(?P<user>\S+)")},
)


# ── Privilege helpers ──

def _run(cmd: list, timeout: int = 15) -> str:
    """Run a command, prefixing `sudo -n` when not root.

    Raises RuntimeError with the stderr message (plus a hint when sudo
    rejected the command because no NOPASSWD rule exists).
    """
    if os.geteuid() != 0 and cmd and cmd[0] != "sudo":
        cmd = ["sudo", "-n"] + cmd
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out: {' '.join(cmd)}") from exc
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        err_low = err.lower()
        if "password is required" in err_low or "interactive authentication" in err_low:
            raise RuntimeError(f"{err} — {SUDO_HINT}")
        raise RuntimeError(err)
    return result.stdout.strip()


def _run_bytes(cmd: list, timeout: int = 15) -> bytes:
    """Like `_run` but returns raw stdout bytes (gz logs must not be decoded)."""
    if os.geteuid() != 0 and cmd and cmd[0] != "sudo":
        cmd = ["sudo", "-n"] + cmd
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out: {' '.join(cmd)}") from exc
    if result.returncode != 0:
        err = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(err or f"{cmd[0]} exited with {result.returncode}")
    return result.stdout


_WARNED_PATHS: set[str] = set()


def _warn_unreadable(path: Path, exc: Exception) -> None:
    """Log an unreadable log file once per process (it is polled every 5 s)."""
    key = str(path)
    if key in _WARNED_PATHS:
        return
    _WARNED_PATHS.add(key)
    logger.warning(
        "cannot read %s (%s) — falling back to journald; run "
        "setup_scripts/setup_service.sh to grant log access", path, exc,
    )


def _read_file(path: Path, max_bytes: int = 2 * 1024 * 1024) -> str:
    """Read up to `max_bytes` (tail) of a log file, transparently for .gz.

    Falls back to `sudo cat` when the file is not readable by the current
    user (e.g. the server user is not in the `adm` group). `.gz` content is
    decompressed here — `zcat` is *not* in the sudoers allow-list, so
    `sudo cat` + gzip is the only path that works for rotated logs.
    Returns "" when the file cannot be read at all; callers then fall back
    to journald.
    """
    try:
        if path.name.endswith(".gz"):
            # gzip streams cannot seek backwards cheaply; read all, keep tail
            with gzip.open(path, "rb") as fh:
                raw = fh.read()
        else:
            size = path.stat().st_size
            with open(path, "rb") as fh:
                if size > max_bytes:
                    fh.seek(size - max_bytes)
                raw = fh.read()
    except PermissionError:
        try:
            raw = _run_bytes(["cat", str(path)])
            if path.name.endswith(".gz"):
                raw = gzip.decompress(raw)
        except (RuntimeError, OSError, EOFError) as exc:
            _warn_unreadable(path, exc)
            return ""
    except (OSError, EOFError):
        return ""
    partial = len(raw) > max_bytes
    data = raw[-max_bytes:] if partial else raw
    lines = data.decode("utf-8", errors="replace").splitlines()
    if partial:
        # First line may be a partial line if we started mid-file
        lines = lines[1:] if len(lines) > 1 else []
    return "\n".join(lines)


# ── Timestamp parsing ──

def _parse_log_ts(line: str, now: datetime) -> datetime | None:
    """Parse a leading ISO-8601 or syslog timestamp; None when unparseable."""
    m = _ISO_TS_RE.match(line)
    if m:
        try:
            dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    m = _SYSLOG_TS_RE.match(line)
    if not m:
        return None
    month, day, hh, mm, ss = m.groups()
    dt = now.replace(year=now.year, month=_SYSLOG_MONTHS[month], day=int(day),
                     hour=int(hh), minute=int(mm), second=int(ss), microsecond=0)
    # Syslog has no year: assume current year, previous if that's in the future
    if dt > now + timedelta(hours=12):
        dt = dt.replace(year=now.year - 1)
    return dt


# ── UFW ──

def ufw_installed() -> bool:
    return Path(UFW_BIN).exists() or shutil.which("ufw") is not None


def _ufw(args: list, timeout: int = 15) -> str:
    return _run([UFW_BIN] + args, timeout=timeout)


def _parse_ufw_status(output: str) -> dict:
    enabled = "Status: active" in output
    logging_on = bool(re.search(r"^Logging:\s*on", output, re.MULTILINE))
    defaults = {}
    m = re.search(r"^Default:\s*(.+)$", output, re.MULTILINE)
    if m:
        for part in m.group(1).split(","):
            bits = part.strip().split()
            if len(bits) >= 2:
                defaults[bits[-1].strip("()")] = bits[0]
    rules = []
    for line in output.splitlines():
        s = line.strip()
        if not s or s.startswith(("Status:", "Logging:", "Default:", "New profiles:")):
            continue
        # Skip the column header rows: "To  Action  From" and the dash separator
        if not s.replace("-", "").replace(" ", "").replace(":", ""):
            continue
        if s.startswith(("To", "Chain")):
            continue
        # `ufw status numbered` prefixes every rule with "[ 12]"; strip it so
        # the same parser handles both output styles.
        stripped = re.sub(r"^\[\s*\d+\]\s*", "", s)
        s = stripped
        comment = ""
        if "#" in s:
            idx = s.index("#")
            comment = s[idx + 1:].strip()
            s = s[:idx].rstrip()
        ipv6 = "(v6)" in s
        s = s.replace("(v6)", " ")
        parts = s.split()
        if len(parts) < 3:
            continue
        i = 1
        action = ""
        direction = ""
        if i < len(parts) and parts[i] in _UFW_VERBS:
            action = parts[i].lower()
            i += 1
            if i < len(parts) and parts[i] in _UFW_DIRS:
                direction = parts[i].lower()
                i += 1
        from_part = " ".join(parts[i:]).strip()
        rules.append({
            # 1-based rule number as used by `ufw delete <num>` — numbering is
            # derived from the listing order, so it must never skip a rule.
            "num": len(rules) + 1,
            "to": parts[0],
            "action": action or "allow",
            "direction": direction,
            "from": from_part or "Anywhere",
            "ipv6": ipv6,
            "comment": comment,
        })
    return {"enabled": enabled, "logging": logging_on, "defaults": defaults, "rules": rules}


def ufw_status() -> dict:
    """Parse `ufw status verbose` into {enabled, logging, defaults, rules[]}."""
    out = _ufw(["status", "verbose"])
    return _parse_ufw_status(out)


def _validate_ip_or_cidr(value: str) -> str:
    try:
        net = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError as exc:
        raise RuntimeError(f"Invalid IP address or CIDR: {value!r}") from exc
    if net.prefixlen == 0:
        # `ufw deny from 0.0.0.0/0` would lock every client out, including
        # this web UI — always refuse a whole-address-space rule.
        raise RuntimeError(f"Refusing {value!r}: a /0 network blocks all traffic")
    return value.strip()


def _validate_port_range(port: str) -> str:
    m = re.fullmatch(r"(\d+)(?::(\d+))?", port or "")
    if not m:
        raise RuntimeError(f"Invalid port or port range: {port!r}")
    start = int(m.group(1))
    end = int(m.group(2) or m.group(1))
    if not (1 <= start <= 65535 and 1 <= end <= 65535 and start <= end):
        raise RuntimeError(f"Invalid port or port range: {port!r}")
    return port


def _sanitize_comment(comment: str) -> str:
    """Validate a UFW rule comment (shown by `ufw status` and used as a tag).

    Spaces are allowed (subprocess gets an argv list, no shell), but quotes,
    backslashes and control characters are rejected — they would corrupt
    /etc/ufw/user.rules. Invalid input raises instead of being dropped
    silently, which used to make typed comments disappear without a hint.
    """
    text = (comment or "").strip()
    if not text:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_.:, -]{1,64}", text):
        raise RuntimeError(
            "Invalid comment: use up to 64 characters from letters, digits, "
            "spaces, dot, comma, colon, _ and -")
    return text


def _rule_signature(rule: dict) -> tuple:
    return (rule["to"], rule["action"], rule["direction"], rule["from"],
            rule["ipv6"], rule["comment"])


def ufw_add_rule(action: str = "allow", protocol: str = "tcp", port: str = "",
                 source: str = "", comment: str = "") -> int:
    """Add a UFW rule. Returns the 1-based rule number of the new rule."""
    action = (action or "allow").lower()
    if action == "drop":
        # `ufw drop` is not a valid verb; UFW's equivalent is `deny` (DROP).
        action = "deny"
    if action not in ("allow", "deny", "reject", "limit"):
        raise RuntimeError(
            f"Invalid action: {action!r} (use allow|deny|reject|limit)")
    protocol = (protocol or "tcp").lower()
    if protocol not in ("tcp", "udp", "any", "icmp"):
        raise RuntimeError(f"Invalid protocol: {protocol!r} (use tcp|udp|any|icmp)")
    port = (port or "").strip()
    if port:
        _validate_port_range(port)
    source = _validate_ip_or_cidr((source or "").strip()) if (source or "").strip() else ""
    comment = _sanitize_comment(comment)

    before = [_rule_signature(r) for r in _read_rules_safe()]

    cmd = [action]
    if protocol == "icmp":
        # UFW has no `port/icmp` syntax — ICMP is a protocol, not a port.
        if port:
            raise RuntimeError("ICMP rules cannot specify a port")
        cmd.extend(["proto", "icmp"])
    else:
        spec = ""
        if port and protocol != "any":
            spec = f"{port}/{protocol}"
        elif port:
            spec = port
        if spec:
            cmd.append(spec)
    if source:
        cmd.extend(["from", source])
    if comment:
        cmd.extend(["comment", comment])
    # NB: no `--force` here — ufw 0.36.x rejects global options before
    # allow/deny/reject/limit ("Invalid syntax"); rule adds don't prompt anyway.
    _ufw(cmd, timeout=20)

    # Locate the new rule: UFW inserts rules by its own ordering rules, so
    # "the last one" is not reliable — compare the listing before/after.
    updated = _read_rules_safe()
    after = [_rule_signature(r) for r in updated]
    for idx in range(min(len(before), len(after))):
        if before[idx] != after[idx]:
            return updated[idx]["num"]
    if len(updated) > len(before):
        return updated[min(len(before), len(updated) - 1)]["num"]
    if updated:
        return updated[-1]["num"]
    logger.warning("ufw add rule: could not determine the new rule number for %s", cmd)
    return 0


def ufw_delete_rule(num: int) -> None:
    if not isinstance(num, int) or not (1 <= num <= 9999):
        raise RuntimeError(f"Invalid rule number: {num}")
    _ufw(["--force", "delete", str(num)], timeout=20)


def _read_rules_safe() -> list[dict]:
    """Rules from `ufw status`, or [] when UFW cannot be queried."""
    try:
        return ufw_status()["rules"]
    except RuntimeError as exc:
        logger.warning("ufw rules unreadable: %s", exc)
        return []


# Baseline allow rules ensured before `ufw enable` so that enabling the
# firewall never locks out web UI / app access. (Historically UFW was
# enabled with an empty ruleset → default-deny-incoming dropped port 443
# and remote phone login stopped working until UFW was disabled again.)
def _detect_lan_subnet() -> str:
    """Best-effort detection of the host's LAN subnet (assumes /24)."""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packet sent; selects default-route iface
        ip = s.getsockname()[0]
        s.close()
        return f"{ip.rsplit('.', 1)[0]}.0/24"
    except Exception:
        # Conservative RFC1918 default; the UI lets the operator edit it.
        return "192.168.1.0/24"


def _existing_rule_comments() -> set:
    """Comments of all configured UFW rules.

    While UFW is inactive `ufw status` hides user rules, so also scan
    /etc/ufw/user.rules directly (via sudo when needed)."""
    comments = set()
    try:
        st = ufw_status()
        comments.update(r["comment"] for r in st["rules"])
    except RuntimeError as exc:
        logger.warning("ufw status unavailable while checking baseline rules: %s", exc)
    for name in ("/etc/ufw/user.rules", "/etc/ufw/user6.rules"):
        try:
            raw = _run(["cat", name])
            comments.update(re.findall(r'--comment\s+"([^"]+)"', raw))
        except RuntimeError as exc:
            # Expected when the NOPASSWD allow-list predates this feature —
            # the duplicate check then falls back to `ufw status` only.
            logger.debug("cannot read %s: %s", name, exc)
    return comments


def ufw_ensure_baseline_rules() -> list[str]:
    """Idempotently add baseline allow rules (tagged by comment).

    Returns the comments of the rules that were actually added."""
    existing = _existing_rule_comments()
    baseline = [
        # (action, protocol, port, source, comment)
        ("allow", "tcp", "443", "", "ethos-web"),   # openresty: web UI + Photos API
        ("allow", "tcp", "9000", "", "ethos-api"),  # direct backend access
        ("allow", "any", "", _detect_lan_subnet(), "ethos-lan"),  # trusted LAN stays open
    ]
    added = []
    for action, protocol, port, source, comment in baseline:
        if comment not in existing:
            ufw_add_rule(action=action, protocol=protocol, port=port,
                         source=source, comment=comment)
            added.append(comment)
    return added


def ufw_set_enabled(enabled: bool) -> None:
    if enabled:
        ufw_ensure_baseline_rules()
    _ufw(["--force", "enable" if enabled else "disable"], timeout=20)
    invalidate_block_cache()


def ufw_set_logging(enabled: bool) -> None:
    """Enable/disable UFW packet logging (writes `UFW BLOCK` kernel events)."""
    _ufw(["logging", "on" if enabled else "off"], timeout=20)
    invalidate_block_cache()


# ── Failed logins ──

def _auth_log_files() -> list[Path]:
    base = Path("/var/log/auth.log")
    files = []
    if base.exists():
        files.append(base)
    try:
        rotated = sorted(Path("/var/log").glob("auth.log.[0-9]*"), reverse=True)
    except OSError:
        rotated = []
    files.extend(rotated)
    return files


def _journal_auth_since(cutoff: datetime) -> str:
    """journald fallback for auth logs (allowed by the NOPASSWD sudoers rule).

    Tries the ssh units first, then the sshd syslog identifiers (sshd-session
    on systemd >= 255). Returns "" when journald is unavailable.
    """
    since = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    base = ["journalctl", "--since", since, "--no-pager", "-o", "short-iso",
            "-n", "20000"]  # `-n` bounds 3-day windows to the newest 20k entries
    for extra in (["-u", "ssh", "-u", "sshd"],
                  ["SYSLOG_IDENTIFIER=sshd", "SYSLOG_IDENTIFIER=sshd-session"]):
        try:
            out = _run(base + extra, timeout=20)
        except RuntimeError as exc:
            logger.warning("failed logins: journalctl fallback failed: %s", exc)
            continue
        if out.strip():
            return out
    return ""


def get_failed_logins(hours: int = 24, limit: int = 50) -> dict:
    """Parse auth logs for failed login attempts (sshd + PAM).

    Returns {total_events, by_ip: [{ip, count, users: {user: n}, last_ts}],
    events: [{ts, ip, user, type}] (newest first, capped at `limit` when > 0)}.
    """
    if hours <= 0 or hours > 24 * 365:
        hours = 24
    now = datetime.now()
    cutoff = now - timedelta(hours=hours)
    seen: dict[tuple, dict] = {}

    def _add(ts: datetime, ip: str, user: str, etype: str) -> None:
        key = (ts, ip, user)
        if key in seen:
            return
        ev = {"ts": ts, "ip": ip, "user": user, "type": etype}
        seen[key] = ev

    def _scan(content: str) -> None:
        for line in content.splitlines():
            ts = _parse_log_ts(line, now)
            if ts is None or ts < cutoff:
                continue
            for pattern in _LOGIN_PATTERNS:
                m = pattern["re"].search(line)
                if m:
                    _add(ts, m.group("ip"), m.group("user"), pattern["type"])
                    break

    readable = False
    for path in _auth_log_files():
        content = _read_file(path)
        if not content:
            continue
        readable = True
        _scan(content)
    if not readable:
        # No readable auth.log (journal-only system, or service user without
        # log access) — journald is the only remaining source.
        _scan(_journal_auth_since(cutoff))

    # Aggregate per IP (local/console attempts have no source address)
    per_ip: dict[str, dict] = {}
    for ev in seen.values():
        if not ev["ip"]:
            continue
        entry = per_ip.setdefault(ev["ip"], {
            "ip": ev["ip"], "count": 0, "users": {}, "last_ts": ev["ts"],
        })
        entry["count"] += 1
        entry["users"][ev["user"]] = entry["users"].get(ev["user"], 0) + 1
        if ev["ts"] > entry["last_ts"]:
            entry["last_ts"] = ev["ts"]
    by_ip = sorted(per_ip.values(), key=lambda e: (-e["count"], -e["last_ts"].timestamp()))
    for entry in by_ip:
        entry["last_ts"] = entry["last_ts"].isoformat()

    events = sorted(seen.values(), key=lambda e: e["ts"], reverse=True)
    return {
        "total_events": len(seen),
        "by_ip": by_ip,
        "events": [
            {
                "ts": e["ts"].isoformat(),
                "ip": e["ip"],
                "user": e["user"],
                "type": e["type"],
            }
            for e in (events[:limit] if limit > 0 else [])
        ],
    }


# ── Firewall block log (UFW BLOCK kernel events) ──

# Common destination ports → human-readable service names for "what they hit"
_PORT_NAMES = {
    20: "ftp-data", 21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp",
    53: "dns", 80: "http", 110: "pop3", 111: "rpcbind", 135: "msrpc",
    139: "netbios", 143: "imap", 443: "https", 445: "smb", 587: "smtp",
    1433: "mssql", 1521: "oracle", 3306: "mysql", 3389: "rdp",
    5432: "postgres", 5900: "vnc", 6379: "redis", 8080: "http-alt",
    8443: "https-alt", 9100: "prometheus", 27017: "mongodb",
}

_ICMP_TYPES = {0: "echo-reply", 3: "unreachable", 5: "redirect", 8: "echo-request (ping)", 11: "rate-limit"}

_UFW_BLOCK_KV_RE = re.compile(r"(\w+)=(\S+)")


def _kernel_log_files() -> list[Path]:
    """Files where UFW BLOCK kernel events land, newest first.

    Prefers the kern.log family; falls back to syslog* only when no
    kern.log exists (avoids double-counting — rsyslog writes netfilter
    messages to both on many systems).
    """
    base = Path("/var/log/kern.log")
    if not base.exists():
        base = Path("/var/log/syslog")
    files: list[Path] = []
    if base.exists():
        files.append(base)
        try:
            rotated = sorted(Path("/var/log").glob(f"{base.name}.[0-9]*"), reverse=True)
        except OSError:
            rotated = []
        files.extend(rotated)
    return files


def _is_external_ip(ip: str) -> bool:
    """True for public (non-RFC1918/loopback/multicast/link-local) addresses."""
    try:
        addr = ipaddress.ip_address(ip.split("/")[0])
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_multicast
                or addr.is_link_local or addr.is_reserved)


def _journal_kernel_since(cutoff: datetime) -> str:
    """journald fallback for `UFW BLOCK` events (kernel ring buffer)."""
    try:
        return _run(
            ["journalctl", "-k", "--since", cutoff.strftime("%Y-%m-%d %H:%M:%S"),
             "--no-pager", "-o", "short-iso", "-n", "20000"],
            timeout=20,
        )
    except RuntimeError as exc:
        logger.warning("firewall log: journald fallback failed: %s", exc)
        return ""


# The Intrusion tab polls every 5 s; parsing up to 2 MB × N rotated logs each
# time is wasteful, especially when several browser tabs are open.
_BLOCK_CACHE_TTL = 5.0
_BLOCK_CACHE: dict[tuple, tuple[float, dict]] = {}


def invalidate_block_cache() -> None:
    """Drop cached `UFW BLOCK` results (called after rule changes)."""
    _BLOCK_CACHE.clear()


def get_firewall_blocks(hours: int = 24, limit: int = 50, external_only: bool = False,
                        ufw_state: dict | None = None) -> dict:
    """Parse `UFW BLOCK` kernel log events (dropped/rejected packets).

    Returns {total_events, logging_enabled, by_ip: [{ip, count, ports,
    first_ts, last_ts}], events: [{ts, ip, dst, proto, sport, dport,
    service, iface}]} — events newest-first, capped at `limit` when > 0.
    """
    if hours <= 0 or hours > 24 * 365:
        hours = 24
    cache_key = (hours, limit, external_only)
    cached = _BLOCK_CACHE.get(cache_key)
    if cached and (time.monotonic() - cached[0]) < _BLOCK_CACHE_TTL:
        return cached[1]

    now = datetime.now()
    cutoff = now - timedelta(hours=hours)
    events: list[dict] = []

    def _scan(content: str) -> None:
        for line in content.splitlines():
            if "UFW BLOCK" not in line:
                continue
            ts = _parse_log_ts(line, now)
            if ts is None or ts < cutoff:
                continue
            kv = dict(_UFW_BLOCK_KV_RE.findall(line))
            src = kv.get("SRC", "")
            if not src:
                continue
            proto = kv.get("PROTO", "").lower()
            dport = kv.get("DPT", "")
            service = ""
            if proto in ("icmp", "icmpv6"):
                service = _ICMP_TYPES.get(int(kv["TYPE"]) if kv.get("TYPE", "").isdigit() else -1, f"type={kv.get('TYPE', '?')}")
            elif dport.isdigit():
                service = _PORT_NAMES.get(int(dport), "")
            events.append({
                "ts": ts,
                "ip": src,
                "dst": kv.get("DST", ""),
                "proto": proto or "?",
                "sport": kv.get("SPT", ""),
                "dport": dport,
                "service": service,
                "iface": kv.get("IN", ""),
            })

    readable = False
    for path in _kernel_log_files():
        content = _read_file(path)
        if not content:
            continue
        readable = True
        _scan(content)
    if not readable:
        # No log file at all, or none the service user may read (kern.log is
        # root:adm 0640) — the kernel journal is the only remaining source.
        # Without this fallback the Intrusion tab silently reported "no
        # blocked packets" on systems where the service user is not in `adm`.
        _scan(_journal_kernel_since(cutoff))

    if external_only:
        events = [e for e in events if _is_external_ip(e["ip"])]

    # Aggregate per source IP
    per_ip: dict[str, dict] = {}
    for ev in events:
        entry = per_ip.setdefault(ev["ip"], {
            "ip": ev["ip"], "count": 0, "ports": {},
            "first_ts": ev["ts"], "last_ts": ev["ts"],
        })
        entry["count"] += 1
        if ev["dport"] or ev["service"]:
            key = f"{ev['proto']}:{ev['dport'] or '?'}"
            ports = entry["ports"]
            ports[key] = ports.get(key, 0) + 1
        if ev["ts"] < entry["first_ts"]:
            entry["first_ts"] = ev["ts"]
        if ev["ts"] > entry["last_ts"]:
            entry["last_ts"] = ev["ts"]
    by_ip = sorted(per_ip.values(), key=lambda e: (-e["count"], -e["last_ts"].timestamp()))
    for entry in by_ip:
        # ports: {"tcp:22": n} → [{port, proto, service, count}] with names
        named = []
        for key, n in sorted(entry["ports"].items(), key=lambda kv: -kv[1]):
            pproto, _, pnum = key.partition(":")
            name = _PORT_NAMES.get(int(pnum), "") if pnum.isdigit() else ""
            named.append({"port": pnum or "?", "proto": pproto, "service": name, "count": n})
        entry["ports"] = named
        entry["first_ts"] = entry["first_ts"].isoformat()
        entry["last_ts"] = entry["last_ts"].isoformat()

    events.sort(key=lambda e: e["ts"], reverse=True)
    if ufw_state is None:
        try:
            ufw_state = ufw_status()
        except RuntimeError:
            ufw_state = None
    result = {
        "total_events": len(events),
        # `logging_known` distinguishes "logging is off" from "UFW state could
        # not be read" — the UI must not claim logging is off in the latter case.
        "logging_known": ufw_state is not None,
        "logging_enabled": bool(ufw_state.get("logging", False)) if ufw_state else False,
        "by_ip": by_ip[:50],
        "events": [
            {**e, "ts": e["ts"].isoformat()}
            for e in (events[:limit] if limit > 0 else [])
        ],
    }
    _BLOCK_CACHE.clear()  # keep a single entry — the UI polls one query at a time
    _BLOCK_CACHE[cache_key] = (time.monotonic(), result)
    return result


# ── Manual IP bans (UFW rules tagged `ethos-ban`) ──

def _normalize_ip(value: str) -> str:
    """Canonical form for comparisons: host addresses lose their /32 or /128.

    UFW prints single hosts as `1.2.3.4` or `1.2.3.4/32` depending on version
    and rule source, so bans must be compared after normalisation — otherwise
    the same address can be banned twice and "unban" matches nothing.
    """
    try:
        net = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError:
        return value.strip()
    if net.num_addresses == 1:
        return str(net.network_address)
    return str(net)


def _first_ip(from_part: str) -> str:
    for token in (from_part or "").replace(",", " ").split():
        try:
            ipaddress.ip_network(token, strict=False)
        except ValueError:
            continue
        return _normalize_ip(token)
    return ""


def list_bans(rules: list[dict] | None = None) -> list[dict]:
    """List manual bans (UFW rules tagged with the `ethos-ban` comment).

    Raises RuntimeError when UFW cannot be queried — silently returning []
    made a broken sudo configuration look like "no bans" (and the UI then
    offered to re-ban already banned IPs).
    """
    if rules is None:
        rules = ufw_status()["rules"]
    bans = []
    for idx, r in enumerate(rules):
        if BAN_COMMENT in r["comment"].split():
            bans.append({"num": r.get("num", idx + 1), "ip": _first_ip(r["from"]) or r["from"]})
    return bans


def ban_ip(ip: str) -> dict:
    """Ban an IP by adding a tagged UFW deny rule. Idempotent per IP."""
    ip = _validate_ip_or_cidr(ip)
    key = _normalize_ip(ip)
    for b in list_bans():
        if _normalize_ip(b["ip"]) == key:
            return {"num": b["num"], "ip": ip, "already_banned": True}
    # `protocol="any"` → `ufw deny from <ip>` (all protocols). Banning only
    # tcp left UDP/ICMP traffic from the offender untouched.
    num = ufw_add_rule(action="deny", protocol="any", source=ip, comment=BAN_COMMENT)
    return {"num": num, "ip": ip}


def ban_ips(ips: list[str]) -> dict:
    """Ban multiple IPs (tagged UFW deny rules). Per-IP results, keeps going on errors."""
    existing = {_normalize_ip(b["ip"]) for b in list_bans()}
    results: list[dict] = []
    errors: list[dict] = []
    for raw in ips:
        try:
            ip = _validate_ip_or_cidr(raw)
        except RuntimeError as exc:
            errors.append({"ip": str(raw), "error": str(exc)})
            continue
        key = _normalize_ip(ip)
        if key in existing:
            results.append({"ip": ip, "already_banned": True})
            continue
        try:
            num = ufw_add_rule(action="deny", protocol="any", source=ip, comment=BAN_COMMENT)
            existing.add(key)
            results.append({"ip": ip, "num": num})
        except Exception as exc:  # noqa: BLE001 — report per-IP, keep going
            errors.append({"ip": ip, "error": str(exc)})
    return {
        "results": results,
        "errors": errors,
        "banned": sum(1 for r in results if not r.get("already_banned")),
        "already": sum(1 for r in results if r.get("already_banned")),
    }


def unban_ip(ip: str) -> int:
    """Remove all manual ban rules for an IP. Returns number removed."""
    ip = _validate_ip_or_cidr(ip)
    key = _normalize_ip(ip)
    removed = 0
    # Bounded: every iteration removes one rule by number, but a rule whose
    # number keeps shifting (or a no-op delete) must not spin forever.
    for _ in range(100):
        targets = [b for b in list_bans() if _normalize_ip(b["ip"]) == key]
        if not targets:
            break
        ufw_delete_rule(min(b["num"] for b in targets))
        removed += 1
    else:
        logger.warning("unban %s: stopped after 100 deletions — check UFW rules manually", ip)
    return removed
