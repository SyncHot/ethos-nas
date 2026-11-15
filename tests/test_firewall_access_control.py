"""Unit tests — Firewall & Access Control service helpers.

Self-contained: no root, no UFW, no running server. Everything that would
shell out to `ufw`/`journalctl` is monkeypatched, so this can run anywhere:

    .venv/bin/python tests/test_firewall_access_control.py

Covers the bugs fixed in the Firewall & Access Control audit:
  • UFW BLOCK / auth log fallback to journald when the log file is unreadable
  • rule numbering used for `ufw delete <num>`
  • ban rules covering all protocols, and ID normalization (/32, /128)
  • action/protocol validation matching what the UI offers
  • comment validation (no silent drops)
  • bounded unban loop
"""
import gzip
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import access_control_service as acs  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def raises(fn, exc=Exception):
    try:
        fn()
    except exc as e:
        return e
    except Exception as e:  # wrong exception type
        return None
    return None


STATUS_SAMPLE = """Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22                         ALLOW IN    192.168.1.0/24
139,445/tcp                ALLOW IN    192.168.1.0/24
443/tcp                    ALLOW IN    Anywhere                   # ethos-web
1.2.3.4                    DENY IN     Anywhere                   # ethos-ban
22/tcp                     LIMIT IN    Anywhere
66.66.66.0/24              DENY FWD    Anywhere
22/tcp (v6)                ALLOW IN    Anywhere (v6)
"""


def test_parse_status():
    print("• ufw status parsing")
    st = acs._parse_ufw_status(STATUS_SAMPLE)
    check("enabled / logging / defaults", st["enabled"] and st["logging"]
          and st["defaults"] == {"incoming": "deny", "outgoing": "allow", "routed": "disabled"},
          st["defaults"])
    nums = [r["num"] for r in st["rules"]]
    check("rule numbers are 1..n without gaps", nums == list(range(1, len(st["rules"]) + 1)), nums)
    check("LIMIT action parsed", str([r["action"] for r in st["rules"]]).count("limit") == 1,
          [r["action"] for r in st["rules"]])
    check("FWD direction parsed", any(r["direction"] == "fwd" for r in st["rules"]))
    check("comment stripped from `from`", any(r["comment"] == "ethos-web" and r["from"] == "Anywhere"
                                              for r in st["rules"]),
          [r["from"] for r in st["rules"]])
    check("ipv6 flag", st["rules"][-1]["ipv6"] is True, st["rules"][-1])

    numbered = "Status: active\n\nTo Action From\n-- ------ ----\n[ 3] 22/tcp ALLOW IN Anywhere\n"
    st2 = acs._parse_ufw_status(numbered)
    check("`ufw status numbered` prefix tolerated", st2["rules"][0]["to"] == "22/tcp"
          and st2["rules"][0]["num"] == 1, st2["rules"])


def test_validators():
    print("• validation")
    check("comment with spaces accepted", acs._sanitize_comment("allow ssh from office") == "allow ssh from office")
    check("empty comment ok", acs._sanitize_comment("  ") == "")
    check("comment with quote rejected", raises(lambda: acs._sanitize_comment('bad"quote'), RuntimeError) is not None)
    check("comment with newline rejected", raises(lambda: acs._sanitize_comment("a\nb"), RuntimeError) is not None)
    check("65-char comment rejected", raises(lambda: acs._sanitize_comment("a" * 65), RuntimeError) is not None)

    check("plain IP accepted", acs._validate_ip_or_cidr("1.2.3.4") == "1.2.3.4")
    check("CIDR accepted", acs._validate_ip_or_cidr("10.0.0.0/8") == "10.0.0.0/8")
    check("/0 rejected (lockout guard)", raises(lambda: acs._validate_ip_or_cidr("0.0.0.0/0"), RuntimeError) is not None)
    check("::/0 rejected", raises(lambda: acs._validate_ip_or_cidr("::/0"), RuntimeError) is not None)
    check("garbage rejected", raises(lambda: acs._validate_ip_or_cidr("not-an-ip"), RuntimeError) is not None)


def test_ip_normalization():
    print("• ban ID normalization")
    check("/32 → host", acs._normalize_ip("1.2.3.4/32") == "1.2.3.4")
    check("/128 → host", acs._normalize_ip("2001:db8::1/128") == "2001:db8::1")
    check("network preserved", acs._normalize_ip("10.0.0.0/8") == "10.0.0.0/8")
    check("_first_ip strips /32", acs._first_ip("1.2.3.4/32") == "1.2.3.4")
    check("_first_ip skips non-IP tokens", acs._first_ip("Anywhere") == "")


def _fake_ufw(store):
    cmds = []

    def _run_iptables(args, timeout=15):
        cmds.append(list(args))
        # Simulate UFW inserting the rule right after position 1.
        store["rules"].insert(1, {
            "num": 2, "to": "22/tcp", "action": "allow", "direction": "in",
            "from": "Anywhere", "ipv6": False, "comment": "",
        })
        for i, r in enumerate(store["rules"], start=1):
            r["num"] = i
        return ""

    return _run_iptables, cmds


def test_add_rule_commands():
    print("• ufw_add_rule command building")
    original_ufw, original_status = acs._ufw, acs.ufw_status
    try:
        store = {"rules": [
            {"num": 1, "to": "443/tcp", "action": "allow", "direction": "in",
             "from": "Anywhere", "ipv6": False, "comment": "ethos-web"},
            {"num": 2, "to": "9000/tcp", "action": "allow", "direction": "in",
             "from": "Anywhere", "ipv6": False, "comment": "ethos-api"},
        ]}
        fake, cmds = _fake_ufw(store)
        acs._ufw = fake
        acs.ufw_status = lambda: {"enabled": True, "logging": True, "defaults": {},
                                  "rules": store["rules"]}

        num = acs.ufw_add_rule(action="allow", protocol="tcp", port="22")
        check("tcp rule command", cmds[-1] == ["allow", "22/tcp"], cmds[-1])
        check("insert position detected (not 'last')", num == 2, num)

        acs.ufw_add_rule(action="drop", protocol="tcp", port="23")
        check("legacy `drop` mapped to `deny`", cmds[-1] == ["deny", "23/tcp"], cmds[-1])

        acs.ufw_add_rule(action="allow", protocol="icmp")
        check("icmp uses `proto icmp`", cmds[-1] == ["allow", "proto", "icmp"], cmds[-1])

        acs.ufw_add_rule(action="allow", protocol="udp", port="53",
                         source="192.168.1.10", comment="dns server")
        check("source + comment as separate argv items",
              cmds[-1] == ["allow", "53/udp", "from", "192.168.1.10", "comment", "dns server"],
              cmds[-1])

        check("icmp with port rejected",
              raises(lambda: acs.ufw_add_rule(action="allow", protocol="icmp", port="8"),
                     RuntimeError) is not None)
        check("`log` action rejected",
              raises(lambda: acs.ufw_add_rule(action="log", protocol="tcp", port="22"),
                     RuntimeError) is not None)
    finally:
        acs._ufw, acs.ufw_status = original_ufw, original_status


def test_bans():
    print("• bans")
    original_status, original_add = acs.ufw_status, acs.ufw_add_rule
    try:
        rules = [
            {"num": 1, "to": "443/tcp", "action": "allow", "direction": "in",
             "from": "Anywhere", "ipv6": False, "comment": "ethos-web"},
            {"num": 2, "to": "Anywhere", "action": "deny", "direction": "in",
             "from": "1.2.3.4/32", "ipv6": False, "comment": "ethos-ban"},
        ]
        acs.ufw_status = lambda: {"enabled": True, "logging": True, "defaults": {}, "rules": rules}

        expected = [{"num": 2, "ip": "1.2.3.4", "kind": "manual", "comment": "ethos-ban"}]
        bans = acs.list_bans()
        check("ban list normalizes /32", bans == expected, bans)
        check("list_bans accepts pre-fetched rules", acs.list_bans(rules) == expected)

        calls = []

        def _add(**kwargs):
            calls.append(kwargs)
            return 9

        acs.ufw_add_rule = _add
        result = acs.ban_ip("5.6.7.8")
        check("ban covers all protocols", calls and calls[-1]["protocol"] == "any", calls)
        check("ban returns rule number", result.get("num") == 9, result)

        again = acs.ban_ip("1.2.3.4")
        check("re-banning /32 form is idempotent", again.get("already_banned") is True, again)

        bulk = acs.ban_ips(["9.9.9.9", "1.2.3.4"])
        check("bulk ban reports already-banned", bulk["already"] == 1 and bulk["banned"] == 1, bulk)

        acs.ufw_status = lambda: (_ for _ in ()).throw(RuntimeError("sudo: password required"))
        err = raises(acs.list_bans, RuntimeError)
        check("list_bans surfaces errors (no silent 'no bans')", err is not None, err)
    finally:
        acs.ufw_status, acs.ufw_add_rule = original_status, original_add


def test_unban_bounded():
    print("• unban loop")
    original_list, original_delete = acs.list_bans, acs.ufw_delete_rule
    try:
        acs.list_bans = lambda rules=None: [{"num": 1, "ip": "1.2.3.4"}]
        acs.ufw_delete_rule = lambda num: None
        removed = acs.unban_ip("1.2.3.4")
        check("unban loop is bounded (no infinite hang)", removed == 100, removed)
    finally:
        acs.list_bans, acs.ufw_delete_rule = original_list, original_delete


def _journal_sample() -> str:
    """Two UFW BLOCK lines inside the 24 h window (relative to now)."""
    now = datetime.now()

    def stamp(minutes_ago: int) -> str:
        return (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    return (
        f"{stamp(1)} nasos kernel: [UFW BLOCK] IN=eth0 OUT= "
        "MAC=aa:bb SRC=45.155.205.233 DST=192.168.1.2 LEN=60 PROTO=TCP SPT=51111 DPT=22 "
        "WINDOW=64240 SYN\n"
        f"{stamp(2)} nasos kernel: [UFW BLOCK] IN=eth0 OUT= "
        "MAC=aa:bb SRC=192.168.1.77 DST=192.168.1.2 LEN=60 PROTO=TCP SPT=40000 DPT=445\n"
    )


def test_firewall_blocks_journal_fallback():
    print("• UFW BLOCK journald fallback")
    original_files = acs._kernel_log_files
    original_read = acs._read_file
    original_run = acs._run
    original_status = acs.ufw_status
    try:
        acs.invalidate_block_cache()
        acs._kernel_log_files = lambda: [Path("/var/log/kern.log")]
        acs._read_file = lambda path, max_bytes=0: ""      # unreadable (root:adm)
        acs._run = lambda cmd, timeout=15: _journal_sample()  # journalctl
        acs.ufw_status = lambda: {"enabled": True, "logging": True, "defaults": {}, "rules": []}

        data = acs.get_firewall_blocks(hours=24, limit=10)
        check("unreadable kern.log falls back to journald", data["total_events"] == 2,
              data["total_events"])
        ssh = next((r for r in data["by_ip"] if r["ip"] == "45.155.205.233"), None)
        check("port 22 named 'ssh'", bool(ssh) and ssh["ports"][0]["service"] == "ssh",
              data["by_ip"])

        cached = acs.get_firewall_blocks(hours=24, limit=10)
        check("result is cached for the 5 s poll", cached is data)
        acs.invalidate_block_cache()
        fresh = acs.get_firewall_blocks(hours=24, limit=10)
        check("cache can be invalidated", fresh is not data)

        external = acs.get_firewall_blocks(hours=24, limit=10, external_only=True)
        check("external-only filter", external["total_events"] == 1
              and external["by_ip"][0]["ip"] == "45.155.205.233", external["by_ip"])
    finally:
        acs._kernel_log_files = original_files
        acs._read_file = original_read
        acs._run = original_run
        acs.ufw_status = original_status
        acs.invalidate_block_cache()


def test_failed_logins():
    print("• failed logins")
    original_files = acs._auth_log_files
    tmp = Path(tempfile.mkdtemp(prefix="ethos-authlog-"))
    log = tmp / "auth.log"
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    stamp2 = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
    log.write_text(
        f"{stamp} nasos sshd[1]: Failed password for invalid user root from 203.0.113.5 port 5555 ssh2\n"
        f"{stamp2} nasos sshd-session[2]: pam_unix(login:auth): authentication failure; "
        f"logname= uid=0 euid=0 tty= ruser= rhost=198.51.100.7  user=admin\n"
        f"{stamp2} nasos sshd-session[3]: pam_unix(sudo:auth): authentication failure; "
        f"logname=marcin uid=1000 euid=0 tty=/dev/pts/0 ruser=marcin rhost=  user=marcin\n"
    )
    try:
        acs._auth_log_files = lambda: [log]
        data = acs.get_failed_logins(hours=24, limit=10)
        types = sorted({e["type"] for e in data["events"]})
        check("sshd + PAM failures counted", data["total_events"] == 3, data["events"])
        check("PAM failures typed as 'pam'", "pam" in types, types)
        check("by_ip skips address-less (console) attempts",
              {r["ip"] for r in data["by_ip"]} == {"203.0.113.5", "198.51.100.7"},
              data["by_ip"])
        check("old-format syslog timestamp support",
              acs._parse_log_ts("Nov  8 10:00:00 nasos sshd[1]: x", now) is not None)

        # gz rotation: file unreadable by us but readable through `sudo cat`
        gz = tmp / "auth.log.1.gz"
        with gzip.open(gz, "wb") as fh:
            fh.write((f"{stamp} nasos sshd[9]: Failed password for root "
                      f"from 192.0.2.44 port 4444 ssh2\n").encode())
        check("gz log is decompressed", "192.0.2.44" in acs._read_file(gz))
    finally:
        acs._auth_log_files = original_files


def main():
    print("Firewall & Access Control — unit checks\n")
    test_parse_status()
    test_validators()
    test_ip_normalization()
    test_add_rule_commands()
    test_bans()
    test_unban_bounded()
    test_firewall_blocks_journal_fallback()
    test_failed_logins()
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
