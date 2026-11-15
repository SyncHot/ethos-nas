"""Unit tests — automatic IP banning (services/autoban_service.py).

Self-contained: no root, no UFW, no server. Every `ufw`/log call is
monkeypatched, and the state store points at a throwaway directory:

    .venv/bin/python tests/test_autoban.py

Covers the safety invariants that matter:
  • local/private/reserved addresses are never banned, and neither is the allowlist
  • thresholds, distinct-port requirement and per-sweep/day caps
  • dry-run never creates a rule or a state record
  • temporary bans expire, repeat offenders escalate, admin unban starts a cooldown
  • ban rules are prepended (`ufw insert 1`) so they beat broad allow rules
  • UFW rule tags are listed with their kind (manual vs auto)
"""
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ethos-autoban-")
# Must be set before any backend import: services/json_store reads DATA_DIR then.
os.environ["ETHOS_DATA_DIR"] = _TMP

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import access_control_service as acs  # noqa: E402
from backend.services import autoban_service as ab  # noqa: E402
from backend.services import json_store  # noqa: E402

PASSED = 0
FAILED = 0

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def _iso(minutes_ago: int = 1) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def _fresh_store():
    path = Path(_TMP) / "security_autoban_test.json"
    if path.exists():
        path.unlink()
    store = json_store.get(path, ab.default_state, version=1)
    store.save(ab.default_state())
    ab._STORE = store
    return store


def _config(**overrides) -> dict:
    cfg = ab.default_config()
    cfg.update(overrides)
    return cfg


def _install_fakes(events=None, blocks=None, rules=None, ban_calls=None, unban_calls=None):
    """Point every side-effecting dependency at in-memory data.

    `fake_ban` appends to the same rule list `fake_list_bans` returns, so
    `reconcile()` sees the rules the engine just created (as UFW would).
    """
    ab._LAN_CACHE.update({"at": 0.0, "net": None})
    ab.detect_lan_subnet = lambda: "192.168.7.0/24"
    ab.ufw_status = lambda: {"enabled": True, "logging": True, "defaults": {}, "rules": []}
    live_rules = [dict(rule) for rule in (rules or [])]

    def fake_list_bans(rules_in=None):
        if rules_in is not None:
            return list(rules_in)
        return [dict(rule) for rule in live_rules]

    login_events = events or []
    block_events = blocks or []
    ab.get_failed_logins = lambda hours=1, limit=50: {
        "total_events": len(login_events),
        "by_ip": [],
        "events": list(login_events),
    }
    ab.get_firewall_blocks = lambda hours=1, limit=50, external_only=False: {
        "total_events": len(block_events),
        "by_ip": [],
        "events": list(block_events),
        "logging_known": True,
        "logging_enabled": True,
    }

    def fake_ban(**kwargs):
        if ban_calls is not None:
            ban_calls.append(kwargs)
        live_rules.append({"kind": "auto", "ip": str(kwargs.get("source") or "")})
        return len(live_rules)

    def fake_unban(ip):
        if unban_calls is not None:
            unban_calls.append(ip)
        live_rules[:] = [rule for rule in live_rules if rule.get("ip") != ip]
        return 1

    ab.list_bans = fake_list_bans
    ab.ufw_add_rule = fake_ban
    ab.unban_ip = fake_unban


def _login_event(ip, minutes_ago=1, user="root", etype="password"):
    return {"ts": _iso(minutes_ago), "ip": ip, "user": user, "type": etype}


def _block_event(ip, dport, minutes_ago=1):
    return {"ts": _iso(minutes_ago), "ip": ip, "dport": str(dport),
            "proto": "tcp", "sport": "50000", "service": "", "iface": "eth0",
            "dst": "192.168.7.10"}


# ── Config / protection ──

def test_config_normalisation():
    print("• config normalisation & allowlist validation")
    cfg = ab.normalize_config({
        "enabled": True,
        "dry_run": False,
        "failed_logins": {"threshold": 0, "window_minutes": 99999},
        "allowlist": ["10.0.0.0/8", "nonsense", "0.0.0.0/0"],
        "max_active_bans": 99999,
    })
    check("enabled/dry_run are parsed", cfg["enabled"] is True and cfg["dry_run"] is False)
    check("threshold is clamped to >= 1", cfg["failed_logins"]["threshold"] == 1,
          str(cfg["failed_logins"]["threshold"]))
    check("window is clamped to <= 1440", cfg["failed_logins"]["window_minutes"] == 1440)
    check("invalid /0 allowlist entries are dropped on read",
          cfg["allowlist"] == ["10.0.0.0/8"], str(cfg["allowlist"]))
    check("active-ban cap is clamped", cfg["max_active_bans"] == 2000)

    _fresh_store()
    invalid = None
    try:
        ab.save_config({"allowlist": ["definitely-not-an-ip"]})
    except ValueError as exc:
        invalid = exc
    check("save rejects an invalid allowlist", invalid is not None)
    invalid = None
    try:
        ab.save_config({"allowlist": ["0.0.0.0/0"]})
    except ValueError as exc:
        invalid = exc
    check("save rejects a /0 allowlist", invalid is not None)


def test_protected_addresses():
    print("• protected address rules")
    _fresh_store()
    _install_fakes()
    protected = [
        "127.0.0.1", "10.1.2.3", "172.16.5.4", "192.168.7.20", "169.254.1.1",
        "100.64.0.5", "224.0.0.1", "0.0.0.0", "::1", "fe80::1", "fc00::1",
        "not-an-ip", "",
    ]
    for ip in protected:
        check(f"{ip or '(empty)'} is protected", ab.is_protected(ip) is True)
    check("public address is not protected", ab.is_protected("8.8.8.8") is False)
    check("allowlisted public address is protected",
          ab.is_protected("8.8.8.8", ["8.8.8.0/24"]) is True)
    check("documentation range is protected", ab.is_protected("203.0.113.5") is True)


# ── Candidate detection ──

def test_login_threshold():
    print("• failed-login candidates")
    store = _fresh_store()
    events = (
        [_login_event("8.8.8.8") for _ in range(6)]
        + [_login_event("1.1.1.1") for _ in range(2)]
        + [_login_event("192.168.7.50") for _ in range(9)]
        + [_login_event("8.8.4.4", minutes_ago=30) for _ in range(9)]
    )
    _install_fakes(events=events)
    cfg = _config()
    cfg["failed_logins"]["threshold"] = 5
    candidates = ab.collect_candidates(cfg, now=NOW, state=store.load(), existing=[])
    ips = [item["ip"] for item in candidates]
    check("only the over-threshold public IP is a candidate", ips == ["8.8.8.8"], str(ips))
    check("candidate carries a human reason", "failed login" in candidates[0]["reason"])
    check("events outside the window are ignored", "8.8.4.4" not in ips)
    check("LAN address is never a candidate", "192.168.7.50" not in ips)


def test_block_candidates_need_distinct_ports():
    print("• UFW block flood candidates")
    store = _fresh_store()
    three_ports = [_block_event("9.9.9.9", 20 + (i % 3)) for i in range(50)]
    five_ports = [_block_event("4.2.2.1", 80 + (i % 5)) for i in range(50)]
    _install_fakes(blocks=three_ports + five_ports)
    cfg = _config()
    cfg["failed_logins"]["enabled"] = False
    cfg["ufw_blocks"].update({"enabled": True, "threshold": 40, "min_ports": 5})
    candidates = ab.collect_candidates(cfg, now=NOW, state=store.load(), existing=[])
    ips = [item["ip"] for item in candidates]
    check("flood on too few ports is ignored", "9.9.9.9" not in ips, str(ips))
    check("flood across enough ports is a candidate", ips == ["4.2.2.1"], str(ips))
    check("reason names the port count", "5 port" in candidates[0]["reason"])


# ── Sweep behaviour ──

def test_dry_run_has_no_side_effects():
    print("• dry-run")
    store = _fresh_store()
    ban_calls = []
    _install_fakes(events=[_login_event("8.8.8.8") for _ in range(6)], ban_calls=ban_calls)
    result = ab.run_sweep(apply=False, now=NOW)
    state = store.load()
    check("candidates are reported", len(result["candidates"]) == 1)
    check("no UFW rule is created", ban_calls == [])
    check("no ban is recorded", state["bans"] == {})


def test_apply_creates_temporary_ban():
    print("• apply + temporary ban")
    store = _fresh_store()
    ban_calls = []
    _install_fakes(events=[_login_event("8.8.8.8") for _ in range(6)], ban_calls=ban_calls)
    result = ab.run_sweep(apply=True, now=NOW)
    state = store.load()
    record = state["bans"].get("8.8.8.8")
    check("one rule is created", len(ban_calls) == 1, str(ban_calls))
    check("rule is an all-protocol deny tagged ethos-autoban",
          ban_calls[0].get("action") == "deny"
          and ban_calls[0].get("protocol") == "any"
          and ban_calls[0].get("comment") == "ethos-autoban")
    check("rule is prepended", ban_calls[0].get("position") == 1)
    check("ban record exists with an expiry", bool(record and record.get("expires_at")))
    check("first ban lasts 60 min", record and record["minutes"] == 60, str(record))
    check("history records the ban", state["history"][-1]["event"] == "banned")
    check("result reports the ban", result["banned"][0]["ip"] == "8.8.8.8")

    # A second sweep must not duplicate the rule.
    second = ab.run_sweep(apply=True, now=NOW + timedelta(minutes=1))
    check("already banned IP is not banned twice", len(ban_calls) == 1)
    check("second sweep has no candidates", second["candidates"] == [])


def test_escalation_after_expiry():
    print("• expiry + escalation")
    store = _fresh_store()
    ban_calls = []
    unban_calls = []
    _install_fakes(events=[_login_event("8.8.8.8") for _ in range(6)],
                   ban_calls=ban_calls, unban_calls=unban_calls)
    ab.run_sweep(apply=True, now=NOW)

    state = store.load()
    state["bans"]["8.8.8.8"]["expires_at"] = (NOW - timedelta(minutes=1)).isoformat()
    store.save(state)

    _install_fakes(events=[_login_event("8.8.8.8") for _ in range(6)],
                   ban_calls=[], unban_calls=unban_calls)
    result = ab.run_sweep(apply=True, now=NOW)
    state = store.load()
    check("expired ban is removed from UFW", unban_calls == ["8.8.8.8"], str(unban_calls))
    check("result reports the expiry", [item["ip"] for item in result["expired"]] == ["8.8.8.8"])
    check("repeat offender is banned again with 24 h", state["bans"]["8.8.8.8"]["minutes"] == 1440,
          str(state["bans"].get("8.8.8.8")))
    check("strike count is 2", state["bans"]["8.8.8.8"]["strikes"] == 2)


def test_caps_and_protection():
    print("• per-sweep cap and protected addresses")
    store = _fresh_store()
    ban_calls = []
    public = ["8.8.8.8", "1.1.1.1", "9.9.9.9", "208.67.222.222", "4.2.2.1",
              "64.6.64.6", "77.88.8.8", "84.200.69.80", "1.0.0.1", "8.8.4.4",
              "208.67.220.220", "9.9.9.10", "149.112.112.112", "185.228.168.9",
              "76.76.19.19"]
    events = [_login_event(ip) for ip in public for _ in range(6)]
    events += [_login_event("192.168.7.99") for _ in range(20)]
    _install_fakes(events=events, ban_calls=ban_calls)
    cfg = _config()
    cfg["max_bans_per_sweep"] = 3
    store.update(lambda state: state.update({"config": cfg}))
    result = ab.run_sweep(apply=True, now=NOW)
    check("only the per-sweep cap is applied", len(result["banned"]) == 3, str(result["banned"]))
    check("the rest are reported as skipped", len(result["skipped"]) == 12, str(len(result["skipped"])))
    check("protected LAN is skipped", all(ban["ip"] != "192.168.7.99" for ban in result["banned"]))
    check("UFW never sees the LAN address",
          all(call.get("source") != "192.168.7.99" for call in ban_calls))


def test_unban_cooldown():
    print("• admin unban + cooldown")
    store = _fresh_store()
    _install_fakes(events=[_login_event("8.8.8.8") for _ in range(6)])
    ab.run_sweep(apply=True, now=NOW)

    result = ab.unban("8.8.8.8")
    state = store.load()
    check("unban removed the rule", result["was_auto"] is True)
    check("auto ban record is gone", "8.8.8.8" not in state["bans"])
    check("cooldown is stored", "8.8.8.8" in state["cooldowns"])

    candidates = ab.collect_candidates(_config(), now=NOW, state=state, existing=[])
    check("cooldown blocks an immediate re-ban",
          all(item["ip"] != "8.8.8.8" for item in candidates), str(candidates))
    state["cooldowns"]["8.8.8.8"] = (NOW - timedelta(minutes=1)).isoformat()
    candidates = ab.collect_candidates(_config(), now=NOW, state=state, existing=[])
    check("cooldown expires", any(item["ip"] == "8.8.8.8" for item in candidates),
          str(candidates))


def test_reconcile_drops_vanished_rules():
    print("• reconcile with UFW")
    store = _fresh_store()
    state = ab.default_state()
    state["bans"]["8.8.8.8"] = {"ip": "8.8.8.8", "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                               "minutes": 60, "reason": "x", "strikes": 1}
    store.save(state)
    kept = ab.reconcile(state, [{"kind": "auto", "ip": "8.8.8.8"}])
    check("present rule keeps the record", kept == [] and "8.8.8.8" in state["bans"])
    vanished = ab.reconcile(state, [])
    check("missing rule drops the record", vanished == ["8.8.8.8"] and state["bans"] == {})


# ── UFW integration points ──

def test_ban_listing_kinds():
    print("• ban rule tags")
    rules = [
        {"num": 1, "from": "1.2.3.4", "comment": "ethos-ban"},
        {"num": 2, "from": "5.6.7.8", "comment": "ethos-autoban"},
        {"num": 3, "from": "Anywhere", "comment": "ethos-web"},
    ]
    bans = acs.list_bans(rules)
    kinds = {ban["ip"]: ban["kind"] for ban in bans}
    check("manual ban is listed", kinds.get("1.2.3.4") == "manual", str(kinds))
    check("auto ban is listed", kinds.get("5.6.7.8") == "auto", str(kinds))
    check("non-ban rules are ignored", "Anywhere" not in kinds)


def test_ufw_insert_position():
    print("• prepended UFW rule")
    captured = {}

    def fake_ufw(args, timeout=15):
        captured["cmd"] = list(args)
        return ""

    original_run = acs._ufw
    original_rules = acs._read_rules_safe
    try:
        acs._ufw = fake_ufw
        acs._read_rules_safe = lambda: []
        acs.ufw_add_rule(action="deny", protocol="any", source="8.8.8.8",
                         comment="ethos-autoban", position=1)
    finally:
        acs._ufw = original_run
        acs._read_rules_safe = original_rules
    cmd = captured.get("cmd") or []
    check("command starts with `insert 1`", cmd[:2] == ["insert", "1"], str(cmd))
    check("command is a deny from the address", "deny" in cmd and "8.8.8.8" in cmd)
    check("comment tag is included", "ethos-autoban" in cmd)


def main() -> int:
    print("Auto-ban service tests\n")
    try:
        test_config_normalisation()
        test_protected_addresses()
        test_login_threshold()
        test_block_candidates_need_distinct_ports()
        test_dry_run_has_no_side_effects()
        test_apply_creates_temporary_ban()
        test_escalation_after_expiry()
        test_caps_and_protection()
        test_unban_cooldown()
        test_reconcile_drops_vanished_rules()
        test_ban_listing_kinds()
        test_ufw_insert_position()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
