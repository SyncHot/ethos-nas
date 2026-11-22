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
      (`-P INPUT DROP/ACCEPT`), bypassing UFW. The tab and the `/api/network/firewall*` endpoints
      (plus `firewall_service.py` and the iptables NOPASSWD rules) were removed entirely — this app
      is the only firewall surface. `ufw.service` is also protected in `/api/systemd`: start/stop/
      restart/enable/disable return **403**, so the generic Services windows cannot unload the
      firewall behind the baseline rules and audit trail.
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
- [x] **P2 — ban precedence.** Ban rules are now prepended with `ufw insert 1` instead of appended.
      UFW evaluates rules in order, so a `deny from <ip>` appended after the baseline
      `allow 443/tcp from anywhere` never matched. This affects manual and automatic bans.
- [x] **P2 — auto-ban.** Native engine with temporary, escalating bans, allowlist protection, caps,
      dry-run and a dedicated UI tab — no fail2ban dependency. See below.

## Auto-ban

`backend/services/autoban_service.py` runs a tracked background sweep (every 60 s) and prepends a
`deny from <ip>` rule tagged `ethos-autoban`. Manual bans keep the `ethos-ban` tag and stay permanent.

### Safety invariants (enforced server-side, not in the UI)

- Only globally routable unicast addresses are banned. Explicitly refused: loopback, RFC1918,
  link-local, CGNAT (`100.64/10`), IPv6 ULA (`fc00::/7`), multicast, reserved, unspecified and the
  detected LAN subnet. IPv4 multicast matters: CPython reports `224.0.0.0/4` as `is_global`.
- The operator allowlist (IPs/CIDRs) is checked before any rule is created; a `/0` entry is rejected.
- Auto-bans are single hosts only (`/32`, `/128`) — never a CIDR.
- `dry_run` is the default: the sweep records candidates and statistics but never touches UFW.

### Policy

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch for the background sweep |
| `dry_run` | `true` | Preview only; the manual “Apply now” action bypasses it (`apply=true`) |
| `failed_logins.enabled` | `true` | Count sshd + PAM/web failures |
| `failed_logins.threshold` / `window_minutes` | `5` / `10` | Events per source IP inside the window |
| `failed_logins.ban_minutes` | `60` | First ban duration |
| `ufw_blocks.enabled` | `false` | Count `UFW BLOCK` floods (scanners) |
| `ufw_blocks.threshold` / `window_minutes` / `min_ports` | `40` / `10` / `5` | Require several targeted ports, not one retry |
| `ufw_blocks.ban_minutes` | `60` | First ban for a block flood |
| `escalate` | `true` | Repeat offender → max(base, 1 h → 24 h → 7 d) |
| `allowlist` | `[]` | Never banned, max 200 entries |
| `max_active_bans` / `max_bans_per_sweep` / `max_bans_per_day` | `200` / `10` / `200` | Hard caps (UFW performance) |

Bans expire on their own; expiry calls `unban_ip` and records `expired` in the history. An admin
unban drops the auto record and starts a 30 min cooldown so the engine does not immediately re-ban
the same host. If the UFW rule disappears externally, the next sweep drops the state entry
(`vanished`) — but only when UFW is active, so a disabled firewall never wipes the state.

### API (admin only)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/security/autoban` | Config, active bans, history, protected networks, detected LAN, UFW state |
| `PUT` | `/api/security/autoban` | Replace the policy (Pydantic ranges; invalid allowlist → 400) |
| `POST` | `/api/security/autoban/run` | One pass; `{"apply": false}` previews, `true` creates rules |
| `GET` | `/api/security/autoban/history` | Bounded recent activity |
| `GET` | `/api/security/bans` | Now includes `kind` (`manual`/`auto`), `expires_at`, `reason` |
| `DELETE` | `/api/security/bans/{ip}` | Removes either kind; auto bans get the cooldown |

State: `<DATA_DIR>/security_autoban.json` (json_store v1; history capped at 500, strikes and
cooldowns at 2 000 entries). Notifications use `source="firewall"` and
`dedupe_key="autoban-sweep"`, so a busy sweep produces one event per 5 min window.

### UI

New rail section **Auto-ban**: master switch, dry-run switch, thresholds/durations, caps, allowlist
editor, read-only protected ranges, **Preview now** (dry run), **Apply now** (confirmed), active bans
with remaining time and unban, and recent activity. The overview shows an Auto-ban tile; the Bans tab
splits manual vs auto, expiry and reason.

## Known follow-ups (not done)

- ~~Roles are decorative~~ — `backend/routers/access_control.py` is mounted with
  `Depends(require_admin)`, so every endpoint in this app is admin-only. Re-verify after any router
  refactor.
- Existing bans created before this fix are TCP-only; re-ban or add a one-time migration if
  all-protocol coverage matters for old entries.
- Baseline rule `ethos-lan` allows the whole detected `/24` (by design, to survive enabling
  UFW remotely). Narrow it in `/etc/ufw/user.rules` if the LAN is not trusted.

## Tests

```
.venv/bin/python tests/test_firewall_access_control.py
.venv/bin/python tests/test_autoban.py
```

`test_firewall_access_control.py` covers status parsing/numbering, validators, ban normalization,
command building, unban bounding, the journald fallback, caching and PAM/sshd login parsing.
`test_autoban.py` covers the protection rules (including IPv4 multicast), thresholds, distinct-port
requirement, dry-run, temporary bans, escalation, caps, cooldown, reconcile and the `insert 1`
command. Both monkeypatch every `ufw`/log call — no root, no firewall changes.
