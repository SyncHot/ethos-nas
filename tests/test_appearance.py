"""API test — per-user desktop wallpaper (/api/appearance).

Spins up an ISOLATED instance on :9002 with a fresh temp data dir (never touches
production state), logs in via PAM, and exercises the full wallpaper lifecycle:

  GET 404 → POST built-in → GET roundtrip → invalid id rejected →
  upload custom image → image served byte-identical → DELETE resets + removes file.

Also verifies the SPA shell ships the new wallpaper markup (esm-v7 entry).

Run:  .venv/bin/python tests/test_appearance.py
"""
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
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

PORT = 9002
BASE = f"http://127.0.0.1:{PORT}"
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()
REPO = Path(__file__).resolve().parent.parent


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    data_dir = Path(tempfile.mkdtemp(prefix="ethos-wallpaper-test-"))
    log_path = data_dir / "server.log"
    proc = None
    started_by_us = False
    try:
        # ── 0. Isolated instance (fresh data dir) ───────────────────────────
        if _port_open(PORT):
            print(f"! Port {PORT} already in use — assuming a compatible test instance is running")
        else:
            env = dict(os.environ, ETHOS_PORT=str(PORT), ETHOS_DATA_DIR=str(data_dir))
            proc = subprocess.Popen(
                [str(REPO / ".venv/bin/python"), "-m", "uvicorn", "backend.main:app",
                 "--host", "127.0.0.1", "--port", str(PORT)],
                cwd=REPO, env=env, stdout=open(log_path, "wb"), stderr=subprocess.STDOUT,
            )
            started_by_us = True

        # Wait for the API to come up (health endpoint answers 401 when unauthenticated).
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                r = requests.get(f"{BASE}/api/system/status", timeout=2)
                if r.status_code in (200, 401):
                    break
            except requests.ConnectionError:
                pass
            time.sleep(0.5)
        else:
            print(f"!! Server did not come up on :{PORT}; log tail:")
            print(log_path.read_text()[-3000:] if log_path.exists() else "(no log)")
            return 1

        # ── 1. Login (PAM) ─────────────────────────────────────────────────
        r = requests.post(f"{BASE}/api/auth/login", json={"username": USER, "password": PASS})
        check("login via PAM", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code != 200:
            return len(failures) or 1
        token = r.json()["token"]
        H = {"Authorization": f"Bearer {token}"}

        # ── 2. Fresh user → GET is 404 (client seeds the record) ───────────
        r = requests.get(f"{BASE}/api/appearance/wallpaper", headers=H)
        check("GET wallpaper before save → 404", r.status_code == 404, f"HTTP {r.status_code}")

        # ── 3. Save a built-in wallpaper and read it back ───────────────────
        r = requests.post(f"{BASE}/api/appearance/wallpaper", headers=H, json={"wallpaper": "dusk"})
        check("POST wallpaper=dusk → ok", r.status_code == 200 and r.json().get("wallpaper") == "dusk",
              f"HTTP {r.status_code} {r.text[:120]}")

        r = requests.get(f"{BASE}/api/appearance/wallpaper", headers=H)
        body = r.json() if r.ok else {}
        check("GET wallpaper roundtrip → dusk, no custom",
              r.status_code == 200 and body.get("wallpaper") == "dusk" and body.get("has_custom") is False,
              f"{r.text[:160]}")

        # ── 4. Unknown id rejected; 'custom' without upload rejected ───────
        r = requests.post(f"{BASE}/api/appearance/wallpaper", headers=H, json={"wallpaper": "not-a-wallpaper"})
        check("POST unknown id → 400", r.status_code == 400, f"HTTP {r.status_code}")

        r = requests.post(f"{BASE}/api/appearance/wallpaper", headers=H, json={"wallpaper": "custom"})
        check("POST custom without upload → 400", r.status_code == 400, f"HTTP {r.status_code}")

        # ── 5. Upload a custom image (tiny PNG) and verify it is served back
        png = (bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d4944415478da63fcffff3f030005fe02fea72d1e8a0000000049454e44ae426082"))
        r = requests.post(f"{BASE}/api/appearance/wallpaper/upload", headers=H,
                          files={"file": ("wall.png", png, "image/png")})
        check("POST upload PNG → ok + wallpaper=custom",
              r.status_code == 200 and r.json().get("wallpaper") == "custom", f"HTTP {r.status_code} {r.text[:160]}")

        r = requests.get(f"{BASE}/api/appearance/wallpaper", headers=H)
        body = r.json() if r.ok else {}
        check("GET after upload → custom, has_custom=true",
              body.get("wallpaper") == "custom" and body.get("has_custom") is True, f"{r.text[:160]}")

        # Image endpoint: query-token auth (the <img>-style path) + byte identity.
        r = requests.get(f"{BASE}/api/appearance/wallpaper/image", params={"token": token})
        check("GET image via ?token= → 200 image/png, bytes match",
              r.status_code == 200 and r.headers.get("content-type", "").startswith("image/png")
              and r.content == png, f"HTTP {r.status_code} len={len(r.content)}")

        # Unsupported type rejected.
        r = requests.post(f"{BASE}/api/appearance/wallpaper/upload", headers=H,
                          files={"file": ("evil.gif", b"GIF89a", "image/gif")})
        check("POST upload GIF → 400", r.status_code == 400, f"HTTP {r.status_code}")

        # ── 6. Reset: record gone (404 again) and custom file deleted ──────
        r = requests.delete(f"{BASE}/api/appearance/wallpaper", headers=H)
        check("DELETE wallpaper → ok", r.status_code == 200, f"HTTP {r.status_code} {r.text[:120]}")

        r = requests.get(f"{BASE}/api/appearance/wallpaper", headers=H)
        check("GET after reset → 404 (record removed)", r.status_code == 404, f"HTTP {r.status_code}")

        custom_file = data_dir / "wallpapers" / f"{USER}.png"
        check("custom image file deleted on reset", not custom_file.exists(), str(custom_file))

        # ── 7. SPA shell ships the new wallpaper markup ────────────────────
        r = requests.get(f"{BASE}/")
        html = r.text
        check("index.html has wallpaper-aurora + esm-v7 entry",
              "wallpaper-aurora" in html and "esm-v7" in html, f"HTTP {r.status_code}")

        # ── 8. Unauthenticated access rejected ─────────────────────────────
        r = requests.get(f"{BASE}/api/appearance/wallpaper")
        check("GET without token → 401", r.status_code == 401, f"HTTP {r.status_code}")

    finally:
        if proc is not None and started_by_us:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(data_dir, ignore_errors=True)

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILURE(S): ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
