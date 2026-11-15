"""End-to-end test — Cameras app (isolated :9001 instance).

Self-contained: starts a throwaway uvicorn on 127.0.0.1:9001 with its own
ETHOS_DATA_DIR, plus a local HTTP-MJPEG "camera" (multipart/x-mixed-replace of
alternating black/white frames — ffmpeg-readable, and the frame alternation is
what triggers motion detection).

Part A (API):  add camera → connection test → snapshot → enable motion →
               wait for a motion event (+ image) → events cleanup → delete cam.
Part B (UI):   PAM login via Playwright → open Surveillance app → tabs render,
               add-camera modal works, live tile <img> points at /live?token=.

Run:  .venv/bin/python tests/test_surveillance.py
"""
import http.server
import io
import json
import os

def _require_test_password() -> str:
    """Test credentials are never committed — export ETHOS_TEST_PASSWORD."""
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
# :9001 (the AGENTS.md convention) may be taken by docker on some hosts —
# pick a free port at runtime unless ETHOS_TEST_PORT is set.
PORT = int(os.environ.get("ETHOS_TEST_PORT", "0")) or 0
BASE = None  # filled in main() once the port is known
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()


# ── Local MJPEG camera source ───────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class MjpegSource:
    """Serves multipart/x-mixed-replace of alternating black/white JPEGs."""

    def __init__(self, port: int):
        self.port = port
        # 8 distinct noise frames on a 4 s wall-clock cycle (0.5 s each): any
        # two grabs ≥1 s apart land on different images regardless of phase —
        # no aliasing between the source period and the motion check interval.
        import random
        rng = random.Random(42)
        frames = []
        for _ in range(8):
            noise = bytes(rng.randrange(256) for _ in range(320 * 180))
            buf = io.BytesIO()
            Image.frombytes("L", (320, 180), noise).convert("RGB").save(buf, "JPEG")
            frames.append(buf.getvalue())
        self.frames = frames

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path != "/stream":
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                try:
                    # Phase-align to wall clock so every fresh connection sees
                    # the *current* frame — like a real camera, not always
                    # frames[0]. This is what makes consecutive ffmpeg grabs
                    # differ and triggers motion detection.
                    idx = int(time.time() * 2) % len(frames)
                    while True:
                        f = frames[idx]
                        idx = (idx + 1) % len(frames)
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                          b"Content-Length: %d\r\n\r\n" % len(f))
                        self.wfile.write(f)
                        self.wfile.flush()
                        time.sleep(0.5)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/stream"


# ── Server lifecycle ────────────────────────────────────────────────────────

def start_server(data_dir: Path, port: int) -> subprocess.Popen:
    env = dict(os.environ, ETHOS_PORT=str(port), ETHOS_DATA_DIR=str(data_dir))
    logf = open("/tmp/ethos-surv-test.log", "ab")
    proc = subprocess.Popen(
        [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            r = requests.get(f"{base}/api/system/status", timeout=2)
            if r.status_code in (200, 401):
                return proc
        except requests.ConnectionError:
            time.sleep(0.5)
    raise RuntimeError("server did not come up — see /tmp/ethos-surv-test.log")


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    global BASE
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-surv-test-"))
    cam_port = _free_port()
    src = MjpegSource(cam_port)
    port = PORT or _free_port()
    proc = start_server(data_dir, port)
    BASE = f"http://127.0.0.1:{port}"
    print(f"Isolated server on :{port} (data: {data_dir}), MJPEG source at {src.url()}")

    try:
        # ── Part A: API ─────────────────────────────────────────────────────
        print("\nPart A — API")
        r = requests.post(f"{BASE}/api/auth/login", json={"username": USER, "password": PASS}, timeout=10)
        check("PAM login", r.status_code == 200, str(r.status_code))
        tok = r.json()["token"]
        H = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

        # validation errors
        r = requests.post(f"{BASE}/api/surveillance/cameras", headers=H,
                          json={"name": "Bad", "url": "ftp://x"}, timeout=10)
        check("rejects bad scheme (400)", r.status_code == 400, str(r.status_code))

        # add camera
        r = requests.post(f"{BASE}/api/surveillance/cameras", headers=H,
                          json={"name": "E2E Cam", "url": src.url()}, timeout=10)
        check("add camera (201)", r.status_code == 201, str(r.status_code))
        cam = r.json()["camera"]
        cid = cam["id"]

        # duplicate URL rejected
        r = requests.post(f"{BASE}/api/surveillance/cameras", headers=H,
                          json={"name": "Dup", "url": src.url()}, timeout=10)
        check("rejects duplicate URL (400)", r.status_code == 400, str(r.status_code))

        # list
        r = requests.get(f"{BASE}/api/surveillance/cameras", headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        cams = r.json()["cameras"]
        check("list shows camera + last_error field", any(c["id"] == cid and "last_error" in c for c in cams))

        # connection test (must succeed against the local source, ≤8 s)
        t0 = time.time()
        r = requests.post(f"{BASE}/api/surveillance/cameras/{cid}/test", headers=H, timeout=15)
        d = r.json()
        check("connection test ok", r.status_code == 200 and d.get("ok") is True, json.dumps(d))
        print(f"    (probe took {time.time() - t0:.1f}s)")

        # snapshot
        r = requests.get(f"{BASE}/api/surveillance/cameras/{cid}/snapshot?w=320",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=15)
        check("snapshot is JPEG", r.status_code == 200 and r.headers.get("content-type") == "image/jpeg"
              and len(r.content) > 100, f"{r.status_code} {len(r.content)}B")

        # live stream: first multipart part must arrive quickly
        try:
            with requests.get(f"{BASE}/api/surveillance/cameras/{cid}/live",
                              headers={"Authorization": f"Bearer {tok}"}, stream=True, timeout=10) as r:
                check("live is multipart MJPEG", "multipart/x-mixed-replace" in r.headers.get("content-type", ""),
                      str(r.headers.get("content-type")))
                first = next(r.iter_content(chunk_size=65536))
                check("live stream delivers frames", b"\xff\xd8" in first, f"{len(first)}B")
        except Exception as exc:  # noqa: BLE001
            check("live stream delivers frames", False, str(exc))

        # motion detection → event
        r = requests.put(f"{BASE}/api/surveillance/cameras/{cid}", headers=H,
                         json={"motion": {"enabled": True, "interval_s": 1, "sensitivity": 0.05}}, timeout=10)
        check("enable motion (200)", r.status_code == 200, str(r.status_code))

        event = None
        for _ in range(30):  # up to ~30 s
            time.sleep(1)
            evs = requests.get(f"{BASE}/api/surveillance/events", headers={"Authorization": f"Bearer {tok}"}, timeout=10).json()["events"]
            if any(e["camera_id"] == cid for e in evs):
                event = next(e for e in evs if e["camera_id"] == cid)
                break
        check("motion event recorded", event is not None, "no event within 30 s")

        if event and event.get("image"):
            r = requests.get(f"{BASE}/api/surveillance/events/{event['id']}/image",
                             headers={"Authorization": f"Bearer {tok}"}, timeout=10)
            check("event image is JPEG", r.status_code == 200 and len(r.content) > 50, str(r.status_code))

        # events cleanup
        if event:
            r = requests.delete(f"{BASE}/api/surveillance/events/{event['id']}", headers=H, timeout=10)
            check("delete event", r.status_code == 200, str(r.status_code))
        r = requests.delete(f"{BASE}/api/surveillance/events", headers=H, timeout=10)
        check("clear events", r.status_code == 200 and r.json().get("ok") is True, str(r.status_code))

        # ── MP4 clip recording ────────────────────────────────
        r = requests.post(f"{BASE}/api/surveillance/cameras/{cid}/record", headers=H, timeout=15)
        check("start recording (201)", r.status_code == 201 and r.json().get("recording") is True,
              f"{r.status_code} {r.text[:80]}")

        r = requests.post(f"{BASE}/api/surveillance/cameras/{cid}/record", headers=H, timeout=15)
        check("double record rejected (409)", r.status_code == 409, str(r.status_code))

        time.sleep(5)  # let a few seconds of footage accumulate

        r = requests.delete(f"{BASE}/api/surveillance/cameras/{cid}/record", headers=H, timeout=30)
        check("stop recording (200)", r.status_code == 200 and r.json().get("stopped") is True,
              f"{r.status_code} {r.text[:80]}")
        size = r.json().get("size_bytes", 0) if r.ok else 0
        check("clip file written (>1 KB)", size > 1024, f"{size}B")

        clips = requests.get(f"{BASE}/api/surveillance/clips?camera_id={cid}",
                             headers={"Authorization": f"Bearer {tok}"}, timeout=10).json()["clips"]
        check("clip listed", len(clips) >= 1 and clips[0]["size_bytes"] > 1024, json.dumps(clips)[:120])

        if clips:
            clip_id = clips[0]["id"]
            r = requests.get(f"{BASE}/api/surveillance/clips/{clip_id}/download",
                             headers={"Authorization": f"Bearer {tok}"}, timeout=30)
            check("clip download is MP4", r.status_code == 200
                  and "video/mp4" in r.headers.get("content-type", "")
                  and len(r.content) > 1024, str(r.status_code))
            check("clip has ftyp box (valid mp4)", b"ftyp" in r.content[4:16], repr(r.content[:16]))
            r = requests.delete(f"{BASE}/api/surveillance/clips/{clip_id}", headers=H, timeout=10)
            check("delete clip (200)", r.status_code == 200, str(r.status_code))
        clips_after = requests.get(f"{BASE}/api/surveillance/clips?camera_id={cid}",
                                   headers={"Authorization": f"Bearer {tok}"}, timeout=10).json()["clips"]
        check("clip list empty after delete", len(clips_after) == 0, json.dumps(clips_after)[:80])

        r = requests.delete(f"{BASE}/api/surveillance/cameras/{cid}/record", headers=H, timeout=15)
        check("stop when not recording (409)", r.status_code == 409, str(r.status_code))

        # disable + delete camera
        r = requests.put(f"{BASE}/api/surveillance/cameras/{cid}", headers=H, json={"enabled": False}, timeout=10)
        check("disable camera (200)", r.status_code == 200, str(r.status_code))
        r = requests.delete(f"{BASE}/api/surveillance/cameras/{cid}", headers=H, timeout=10)
        check("delete camera", r.status_code == 200, str(r.status_code))
        cams = requests.get(f"{BASE}/api/surveillance/cameras", headers={"Authorization": f"Bearer {tok}"}, timeout=10).json()["cameras"]
        check("camera list empty after delete", len(cams) == 0, json.dumps(cams))

        # ── Part C: Network discovery ───────────────────────────────────────
        print("\nPart C — Discovery")
        sys.path.insert(0, str(ROOT))
        from backend.services import camera_discovery as cd

        sample_onvif = (
            b'<?xml version="1.0"?><env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope">'
            b'<env:Body><ProbeMatchMessage><Types>dn:Device</Types>'
            b'<XAddrs>http://192.168.1.50/onvif/device</XAddrs></ProbeMatchMessage></env:Body></env:Envelope>')
        check("parse_onvif_response extracts XAddr",
              cd.parse_onvif_response(sample_onvif) == "http://192.168.1.50/onvif/device",
              repr(cd.parse_onvif_response(sample_onvif)))
        check("parse_onvif_response ignores non-ONVIF data", cd.parse_onvif_response(b"hello") is None)

        # Fake RTSP responder + plain HTTP server on 127.0.0.1.
        rtsp_port = _free_port()
        http_port = _free_port()

        class RtspHandler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    self.rfile.readline()          # request line + headers
                    while True:
                        line = self.rfile.readline()
                        if not line or line in (b"\r\n", b""):
                            break
                    self.wfile.write(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nPublic: OPTIONS, DESCRIBE\r\n\r\n")
                except OSError:
                    pass

        class HttpHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            def log_message(self, *a):
                pass

        rtsp_srv = socketserver.ThreadingTCPServer(("127.0.0.1", rtsp_port), RtspHandler)
        http_srv = http.server.HTTPServer(("127.0.0.1", http_port), HttpHandler)
        threading.Thread(target=rtsp_srv.serve_forever, daemon=True).start()
        threading.Thread(target=http_srv.serve_forever, daemon=True).start()

        try:
            scan = cd.scan_ips(["127.0.0.1"], ports=(rtsp_port, http_port), rtsp_ports=(rtsp_port,))
            entry = scan.get("127.0.0.1", {})
            check("scan finds both open ports",
                  set(entry.get("ports", [])) == {rtsp_port, http_port}, json.dumps(entry))
            check("RTSP OPTIONS confirmation works", entry.get("rtsp") is True, json.dumps(entry))
        finally:
            rtsp_srv.shutdown(); rtsp_srv.server_close()
            http_srv.shutdown(); http_srv.server_close()

        # API: ONVIF-only probe (no subnet scan) — must return the result shape.
        r = requests.post(f"{BASE}/api/surveillance/discover", headers=H,
                          json={"timeout_s": 1, "scan_subnets": False}, timeout=30)
        d = r.json() if r.status_code == 200 else {}
        check("POST /discover (onvif only) → shape",
              r.status_code == 200 and isinstance(d.get("results"), list)
              and "scanned" in d and "duration_s" in d, json.dumps(d)[:150])

        # API: full discovery incl. real LAN scan — must complete and stay well-formed.
        t0 = time.time()
        r = requests.post(f"{BASE}/api/surveillance/discover", headers=H,
                          json={"timeout_s": 1, "scan_subnets": True}, timeout=60)
        d = r.json() if r.status_code == 200 else {}
        check("POST /discover (full) → shape + bounded runtime",
              r.status_code == 200 and isinstance(d.get("results"), list)
              and d.get("scanned", {}).get("network")
              and time.time() - t0 < 45,
              f"{time.time() - t0:.1f}s {json.dumps(d)[:150]}")
        for row in d.get("results", []):
            if not (row.get("ip") and row.get("suggested_url")):
                check("every result has ip + suggested_url", False, json.dumps(row))
                break
        else:
            check("every result has ip + suggested_url", True)

        # ── Part B: UI (Playwright) ────────────────────────────────────────
        print("\nPart B — UI")
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
            js_errors: list[str] = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))

            page.goto(BASE, wait_until="networkidle")
            page.fill('[data-testid="login-username"]', USER)
            page.fill('[data-testid="login-password"]', PASS)
            page.click('[data-testid="login-submit"]')
            page.wait_for_selector("#loginOverlay", state="hidden", timeout=20_000)
            check("PAM login (UI)", bool(page.evaluate("localStorage.getItem('token') || ''")))

            page.evaluate("window.openApp('surveillance')")
            page.wait_for_selector('[data-testid="surv-tab-live"]', timeout=15_000)
            check("app window renders tabs", True)

            # add a camera through the modal
            page.click('[data-testid="surv-tab-cameras"]')
            page.click('[data-testid="surv-add-camera"]')
            page.wait_for_selector('.ethos-modal-input[name="name"]', timeout=10_000)
            page.fill('.ethos-modal-input[name="name"]', "UI Cam")
            page.fill('.ethos-modal-input[name="url"]', src.url())
            page.click('[data-testid="modal-form-ok"]')
            page.wait_for_function(
                "() => document.querySelector('table tbody tr[data-cam]') !== null", timeout=15_000)
            check("camera row appears after modal add", True)

            rec_btns = page.locator('button[data-testid^="surv-record-"]').count()
            check("record button present on camera row", rec_btns >= 1, f"{rec_btns} buttons")

            # network discovery: click → panel fills (results or empty message) → button re-enables
            page.click('[data-testid="surv-discover"]')
            try:
                page.wait_for_function(
                    "() => { const el = document.querySelector('#surv-discover');"
                    " const t = el ? el.textContent : ''; return t.includes('Found ') || t.includes('No cameras found'); }",
                    timeout=60_000)
                check("discovery panel shows results/empty message", True)
            except Exception as e:
                check("discovery panel shows results/empty message", False, str(e)[:120])
            page.wait_for_function(
                "() => { const b = document.querySelector('[data-testid=\"surv-discover\"]'); return b && !b.disabled; }",
                timeout=30_000)
            check("discover button re-enabled after scan", True)

            # live tab shows an MJPEG tile for the camera
            page.click('[data-testid="surv-tab-live"]')
            img = page.wait_for_selector('img[data-testid^="surv-live-"]', timeout=15_000)
            src_attr = img.get_attribute("src") or ""
            check("live tile <img> targets /live?token=", "/live?token=" in src_attr, src_attr[:80])

            # events + clips tabs render (empty state ok)
            page.click('[data-testid="surv-tab-events"]')
            check("events tab renders", True)
            page.click('[data-testid="surv-tab-clips"]')
            page.wait_for_timeout(500)  # let the async clip load settle
            check("clips tab renders", True)

            check("no JS errors on page", not js_errors, "; ".join(js_errors)[:300])
            browser.close()

    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        src.srv.shutdown()
        shutil.rmtree(data_dir, ignore_errors=True)

    print(f"\n{'=' * 50}\n{len(failures)} failure(s)" if failures else "\nAll checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
