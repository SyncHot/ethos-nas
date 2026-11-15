"""Tests — Network app (interfaces contract + connection status endpoint).

Part A1 (in-process, always runs): DNS parsing from a fake resolv.conf;
`ip -j route show default` output parsing incl. malformed JSON / missing `ip` /
no-default-route states; _collect_status gateway selection.

Part A2 (live API E2E — needs an isolated instance per AGENTS.md):
    fuser -k 9001/tcp 2>/dev/null; \
    ETHOS_PORT=9001 ETHOS_DATA_DIR=/tmp/ethos-net-test-data ETHOS_NET_STATS_INTERVAL=5 \
        nohup .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 9001 \
        > /tmp/ethos-nas-net-test.log 2>&1 &
    then:  ETHOS_TEST_BASE=http://127.0.0.1:9001 .venv/bin/python tests/test_network.py
    cleanup after stopping the instance: rm -rf /tmp/ethos-net-test-data

Part B (UI via Playwright, auto-skips when chromium is unavailable): Interfaces
tab renders the table (not "No network interfaces found") and the Connection
card shows the gateway.
"""
import json
import os

def _require_test_password() -> str:
    """Test credentials are never committed — export ETHOS_TEST_PASSWORD."""
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw
import subprocess
import sys
import tempfile
import types
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def api(method, path, token=None, body=None):
    """Minimal JSON API client. Returns (status_code, parsed_json_or_None)."""
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def login():
    code, data = api("POST", "/api/auth/login", body={"username": USER, "password": PASS})
    if code != 200 or not isinstance(data, dict):
        raise RuntimeError(f"login failed: HTTP {code}")
    return data["token"]


# ── Part A1: in-process parsing logic (deterministic) ───────────────────────

def part_a1() -> int:
    print("\nPart A1 — parsing logic (in-process)")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from backend.routers import network as net

    failures = 0
    tmpdir = Path(tempfile.mkdtemp(prefix="ethos-net-test-"))

    # 1. DNS parsing: v4 + v6 + comments/options ignored; missing file → [].
    resolv = tmpdir / "resolv.conf"
    resolv.write_text(
        "# comment line\n"
        "nameserver 127.0.0.53\n"
        "   nameserver   8.8.8.8   \n"          # extra whitespace tolerated
        "nameserver 2606:4700:4700::1111\n"
        "options edns0 trust-ad\n"
        "search .\n",
    )
    got = net._parse_dns_servers(str(resolv))
    if not check("dns parse: v4+v6, comments/options skipped",
                 got == ["127.0.0.53", "8.8.8.8", "2606:4700:4700::1111"], f"got {got}"):
        failures += 1
    if not check("dns parse: missing file → [] (no exception)",
                 net._parse_dns_servers(str(tmpdir / "nope.conf")) == []):
        failures += 1

    # 2. Route parsing: valid JSON, no-default-route, malformed, missing `ip`.
    class _R:  # fake subprocess.run result
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout = rc, out

    saved_run = net.subprocess.run
    saved_resolv = net.RESOLV_CONF
    try:
        ip_json = json.dumps([
            {"dst": "default", "gateway": "192.168.1.1", "dev": "enp3s0",
             "protocol": "dhcp", "prefsrc": "192.168.1.215", "metric": 100, "flags": []},
        ])
        net.subprocess.run = lambda *a, **k: _R(0, ip_json)
        routes, note = net._default_routes()
        if not check("routes parse: gateway/dev/source_ip extracted",
                     note is None and len(routes) == 1
                     and routes[0]["gateway"] == "192.168.1.1"
                     and routes[0]["dev"] == "enp3s0"
                     and routes[0]["source_ip"] == "192.168.1.215",
                     f"routes={routes} note={note}"):
            failures += 1

        net.subprocess.run = lambda *a, **k: _R(0, "[]")
        routes, note = net._default_routes()
        if not check("no default route → ([], None) — valid state",
                     routes == [] and note is None, f"routes={routes} note={note}"):
            failures += 1

        net.subprocess.run = lambda *a, **k: _R(0, "not-json{")
        routes, note = net._default_routes()
        if not check("malformed JSON → graceful note",
                     routes == [] and note == "could not parse route output", f"note={note}"):
            failures += 1

        def _missing(*a, **k):
            raise FileNotFoundError("ip")
        net.subprocess.run = _missing
        routes, note = net._default_routes()
        if not check("missing `ip` → graceful note",
                     routes == [] and note is not None and "not found" in note, f"note={note}"):
            failures += 1

        net.subprocess.run = lambda *a, **k: _R(2, "")
        routes, note = net._default_routes()
        if not check("non-zero exit → graceful note",
                     routes == [] and note is not None, f"note={note}"):
            failures += 1

        # 3. _collect_status picks the first gateway + merges DNS from file.
        net.RESOLV_CONF = str(resolv)
        net.subprocess.run = lambda *a, **k: _R(0, ip_json)
        st = net._collect_status()
        if not check("_collect_status: gateway selected from first routed entry",
                     st["gateway"] == {"ip": "192.168.1.1", "interface": "enp3s0",
                                       "source_ip": "192.168.1.215"}
                     and st["dns_servers"] == ["127.0.0.53", "8.8.8.8", "2606:4700:4700::1111"],
                     json.dumps(st)[:200]):
            failures += 1

        # No gateway anywhere → gateway None, routes still listed.
        net.subprocess.run = lambda *a, **k: _R(0, json.dumps([{"dst": "default", "dev": "lo"}]))
        st = net._collect_status()
        if not check("route without gateway → gateway=None, route kept",
                     st["gateway"] is None and len(st["routes"]) == 1):
            failures += 1
    finally:
        net.subprocess.run = saved_run
        net.RESOLV_CONF = saved_resolv

    # 4. Real-host sanity: _collect_status() must not raise on this machine.
    real = net._collect_status()
    if not check("real _collect_status(): well-formed",
                 isinstance(real.get("dns_servers"), list) and isinstance(real.get("routes"), list),
                 json.dumps(real)[:200]):
        failures += 1

    # ── Net stats service: sampler + history deltas ────────────────────────
    from backend.config import settings
    from backend.services import net_stats_service as nss

    ns_dir = Path(tempfile.mkdtemp(prefix="ethos-netstats-test-"))
    clock = {"now": 1_752_600_000}
    counters = {"rx": 10**9, "tx": 5 * 10**8}

    class _FakeIO:
        def __init__(self):
            self.bytes_recv = counters["rx"]
            self.bytes_sent = counters["tx"]

    saved_ns_dir = settings.DATA_DIR
    saved_psutil = nss.psutil
    saved_max = nss.MAX_SAMPLES
    try:
        settings.DATA_DIR = str(ns_dir)
        nss.psutil = types.SimpleNamespace(net_io_counters=lambda: _FakeIO())
        nss._now_ts = lambda: clock["now"]

        # 5. record_sample writes a well-formed store entry.
        s = nss.record_sample()
        if not check("record_sample → {ts,rx,tx} + persisted",
                     s == {"ts": clock["now"], "rx": counters["rx"], "tx": counters["tx"]}
                     and json.loads((ns_dir / "net_stats.json").read_text())["samples"] == [s]):
            failures += 1

        # Same-second duplicate replaces the last sample instead of appending.
        counters["rx"] += 10**6
        nss.record_sample()
        store = json.loads((ns_dir / "net_stats.json").read_text())
        if not check("same-second re-sample replaces, doesn't double",
                     len(store["samples"]) == 1 and store["samples"][0]["rx"] == counters["rx"]):
            failures += 1

        # 6. Trimming to cap (temporarily shrink MAX_SAMPLES).
        nss.MAX_SAMPLES = 5
        for _ in range(7):
            clock["now"] += 60
            counters["rx"] += 10**6
            nss.record_sample()
        store = json.loads((ns_dir / "net_stats.json").read_text())
        if not check("samples trimmed to cap (oldest dropped)",
                     len(store["samples"]) == 5 and store["samples"][0]["ts"] == clock["now"] - 4 * 60):
            failures += 1

        # 7. get_history: deltas between consecutive samples.
        (ns_dir / "net_stats.json").unlink()
        base = 1_752_600_000
        clock["now"] = base
        counters.update(rx=100, tx=50)
        nss.record_sample()                       # t=base: rx=100, tx=50
        clock["now"] = base + 60
        counters.update(rx=700, tx=250)           # Δrx=600/60s=10 B/s, Δtx≈3.33
        nss.record_sample()
        pts = nss.get_history(minutes=60)
        if not check("history: delta computed per interval",
                     len(pts) == 1 and pts[0]["ts"] == base + 60
                     and pts[0]["rx_bps"] == 10.0 and abs(pts[0]["tx_bps"] - 3.3) < 0.1,
                     json.dumps(pts)):
            failures += 1

        # Counter reset (negative delta) → zeroed, never negative.
        clock["now"] = base + 120
        counters.update(rx=10, tx=400)            # Δrx=-690→0, Δtx=150/60=2.5
        nss.record_sample()
        pts = nss.get_history(minutes=60)
        if not check("history: counter reset clamped to zero",
                     len(pts) == 2 and pts[1]["rx_bps"] == 0.0 and pts[1]["tx_bps"] == 2.5,
                     json.dumps(pts)):
            failures += 1

        # Window filtering + baseline kept at the window edge.
        clock["now"] = base + 3600                # 1 h later: nothing in last 5 min
        if not check("history: empty outside window", nss.get_history(minutes=5) == []):
            failures += 1
        clock["now"] = base + 179                 # cutoff=base+119 → only t=base+120 point
        pts = nss.get_history(minutes=1)
        if not check("history: window edge keeps baseline delta",
                     len(pts) == 1 and pts[0]["ts"] == base + 120, json.dumps(pts)):
            failures += 1

        # Empty store → [].
        (ns_dir / "net_stats.json").unlink()
        if not check("history: empty store → []", nss.get_history(minutes=60) == []):
            failures += 1
    finally:
        settings.DATA_DIR = saved_ns_dir
        nss.psutil = saved_psutil
        nss.MAX_SAMPLES = saved_max

    import shutil as _shutil
    _shutil.rmtree(tmpdir, ignore_errors=True)
    _shutil.rmtree(ns_dir, ignore_errors=True)
    return failures


# ── Part A2: live API E2E (isolated instance on :9001) ──────────────────────

def part_a2(token) -> int:
    print("\nPart A2 — API E2E")
    failures = 0

    # 1. /status shape + host reality.
    code, data = api("GET", "/api/network/status", token)
    if not check("GET /api/network/status → 200 with gateway/dns_servers/routes",
                 code == 200 and isinstance(data, dict)
                 and "gateway" in data and isinstance(data.get("dns_servers"), list)
                 and isinstance(data.get("routes"), list), f"HTTP {code}"):
        return failures + 1

    gw = data["gateway"]
    if not check("default gateway present on this host",
                 isinstance(gw, dict) and gw.get("ip") and gw.get("interface"),
                 json.dumps(gw)[:200]):
        failures += 1
    # Cross-check against the kernel's own view (ground truth).
    try:
        real = subprocess.run(["ip", "-j", "route", "show", "default"],
                              capture_output=True, text=True, timeout=10)
        real_routes = json.loads(real.stdout or "[]") if real.returncode == 0 else []
        real_gw = next((r.get("gateway") for r in real_routes if r.get("gateway")), None)
    except Exception:
        real_gw = None
    if real_gw is not None:
        if not check("API gateway matches `ip route` ground truth",
                     gw.get("ip") == real_gw, f"api={gw.get('ip')} kernel={real_gw}"):
            failures += 1
    if not check("dns_servers non-empty (resolv.conf present)", len(data["dns_servers"]) >= 1,
                 json.dumps(data["dns_servers"])):
        failures += 1

    # 2. /interfaces contract — object with `interfaces` array + real fields.
    code, data = api("GET", "/api/network/interfaces", token)
    if not check("GET /api/network/interfaces → {interfaces:[...], total}",
                 code == 200 and isinstance(data, dict)
                 and isinstance(data.get("interfaces"), list) and "total" in data, f"HTTP {code}"):
        failures += 1
    ifaces = (data or {}).get("interfaces") or []
    if not check("interface entries carry name/status/ipv4/mac fields",
                 len(ifaces) >= 1 and all(
                     set(("name", "status", "ipv4", "mac")) <= set(i) for i in ifaces),
                 f"got {len(ifaces)}"):
        failures += 1
    up = [i for i in ifaces if i.get("status") == "up"]
    if not check("at least one interface is up with an IPv4",
                 any(i.get("ipv4") for i in up), f"up={len(up)}"):
        failures += 1

    # 3. /stats/history — sampler output shape; with ETHOS_NET_STATS_INTERVAL=5
    #    points appear within ~30 s of instance start (poll until they do).
    code, data = api("GET", "/api/network/stats/history?minutes=60", token)
    if not check("GET /stats/history → 200 {samples, interval_seconds, now}",
                 code == 200 and isinstance(data, dict)
                 and isinstance(data.get("samples"), list)
                 and data.get("interval_seconds") in (5, 60)
                 and isinstance(data.get("now"), int), f"HTTP {code}"):
        failures += 1

    import time as _time
    samples = []
    for _ in range(20):
        code, data = api("GET", "/api/network/stats/history?minutes=60", token)
        samples = (data or {}).get("samples") or []
        if samples:
            break
        _time.sleep(1.5)
    if not check("sampler produces history points within ~30 s", len(samples) >= 1,
                 f"got {len(samples)} after polling"):
        failures += 1
    else:
        ok_shape = all(set(("ts", "rx_bps", "tx_bps")) <= set(p)
                       and p["rx_bps"] >= 0 and p["tx_bps"] >= 0 for p in samples)
        if not check("history points well-formed (ts, rx_bps≥0, tx_bps≥0)",
                     ok_shape, json.dumps(samples[:3])):
            failures += 1

    code, data = api("GET", "/api/network/stats/history?minutes=0", token)
    if not check("history minutes<1 → 422 (query validation)", code == 422):
        failures += 1

    return failures


# ── Part B: UI (Playwright; auto-skips without chromium) ─────────────────────

def part_b(token) -> int:
    print("\nPart B — UI (Playwright)")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [SKIP] playwright not installed")
        return 0

    failures = 0
    # The whole body must run INSIDE the sync_playwright() context — the
    # driver's event loop is closed when the with-block exits, and any
    # browser call after that raises "Event loop is closed" (T-0007).
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            print(f"  [SKIP] chromium unavailable ({str(e).splitlines()[0][:120]}…)")
            return 0

        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        js_errors: list[str] = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))

        try:
            # Fresh page → login overlay.
            page.goto(BASE, wait_until="networkidle")
            page.evaluate("localStorage.removeItem('token')")
            page.reload(wait_until="networkidle")
            page.fill('[data-testid="login-username"]', USER)
            page.fill('[data-testid="login-password"]', PASS)
            # The login handler hides the overlay and then does
            # window.location.reload() — mark the document so we can wait for
            # the fresh post-login page before interacting (T-0007).
            page.evaluate("window.__preLoginDoc = true")
            page.click('[data-testid="login-submit"]')
            page.wait_for_function(
                "() => { const o = document.getElementById('loginOverlay'); return !o || o.style.display === 'none'; }",
                timeout=20_000,
            )
            page.wait_for_function(
                "() => !window.__preLoginDoc && document.readyState === 'complete'",
                timeout=20_000,
            )

            # Open the Network app (Interfaces tab is default).
            page.evaluate("window.openApp('network')")
            try:
                # data-testid, not an id — the app only sets data-testid on the table
                page.wait_for_selector('[data-testid="net-interfaces-table"]', timeout=15_000)
            except Exception:
                pass
            if not check("interfaces table rendered (contract fixed)",
                         page.locator('[data-testid="net-interfaces-table"]').count() == 1):
                browser.close()
                return failures + 1
            body_text = page.locator("#netContent").inner_text()
            if not check("'No network interfaces found' no longer shown",
                         "No network interfaces found" not in body_text, body_text[:200]):
                failures += 1

            # Connection card shows the gateway.
            card = page.locator('[data-testid="net-connection-card"]')
            if not check("Connection card rendered", card.count() == 1):
                failures += 1
            else:
                code, st = api("GET", "/api/network/status", token)
                gw_ip = ((st or {}).get("gateway") or {}).get("ip", "")
                if gw_ip:
                    card_text = card.inner_text()
                    if not check(f"Connection card shows gateway {gw_ip}", gw_ip in card_text,
                                 card_text[:200]):
                        failures += 1

            if js_errors:
                print(f"         page errors: {js_errors[:3]}")
        finally:
            browser.close()
    return failures


def main():
    total = part_a1()

    try:
        token = login()
    except Exception as e:
        print(f"\n[SKIP] Part A2/B — server not reachable at {BASE} ({e})")
        print("       Start an isolated instance first:")
        print("         fuser -k 9001/tcp 2>/dev/null; ETHOS_PORT=9001 \\")
        print("           ETHOS_DATA_DIR=/tmp/ethos-net-test-data nohup .venv/bin/python -m uvicorn \\")
        print("           backend.main:app --host 127.0.0.1 --port 9001 > /tmp/ethos-nas-net-test.log 2>&1 &")
        print(f"       then re-run with ETHOS_TEST_BASE=http://127.0.0.1:9001")
        sys.exit(1 if total else 0)

    total += part_a2(token)
    total += part_b(token)

    print(f"\n{'=' * 50}\nRESULT: {'ALL PASS' if total == 0 else f'{total} FAILURE(S)'}")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
