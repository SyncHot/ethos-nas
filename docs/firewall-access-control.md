# Firewall & Access Control — audit & fixes

App: **Firewall & Access Control** (`frontend/static/apps/access-control.js`, registry id `security`)
Backend: `backend/routers/access_control.py` → `/api/security/*`, `backend/services/access_control_service.py` (UFW, auth logs, bans).
Sudo: `setup_scripts/setup_service.sh` (NOPASSWD for `ufw`, `journalctl`, `cat` on specific log files).

## Status

- [x] **P0 — UFW BLOCK events invisible in production.** `kern.log`/`syslog` are `root:adm 0640`
      and the service user was neither in `adm` nor allowed to `sudo cat` them. `_read_file()`
      returned `""` and the journald fallback only ran when *no* log file existed → the
      Intrusion Attempts tab always showed "No blocked packets" (measured: 0 vs 3 800+ events).
      Fix: journald fallback whenever the files are unreadable (`journalctl -k`, bounded with
      `-n 20000`), one-time `logger.warning` instead of silent `""`, `adm` group + `cat`
      allow-list entries for `kern.log`/`syslog` in the installer.
- [x] **P0 — rotated `.gz` auth logs unreadable.** Fallback used `zcat`, which is not in the
      sudoers allow-list. Now `sudo cat` + `gzip.decompress` in Python.
- [x] **P0 — bans only covered TCP.** `ban_ip()` created `deny … proto tcp`, so UDP/ICMP from a
      banned host still passed. Now `ufw deny from <ip>` (all protocols).
- [x] **P0 — ban identity mismatches.** UFW reports hosts as `1.2.3.4` or `1.2.3.4/32` depending
      on version; comparisons now normalize `/32` and `/128` (prevents duplicate bans and
      "unban removes nothing").
- [x] **P0 — unban could loop forever.** `unban_ip()` had `while True` + delete-by-number; now
      bounded to 100 iterations with a warning.
- [x] **P0 — DELETE /bans/{ip} 404 for CIDR bans.** Path param → `{ip:path}`.
- [x] **P0 — split firewall control.** The Network app's "Firewall" tab drove raw `iptables`
      (`-P INPUT DROP/ACCEPT`), bypassing UFW. Tab is now read-only and links to this app; the
      `POST/DELETE /api/network/firewall*` endpoints answer **409** with a pointer to UFW.
- [x] **P1 — UI/backend option mismatch.** UI offered `log` (rejected) and `drop` (not a UFW
      verb), backend rejected `icmp`. Now: `allow|deny|reject|limit` + `tcp|udp|any|icmp`
      (`icmp` → `ufw <action> proto icmp`), legacy `drop` mapped to `deny`.
- [x] **P1 — rule numbering.** `num` is returned per rule and used by the UI for
      `ufw delete <num>`; the new rule's number is found by diffing the listing before/after
      (UFW inserts by specificity, so "last rule" was wrong).
- [x] **P1 — silent "no bans".** `list_bans()` swallowed `RuntimeError`; a broken sudo config
      looked like "No active bans" and the UI offered to re-ban. Errors now surface.
- [x] **P1 — blocking calls in async handlers.** All UFW/log work runs via
      `asyncio.to_thread()`; `UFW BLOCK` results are cached for 5 s (the tab polls every 5 s)
      and invalidated on rule/logging changes.
- [x] **P1 — audit trail & notifications.** Every mutation logs via the ethos logger and emits a
      Notification Center event (`source="firewall"`).
- [x] **P1 — comment validation.** Comments with spaces were silently dropped; they are now
      accepted (quotes/newlines rejected with an explicit error).
- [x] **P1 — lockout guard.** `0.0.0.0/0` / `::/0` ban requests are refused; deleting a baseline
      rule (`ethos-web`, `ethos-api`, `ethos-lan`) requires an explicit warning dialog.
- [x] **P1 — PAM failures counted.** Failed web/console/`sudo` logins now appear next to sshd
      failures (no source address → excluded from the per-IP table, still counted).
- [x] **P2 — overview cards.** Blocked packets (24 h) and active bans, plus a "logging is off"
      warning (only when UFW state is actually known).
- [x] **P2 — ban parity.** Failed Logins tab can ban/unban per IP, like Intrusion Attempts.

## Known follow-ups (not done)

- Roles are decorative: any authenticated user can change firewall state. Admin gating belongs
  in the global auth middleware, not in this router alone.
- Existing bans created before this fix are TCP-only; re-ban or add a one-time migration if
  all-protocol coverage matters for old entries.
- Baseline rule `ethos-lan` allows the whole detected `/24` (by design, to survive enabling
  UFW remotely). Narrow it in `/etc/ufw/user.rules` if the LAN is not trusted.

## Tests

```
.venv/bin/python tests/test_firewall_access_control.py
```

Covers status parsing/numbering, validators, ban normalization, command building, unban
bounding, the journald fallback, caching and PAM/sshd login parsing.
