#!/usr/bin/env python3
"""Downloads provider API tests — live routing through a tb7.pl fixture.

Boots an isolated instance whose tb7 base URL points at a local fixture server,
so the whole flow (config masking → verify → hoster routing → link unlock →
download) runs without touching tb7.pl or production state.

Covers:
  * GET /api/downloads/providers metadata, slots and no secret leakage
  * saving tb7 username/password (admin-only) with a masked password
  * /verify with a masked password and with a wrong password
  * a listed hoster URL becomes a debrid_link task served through tb7.pl
  * magnets are rejected while the torrent slot is empty
  * provider slot validation (tb7 cannot be the torrent provider)
  * no credentials / per-file headers in downloads_tasks.json

Run:  .venv/bin/python tests/test_download_providers_api.py
"""

import base64
import hashlib
import hmac
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "providers-api-test-secret"
USERNAME = "test-user"
PASSWORD = "tb7-s3cret-pw"
RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _token(role: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": "1", "username": f"prov-{role}", "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base: str, method: str, path: str, token: str | None = None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


def _wait_ready(base: str, proc, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            status, _body = _call(base, "GET", "/api/system/status")
        except OSError:
            status = 0
        if status == 401:
            return True
        time.sleep(0.3)
    return False


_LOGIN_FORM = (
    '<html><body><form method="post" action="/login">'
    '<input type="password" name="password">'
    "<button>Zaloguj</button></form></body></html>"
)
_HOME_OK = (
    "<html><body>Wyloguj"
    "<div>Dostęp Premium ważny do <b>01.12.2026 / 12:00</b></div>"
    "<div>Pozostały Limit Premium do wykorzystania: <b>8,5 GB</b></div>"
    "</body></html>"
)
FILE_BYTES = b"hello-from-tb7"


class _Fixture(http.server.BaseHTTPRequestHandler):
    port = 0

    def log_message(self, *args):
        return

    def _send(self, body: str | bytes, status: int = 200, cookie: str = "", content_type="text/html; charset=utf-8"):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _authorised(self) -> bool:
        return "sid=ok" in (self.headers.get("Cookie") or "")

    def do_GET(self):
        if self.path == "/login":
            self._send(_LOGIN_FORM)
        elif self.path == "/jdhostingi.txt":
            self._send("rapidgator.net\nk2s.cc\nnitroflare.com\n")
        elif self.path == "/":
            self._send(_HOME_OK if self._authorised() else _LOGIN_FORM)
        elif self.path == "/file.bin":
            self._send(FILE_BYTES, content_type="application/octet-stream")
        else:
            self._send("not found", status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if self.path == "/login":
            if form.get("login") == [USERNAME] and form.get("password") == [PASSWORD]:
                self._send(_HOME_OK, cookie="sid=ok; Path=/")
            else:
                self._send(_LOGIN_FORM + "<div class='alert'>Błędne dane logowania</div>")
            return
        if self.path == "/mojekonto/sciagaj":
            if not self._authorised():
                self._send(_LOGIN_FORM)
                return
            if form.get("step") == ["1"]:
                self._send(
                    '<div class="name">Example.File.part1.rar</div>'
                    f'Rozmiar: <span class="type red">{len(FILE_BYTES)} B</span>'
                    '<form><input type="checkbox" name="0"></form>'
                )
                return
            if form.get("step") == ["2"]:
                self._send(f'<a href="http://localhost:{self.port}/file.bin" target="_blank">Pobierz</a>')
                return
        self._send("not found", status=404)


def main() -> int:
    fixture_port = _free_port()
    _Fixture.port = fixture_port
    fixture = http.server.ThreadingHTTPServer(("127.0.0.1", fixture_port), _Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    tb7_base = f"http://127.0.0.1:{fixture_port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-providers-api-"))
    download_dir = Path(tempfile.mkdtemp(prefix="ethos-providers-dl-"))
    log_path = data_dir / "server.log"
    env = {
        **os.environ,
        "ETHOS_PORT": str(port),
        "ETHOS_DATA_DIR": str(data_dir),
        "ETHOS_JWT_SECRET": SECRET,
        "ETHOS_TB7_BASE": tb7_base,
    }
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    admin = _token("admin")
    user = _token("user")

    try:
        if not _wait_ready(base, proc):
            print("Instance failed to start. Log tail:")
            print(log_path.read_text()[-3000:])
            return 1

        print("Provider catalogue")
        status, body = _call(base, "GET", "/api/downloads/providers", admin)
        catalogue = (body or {}).get("providers") or []
        by_id = {p.get("id"): p for p in catalogue}
        check("GET /providers → 200", status == 200, f"HTTP {status}")
        check("four providers are listed", len(catalogue) == 4, str([p.get("id") for p in catalogue]))
        check("tb7 is a multihoster", by_id.get("tb7", {}).get("kind") == "multihoster")
        check("tb7 lists its hosters",
              by_id.get("tb7", {}).get("hosters") == ["rapidgator.net", "k2s.cc", "nitroflare.com"],
              str(by_id.get("tb7", {}).get("hosters")))
        check("slots are derived from capabilities",
              set((body or {}).get("slots", {}).get("torrent") or []) == {"realdebrid", "alldebrid", "torbox"}
              and set((body or {}).get("slots", {}).get("link") or []) == {"alldebrid", "tb7"})
        check("no provider is active yet", (body or {}).get("active") == {"torrent": "", "link": ""})

        print("\nSave credentials")
        status, cfg = _call(base, "POST", "/api/downloads/config", admin, {
            "download_dir": str(download_dir),
            "debrid": {
                "link_provider": "tb7",
                "tb7_username": USERNAME,
                "tb7_password": PASSWORD,
            },
        })
        db = (cfg or {}).get("debrid") or {}
        check("config save → 200", status == 200, f"HTTP {status}")
        check("link slot is set", db.get("link_provider") == "tb7", str(db))
        check("username is visible", db.get("tb7_username") == USERNAME, str(db.get("tb7_username")))
        check("password is masked", db.get("tb7_password") == "****" + PASSWORD[-4:], str(db.get("tb7_password")))
        check("masked password differs from the real one", PASSWORD not in json.dumps(cfg))

        status, ucfg = _call(base, "GET", "/api/downloads/config", user)
        check("user config masks the password too",
              ((ucfg or {}).get("debrid") or {}).get("tb7_password") == "****" + PASSWORD[-4:]
              and PASSWORD not in json.dumps(ucfg))

        status, body = _call(base, "GET", "/api/downloads/providers", admin)
        check("GET /providers never returns the password", PASSWORD not in json.dumps(body))
        tb7_entry = next((p for p in (body or {}).get("providers") or [] if p.get("id") == "tb7"), {})
        tb7_fields = {f.get("name"): f for f in tb7_entry.get("credentials") or []}
        check("tb7 declares username + password",
              set(tb7_fields) == {"tb7_username", "tb7_password"}, str(tb7_fields))
        check("password field is marked as set", tb7_fields.get("tb7_password", {}).get("set") is True)
        check("username field is not secret", tb7_fields.get("tb7_username", {}).get("secret") is False)

        print("\nVerify")
        status, body = _call(base, "POST", "/api/downloads/verify", admin, {
            "provider": "tb7",
            "credentials": {"tb7_username": USERNAME, "tb7_password": "****" + PASSWORD[-4:]},
        })
        account = (body or {}).get("account") or {}
        check("masked password verifies with the stored one", status == 200, f"HTTP {status}: {body}")
        check("account summary is returned",
              account.get("premium_until") == "01.12.2026 / 12:00"
              and account.get("traffic_left_bytes") == int(8.5 * 1000 ** 3), str(account))
        status, body = _call(base, "POST", "/api/downloads/verify", admin, {
            "provider": "tb7",
            "credentials": {"tb7_username": USERNAME, "tb7_password": "wrong-password"},
        })
        check("wrong password → 401", status == 401, f"HTTP {status}: {body}")
        status, body = _call(base, "POST", "/api/downloads/verify", admin, {"provider": "nope"})
        check("unknown provider → 400", status == 400, f"HTTP {status}")
        check("user cannot verify", _call(base, "POST", "/api/downloads/verify", user,
                                          {"provider": "tb7"})[0] == 403)

        print("\nRouting")
        status, body = _call(base, "POST", "/api/downloads/tasks", admin,
                             {"url": "https://rapidgator.net/file/abc"})
        task_id = (body or {}).get("id") if isinstance(body, dict) else None
        check("listed hoster URL → debrid_link task", status == 200 and (body or {}).get("kind") == "debrid_link",
              f"HTTP {status}: {body}")
        check("task records the tb7 provider", (body or {}).get("provider") == "tb7", str((body or {}).get("provider")))

        deadline = time.time() + 30
        final = {}
        while time.time() < deadline and task_id:
            status, final = _call(base, "GET", f"/api/downloads/tasks/{task_id}", admin)
            if (final or {}).get("status") in ("completed", "error", "canceled"):
                break
            time.sleep(0.4)
        check("tb7 link download completes", (final or {}).get("status") == "completed",
              f"status={((final or {}).get('status'))} error={((final or {}).get('error'))}")
        dest = download_dir / "Example.File.part1.rar"
        check("file is written with the provider name", dest.is_file(), str(dest))
        if dest.is_file():
            check("file content matches", dest.read_bytes() == FILE_BYTES, str(dest.stat().st_size))

        status, body = _call(base, "POST", "/api/downloads/tasks", admin,
                             {"url": "magnet:?xt=urn:btih:" + "a" * 40})
        check("magnet without a torrent provider → 400", status == 400, f"HTTP {status}: {body}")
        detail = str((body or {}).get("detail") or "")
        check("magnet error explains the tb7 limitation", "tb7" in detail and "torrent" in detail.lower(), detail)

        status, body = _call(base, "POST", "/api/downloads/tasks", admin,
                             {"url": "https://example.com/plain-file.bin"})
        check("an unlisted host falls back to the HTTP engine",
              status == 200 and (body or {}).get("kind") == "http", f"HTTP {status}: {body}")

        print("\nSlot validation")
        status, cfg = _call(base, "POST", "/api/downloads/config", admin, {"debrid": {"provider": "tb7"}})
        check("tb7 cannot become the torrent provider",
              ((cfg or {}).get("debrid") or {}).get("provider") == ""
              and ((cfg or {}).get("debrid") or {}).get("link_provider") == "tb7", str((cfg or {}).get("debrid")))
        status, cfg = _call(base, "POST", "/api/downloads/config", admin,
                            {"debrid": {"provider": "realdebrid", "link_provider": "torbox"}})
        check("torbox cannot become the link provider",
              ((cfg or {}).get("debrid") or {}).get("link_provider") == ""
              and ((cfg or {}).get("debrid") or {}).get("provider") == "realdebrid", str((cfg or {}).get("debrid")))
        status, cfg = _call(base, "POST", "/api/downloads/config", user,
                            {"debrid": {"link_provider": "", "tb7_password": "user-overwrite"}})
        check("a non-admin cannot change provider credentials",
              ((cfg or {}).get("debrid") or {}).get("link_provider") == ""
              and ((cfg or {}).get("debrid") or {}).get("tb7_password") == "****" + PASSWORD[-4:],
              str((cfg or {}).get("debrid")))

        print("\nPersistence")
        tasks_file = data_dir / "downloads_tasks.json"
        raw = tasks_file.read_text(encoding="utf-8") if tasks_file.is_file() else ""
        check("tasks store has no credentials", PASSWORD not in raw and USERNAME not in raw)
        check("tasks store has no per-file headers", "_link_url_headers" not in raw)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        fixture.shutdown()
        fixture.server_close()

    print(f"\n{'FAIL' if any(not ok for _n, ok, _d in RESULTS) else 'OK'} — "
          f"{sum(1 for _n, ok, _d in RESULTS if not ok)} failure(s)")
    return 1 if any(not ok for _n, ok, _d in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
