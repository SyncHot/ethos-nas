"""EthOS NAS — automatic IP banning (UFW).

A tracked background loop reads failed-login events (and optionally `UFW BLOCK`
floods), counts them per source IP inside a configurable window and prepends a
temporary `deny from <ip>` rule tagged `ethos-autoban`.

Safety invariants
-----------------
* Only globally routable unicast addresses are banned. Loopback, RFC1918,
  link-local, CGNAT, ULA, multicast, reserved ranges, the operator allowlist
  and the detected LAN subnet are always refused.
* Auto-bans are single host addresses (/32, /128) — never a CIDR.
* Auto-bans expire; repeat offenders escalate (1 h → 24 h → 7 d).
* Hard caps: max active bans, new bans per sweep and per day.
* `dry_run` is the default: no rule is created and no state is written for
  candidates — the UI can preview exactly what would happen.

The engine never queues its own state: every mutable field lives in
`<DATA_DIR>/security_autoban.json` through `json_store`, so it survives a
restart and is safe against concurrent writers.
"""

import asyncio
import ipaddress
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.config import settings
from backend.services import json_store
from backend.services.access_control_service import (
    AUTOBAN_COMMENT,
    detect_lan_subnet,
    get_failed_logins,
    get_firewall_blocks,
    list_bans,
    ufw_add_rule,
    ufw_status,
    unban_ip,
)
from backend.services.task_registry import spawn

logger = logging.getLogger("ethos-nas.services.autoban_service")

SWEEP_SECONDS = 60
ESCALATION_MINUTES = (60, 1440, 10080)  # 1 h → 24 h → 7 d
COOLDOWN_MINUTES = 30
MAX_HISTORY = 500
MAX_STRIKES = 2000
MAX_COOLDOWNS = 2000
SCAN_EVENT_LIMIT = 5000
MAX_ALLOWLIST = 200

# Shown in the UI; `is_global` is the actual enforcement rule.
PROTECTED_NETWORKS = (
    {"label": "Loopback", "cidr": "127.0.0.0/8"},
    {"label": "Loopback (IPv6)", "cidr": "::1/128"},
    {"label": "Private (RFC1918)", "cidr": "10.0.0.0/8"},
    {"label": "Private (RFC1918)", "cidr": "172.16.0.0/12"},
    {"label": "Private (RFC1918)", "cidr": "192.168.0.0/16"},
    {"label": "Link-local", "cidr": "169.254.0.0/16"},
    {"label": "Link-local (IPv6)", "cidr": "fe80::/10"},
    {"label": "Carrier-grade NAT", "cidr": "100.64.0.0/10"},
    {"label": "Unique local (IPv6)", "cidr": "fc00::/7"},
    {"label": "Multicast", "cidr": "224.0.0.0/4"},
)


# ── Store ──

def default_config() -> dict:
    return {
        "enabled": False,
        "dry_run": True,
        "escalate": True,
        "failed_logins": {
            "enabled": True,
            "threshold": 5,
            "window_minutes": 10,
            "ban_minutes": 60,
        },
        "ufw_blocks": {
            "enabled": False,
            "threshold": 40,
            "window_minutes": 10,
            "ban_minutes": 60,
            "min_ports": 5,
        },
        "allowlist": [],
        "max_active_bans": 200,
        "max_bans_per_sweep": 10,
        "max_bans_per_day": 200,
    }


def default_state() -> dict:
    return {
        "config": default_config(),
        "bans": {},
        "strikes": {},
        "cooldowns": {},
        "history": [],
        "stats": {
            "last_run_at": None,
            "last_candidates": 0,
            "last_banned": 0,
            "day": "",
            "banned_today": 0,
        },
    }


_STORE = json_store.get(
    settings.DATA_DIR / "security_autoban.json",
    default_state,
    version=1,
)


# ── Small helpers ──

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _norm_ip(value: str) -> str:
    """Canonical host form, matching access_control_service._normalize_ip."""
    try:
        net = ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError:
        return str(value).strip()
    if net.num_addresses == 1:
        return str(net.network_address)
    return str(net)


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _allow_list(value: Any) -> list[str]:
    """Normalise an allowlist; invalid or /0 entries are dropped on read."""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            logger.warning("autoban: ignoring invalid allowlist entry %r", text)
            continue
        if net.prefixlen == 0:
            logger.warning("autoban: ignoring allowlist entry %r (whole address space)", text)
            continue
        out.append(str(net))
        if len(out) >= MAX_ALLOWLIST:
            break
    return out


def validate_allowlist(value: Any) -> list[str]:
    """Strict allowlist validation used by PUT; raises ValueError."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("allowlist must be a list of IP addresses or CIDRs")
    if len(value) > MAX_ALLOWLIST:
        raise ValueError(f"allowlist is limited to {MAX_ALLOWLIST} entries")
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid allowlist entry: {text!r}") from exc
        if net.prefixlen == 0:
            raise ValueError(f"Refusing {text!r}: a /0 allowlist disables all protection")
        out.append(str(net))
    return out


def normalize_config(raw: Any) -> dict:
    """Tolerant normalisation of a stored/partial config."""
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg
    cfg["enabled"] = _bool(raw.get("enabled"), cfg["enabled"])
    cfg["dry_run"] = _bool(raw.get("dry_run"), cfg["dry_run"])
    cfg["escalate"] = _bool(raw.get("escalate"), cfg["escalate"])
    for key in ("failed_logins", "ufw_blocks"):
        sub = raw.get(key)
        if not isinstance(sub, dict):
            continue
        merged = cfg[key]
        merged["enabled"] = _bool(sub.get("enabled"), merged["enabled"])
        merged["threshold"] = _int(sub.get("threshold"), merged["threshold"], 1, 1000)
        merged["window_minutes"] = _int(sub.get("window_minutes"),
                                        merged["window_minutes"], 1, 1440)
        merged["ban_minutes"] = _int(sub.get("ban_minutes"),
                                     merged["ban_minutes"], 5, 43200)
        if key == "ufw_blocks":
            merged["min_ports"] = _int(sub.get("min_ports"),
                                       merged["min_ports"], 1, 100)
    cfg["allowlist"] = _allow_list(raw.get("allowlist"))
    cfg["max_active_bans"] = _int(raw.get("max_active_bans"),
                                  cfg["max_active_bans"], 1, 2000)
    cfg["max_bans_per_sweep"] = _int(raw.get("max_bans_per_sweep"),
                                     cfg["max_bans_per_sweep"], 1, 100)
    cfg["max_bans_per_day"] = _int(raw.get("max_bans_per_day"),
                                   cfg["max_bans_per_day"], 1, 5000)
    return cfg


def current_config() -> dict:
    return normalize_config(_STORE.load().get("config"))


def save_config(raw: Any) -> dict:
    """Persist a validated config. Raises ValueError for an invalid allowlist."""
    cfg = normalize_config(raw)
    # Validate the raw entries: `_allow_list` drops bad input while reading,
    # but a PUT must reject it so the UI can show the error.
    cfg["allowlist"] = validate_allowlist((raw or {}).get("allowlist")
                                          if isinstance(raw, dict) else None)

    def _mutate(state: dict) -> dict:
        state["config"] = cfg
        return cfg

    _STORE.update(_mutate)
    logger.info("autoban: config saved (enabled=%s dry_run=%s window=%s/%s min)",
                cfg["enabled"], cfg["dry_run"],
                cfg["failed_logins"]["threshold"], cfg["failed_logins"]["window_minutes"])
    return cfg


# ── Protection rules ──

_LAN_CACHE: dict[str, Any] = {"at": 0.0, "net": None}


def _lan_network():
    now = time.monotonic()
    if now - float(_LAN_CACHE["at"]) < 300:
        return _LAN_CACHE["net"]
    try:
        net = ipaddress.ip_network(detect_lan_subnet(), strict=False)
    except (ValueError, RuntimeError) as exc:
        logger.warning("autoban: cannot detect the LAN subnet: %s", exc)
        net = None
    _LAN_CACHE["at"] = now
    _LAN_CACHE["net"] = net
    return net


def is_protected(ip: str, allowlist: list[str] | None = None) -> bool:
    """True when the address must never be auto-banned.

    Note: `ipaddress` marks documentation/benchmark ranges (203.0.113.0/24 …)
    as non-global too, which is the desired behaviour — they are never real
    offenders.
    """
    text = str(ip or "").strip()
    if not text:
        return True
    try:
        addr = ipaddress.ip_address(text.split("/")[0])
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    # `is_global` alone is not enough: IPv4 multicast (224.0.0.0/4) is
    # reported as global on CPython, so every non-unicast class is explicit.
    if (not addr.is_global or addr.is_private or addr.is_loopback
            or addr.is_link_local or addr.is_multicast or addr.is_reserved
            or addr.is_unspecified):
        return True
    lan = _lan_network()
    if lan is not None and addr in lan:
        return True
    for entry in allowlist or []:
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if addr in net:
            return True
    return False


def protected_networks() -> list[dict]:
    return list(PROTECTED_NETWORKS)


# ── Candidate detection ──

def _login_candidates(cfg: dict, now: datetime, result: dict) -> None:
    src = cfg["failed_logins"]
    if not src["enabled"]:
        return
    hours = max(1, math.ceil(src["window_minutes"] / 60))
    cutoff = now - timedelta(minutes=src["window_minutes"])
    try:
        data = get_failed_logins(hours=hours, limit=SCAN_EVENT_LIMIT)
    except Exception as exc:  # noqa: BLE001 — one bad source must not stop the sweep
        logger.warning("autoban: failed-login scan failed: %s", exc)
        return
    for event in data.get("events", []):
        ts = _parse_ts(event.get("ts"))
        ip = str(event.get("ip") or "").strip()
        if not ip or ts is None or ts < cutoff:
            continue
        entry = result.setdefault(ip, _empty_candidate(ip, ts))
        entry["logins"] += 1
        entry["first"] = min(entry["first"], ts)
        entry["last"] = max(entry["last"], ts)


def _block_candidates(cfg: dict, now: datetime, result: dict) -> None:
    src = cfg["ufw_blocks"]
    if not src["enabled"]:
        return
    hours = max(1, math.ceil(src["window_minutes"] / 60))
    cutoff = now - timedelta(minutes=src["window_minutes"])
    try:
        data = get_firewall_blocks(hours=hours, limit=SCAN_EVENT_LIMIT,
                                   external_only=True)
    except Exception as exc:  # noqa: BLE001 — one bad source must not stop the sweep
        logger.warning("autoban: firewall-block scan failed: %s", exc)
        return
    for event in data.get("events", []):
        ts = _parse_ts(event.get("ts"))
        ip = str(event.get("ip") or "").strip()
        if not ip or ts is None or ts < cutoff:
            continue
        entry = result.setdefault(ip, _empty_candidate(ip, ts))
        entry["blocks"] += 1
        port = str(event.get("dport") or "").strip()
        if port:
            entry["ports"].add(port)
        entry["first"] = min(entry["first"], ts)
        entry["last"] = max(entry["last"], ts)


def _empty_candidate(ip: str, ts: datetime) -> dict:
    return {"ip": ip, "logins": 0, "blocks": 0, "ports": set(),
            "first": ts, "last": ts}


def _cooldown_active(state: dict, ip: str, now: datetime) -> bool:
    until = _parse_ts((state.get("cooldowns") or {}).get(_norm_ip(ip)))
    return bool(until and until > now)


def collect_candidates(cfg: dict, *, now: datetime | None = None,
                       state: dict | None = None,
                       existing: list[dict] | None = None) -> list[dict]:
    """Return IPs that crossed a threshold, newest evidence first."""
    now = now or _now()
    # `json_store.load()` może zwrócić None dla pustego/uszkodzonego pliku —
    # normalizujemy raz, żeby dalsze `state.get(...)` i `_cooldown_active(state, …)`
    # nie operowały na Optional (i żeby logika nie różniła się od pustego stanu).
    state = state if state is not None else _STORE.load()
    if not isinstance(state, dict):
        state = {}
    cfg = normalize_config(cfg)

    pooled: dict[str, dict] = {}
    _login_candidates(cfg, now, pooled)
    _block_candidates(cfg, now, pooled)

    if existing is None:
        try:
            existing = list_bans()
        except RuntimeError as exc:
            logger.warning("autoban: cannot list UFW bans: %s", exc)
            existing = []
    banned = {_norm_ip(b["ip"]) for b in existing}
    banned.update(_norm_ip(ip) for ip in (state.get("bans") or {}))

    login_cfg = cfg["failed_logins"]
    block_cfg = cfg["ufw_blocks"]
    out: list[dict] = []
    for ip, entry in pooled.items():
        key = _norm_ip(ip)
        if key in banned:
            continue
        if is_protected(ip, cfg["allowlist"]):
            continue
        if _cooldown_active(state, ip, now):
            continue
        reasons = []
        if login_cfg["enabled"] and entry["logins"] >= login_cfg["threshold"]:
            reasons.append(f"{entry['logins']} failed login(s) in {login_cfg['window_minutes']} min")
        if (block_cfg["enabled"]
                and entry["blocks"] >= block_cfg["threshold"]
                and len(entry["ports"]) >= block_cfg["min_ports"]):
            reasons.append(
                f"{entry['blocks']} blocked packet(s) on {len(entry['ports'])} port(s) "
                f"in {block_cfg['window_minutes']} min")
        if not reasons:
            continue
        sources = []
        if entry["logins"]:
            sources.append("logins")
        if entry["blocks"]:
            sources.append("ufw_blocks")
        out.append({
            "ip": ip,
            "count": max(entry["logins"], entry["blocks"]),
            "logins": entry["logins"],
            "blocks": entry["blocks"],
            "ports": len(entry["ports"]),
            "sources": sources,
            "reason": " · ".join(reasons),
            "first_ts": _iso(entry["first"]),
            "last_ts": _iso(entry["last"]),
        })
    out.sort(key=lambda item: (-item["count"], item["ip"]))
    return out


# ── Ban lifecycle ──

def _ban_minutes(cfg: dict, candidate: dict, strikes: int) -> int:
    base = (cfg["failed_logins"]["ban_minutes"] if "logins" in candidate["sources"]
            else cfg["ufw_blocks"]["ban_minutes"])
    if cfg["escalate"] and strikes > 1:
        index = min(strikes - 1, len(ESCALATION_MINUTES) - 1)
        return max(base, ESCALATION_MINUTES[index])
    return base


def _record_history(state: dict, now: datetime, event: str, ip: str,
                    minutes: int | None = None, reason: str = "") -> None:
    history = state.setdefault("history", [])
    history.append({
        "ts": _iso(now),
        "event": event,
        "ip": ip,
        "minutes": minutes,
        "reason": reason,
    })
    if len(history) > MAX_HISTORY:
        del history[:-MAX_HISTORY]


def _trim_maps(state: dict) -> None:
    for key, limit in (("strikes", MAX_STRIKES), ("cooldowns", MAX_COOLDOWNS)):
        mapping = state.get(key)
        if not isinstance(mapping, dict) or len(mapping) <= limit:
            continue
        ordered = sorted(mapping.items(),
                         key=lambda kv: str(kv[1].get("last_ban") if isinstance(kv[1], dict) else kv[1]))
        state[key] = dict(ordered[-limit:])


def _apply(candidates: list[dict], cfg: dict, state: dict, now: datetime) -> dict:
    today = now.date().isoformat()
    stats = state.setdefault("stats", {})
    if stats.get("day") != today:
        stats["day"] = today
        stats["banned_today"] = 0

    active = len(state.get("bans") or {})
    day_left = max(0, cfg["max_bans_per_day"] - int(stats.get("banned_today") or 0))
    limit = min(cfg["max_bans_per_sweep"],
                max(0, cfg["max_active_bans"] - active),
                day_left)

    banned: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    for candidate in candidates:
        ip = candidate["ip"]
        if len(banned) >= limit:
            skipped.append({"ip": ip, "reason": "cap reached"})
            continue
        if is_protected(ip, cfg["allowlist"]):
            skipped.append({"ip": ip, "reason": "protected address"})
            continue
        strikes = int((state.get("strikes") or {}).get(ip, {}).get("count", 0)) + 1
        minutes = _ban_minutes(cfg, candidate, strikes)
        try:
            num = ufw_add_rule(action="deny", protocol="any", source=ip,
                               comment=AUTOBAN_COMMENT, position=1)
        except Exception as exc:  # noqa: BLE001 — report per IP, keep going
            logger.error("autoban: could not ban %s: %s", ip, exc)
            errors.append({"ip": ip, "error": str(exc)})
            continue

        expires = now + timedelta(minutes=minutes)
        state.setdefault("bans", {})[ip] = {
            "ip": ip,
            "banned_at": _iso(now),
            "expires_at": _iso(expires),
            "minutes": minutes,
            "strikes": strikes,
            "sources": candidate["sources"],
            "reason": candidate["reason"],
            "count": candidate["count"],
            "rule_num": num,
        }
        state.setdefault("strikes", {})[ip] = {
            "count": strikes,
            "last_ban": _iso(now),
        }
        (state.get("cooldowns") or {}).pop(ip, None)
        _record_history(state, now, "banned", ip, minutes, candidate["reason"])
        stats["banned_today"] = int(stats.get("banned_today") or 0) + 1
        banned.append({
            "ip": ip,
            "minutes": minutes,
            "expires_at": _iso(expires),
            "strikes": strikes,
            "reason": candidate["reason"],
            "count": candidate["count"],
        })
        logger.warning("autoban: banned %s for %d min (strike %d, %s)",
                       ip, minutes, strikes, candidate["reason"])

    return {"banned": banned, "skipped": skipped, "errors": errors}


def expire_bans(state: dict, now: datetime) -> list[dict]:
    """Remove expired auto-bans from UFW (best effort, retried next sweep)."""
    expired: list[dict] = []
    for ip, record in list((state.get("bans") or {}).items()):
        until = _parse_ts(record.get("expires_at"))
        if until is None or until > now:
            continue
        try:
            unban_ip(ip)
        except Exception as exc:  # noqa: BLE001 — keep the record for a retry
            logger.error("autoban: could not expire ban for %s: %s", ip, exc)
            continue
        del state["bans"][ip]
        _record_history(state, now, "expired", ip, record.get("minutes"),
                        record.get("reason", ""))
        expired.append({"ip": ip})
        logger.info("autoban: ban for %s expired after %s min",
                    ip, record.get("minutes"))
    return expired


def reconcile(state: dict, rules: list[dict]) -> list[str]:
    """Drop state entries whose UFW rule disappeared (manual cleanup)."""
    if not state.get("bans"):
        return []
    auto_rules = {_norm_ip(rule["ip"]) for rule in rules if rule.get("kind") == "auto"}
    if not auto_rules:
        # UFW may be disabled (rules hidden) — never wipe state on an empty read.
        try:
            enabled = ufw_status()["enabled"]
        except Exception:  # noqa: BLE001 — cannot know; keep the state
            enabled = False
        if not enabled:
            return []
    vanished = []
    for ip in list((state.get("bans") or {})):
        if _norm_ip(ip) not in auto_rules:
            del state["bans"][ip]
            vanished.append(ip)
            _record_history(state, _now(), "vanished", ip, reason="UFW rule not found")
            logger.warning("autoban: UFW rule for %s disappeared — state dropped", ip)
    return vanished


def run_sweep(*, apply: bool = True, now: datetime | None = None) -> dict:
    """One detection pass. `apply=False` is a pure preview (no UFW changes)."""
    now = now or _now()
    state = _STORE.load()
    cfg = normalize_config(state.get("config"))

    try:
        rules = list_bans()
    except RuntimeError as exc:
        logger.warning("autoban: UFW rules unavailable: %s", exc)
        rules = None

    expired = expire_bans(state, now)
    vanished = reconcile(state, rules) if rules is not None else []

    candidates = collect_candidates(cfg, now=now, state=state, existing=rules)

    if apply:
        outcome = _apply(candidates, cfg, state, now)
    else:
        outcome = {"banned": [], "skipped": [], "errors": []}

    stats = state.setdefault("stats", {})
    stats["last_run_at"] = _iso(now)
    stats["last_candidates"] = len(candidates)
    stats["last_banned"] = len(outcome["banned"])
    _trim_maps(state)
    _STORE.save(state)

    if outcome["banned"]:
        _notify("warning", "Auto-ban: IPs blocked",
                ", ".join(item["ip"] for item in outcome["banned"][:8])
                + (f" (+{len(outcome['banned']) - 8} more)"
                   if len(outcome["banned"]) > 8 else ""))
    if outcome["errors"]:
        _notify("error", "Auto-ban failed",
                f"{len(outcome['errors'])} IP(s) could not be blocked — check the UFW log.")

    return {
        "applied": apply,
        "dry_run": not apply,
        "candidates": candidates,
        "banned": outcome["banned"],
        "skipped": outcome["skipped"],
        "errors": outcome["errors"],
        "expired": expired,
        "vanished": vanished,
        "stats": dict(stats),
    }


def _notify(kind: str, title: str, message: str) -> None:
    """Publish one summary event per sweep (never breaks the sweep)."""
    try:
        from backend.services.notifications_service import emit_event

        emit_event(kind, title, message, source="firewall",
                   dedupe_key="autoban-sweep")
    except Exception:  # noqa: BLE001 — notifications are best-effort
        logger.warning("autoban: could not emit notification %r", title, exc_info=True)


def unban(ip: str) -> dict:
    """Admin unban: remove rules, drop the auto record and start a cooldown.

    The cooldown stops the engine from re-banning the same address a minute
    later while the operator is investigating.
    """
    now = _now()
    key = _norm_ip(ip)
    removed = unban_ip(ip)

    def _mutate(state: dict) -> dict:
        record = (state.get("bans") or {}).pop(key, None)
        if record is not None:
            state.setdefault("cooldowns", {})[key] = _iso(
                now + timedelta(minutes=COOLDOWN_MINUTES))
            _record_history(state, now, "unbanned", ip,
                            record.get("minutes"), record.get("reason", ""))
        _trim_maps(state)
        return {"removed": removed, "was_auto": record is not None}

    return _STORE.update(_mutate)


# ── Status for the UI ──

def status() -> dict:
    state = _STORE.load()
    cfg = normalize_config(state.get("config"))
    now = _now()
    active = []
    for ip, record in (state.get("bans") or {}).items():
        until = _parse_ts(record.get("expires_at"))
        active.append({
            **record,
            "ip": ip,
            "expires_in_seconds": max(0, int((until - now).total_seconds())) if until else 0,
        })
    active.sort(key=lambda item: item.get("expires_at") or "")
    try:
        ufw_enabled = ufw_status()["enabled"]
    except Exception as exc:  # noqa: BLE001 — status must render without UFW
        logger.warning("autoban: cannot read UFW state: %s", exc)
        ufw_enabled = None
    history = list(state.get("history") or [])[-20:]
    history.reverse()
    stats = dict(state.get("stats") or {})
    stats["active_bans"] = len(active)
    stats["banned_today"] = (int(stats.get("banned_today") or 0)
                             if stats.get("day") == now.date().isoformat() else 0)
    return {
        "config": cfg,
        "active": active,
        "history": history,
        "stats": stats,
        "ufw_enabled": ufw_enabled,
        "protected_networks": protected_networks(),
        "detected_lan": detect_lan_subnet(),
        "cooldown_minutes": COOLDOWN_MINUTES,
        "escalation_minutes": list(ESCALATION_MINUTES),
    }


def summary() -> dict:
    """Lightweight payload for /api/security/status (no UFW shell-out)."""
    state = _STORE.load()
    cfg = normalize_config(state.get("config"))
    today = _now().date().isoformat()
    stats = state.get("stats") or {}
    banned_today = int(stats.get("banned_today") or 0) if stats.get("day") == today else 0
    return {
        "enabled": cfg["enabled"],
        "dry_run": cfg["dry_run"],
        "active_bans": len(state.get("bans") or {}),
        "banned_today": banned_today,
        "last_run_at": stats.get("last_run_at"),
    }


# ── Background loop ──

_task: asyncio.Task | None = None


async def _loop() -> None:
    # Let the app finish starting and the log sources settle.
    try:
        await asyncio.sleep(30)
        while True:
            try:
                cfg = await asyncio.to_thread(current_config)
                if cfg["enabled"] and not cfg["dry_run"]:
                    await asyncio.to_thread(run_sweep, apply=True)
                elif cfg["enabled"]:
                    # Dry-run still refreshes candidates/stats for the UI.
                    await asyncio.to_thread(run_sweep, apply=False)
            except Exception as exc:  # noqa: BLE001 — the loop must survive
                logger.error("autoban: sweep failed: %s", exc, exc_info=True)
            await asyncio.sleep(SWEEP_SECONDS)
    except asyncio.CancelledError:
        raise


def start_autoban() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = spawn(_loop(), name="autoban-sweeper")
    logger.info("Auto-ban sweeper started (every %ds)", SWEEP_SECONDS)


async def stop_autoban() -> None:
    global _task
    task, _task = _task, None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass  # intentional: a failure here must not break shutdown
