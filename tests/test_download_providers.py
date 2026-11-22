#!/usr/bin/env python3
"""Provider registry + tb7.pl client unit tests (offline).

A local HTTP server mimics the tb7.pl pages the client scrapes (login form,
link generator steps, account summary, hoster list), so no network is used.

Run:  .venv/bin/python tests/test_download_providers.py
"""

import http.server
import os
import socket
import sys
import tempfile
import threading
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before backend.config is imported.
_DATA = tempfile.mkdtemp(prefix="ethos-providers-test-")
os.environ.setdefault("ETHOS_DATA_DIR", _DATA)

import backend.services.debrid as debrid  # noqa: E402
import backend.services.download_providers as providers  # noqa: E402
import backend.services.download_tasks as download_tasks  # noqa: E402
import backend.services.tb7 as tb7  # noqa: E402

_failures = 0

USERNAME = "test-user"
PASSWORD = "tb7-s3cret-pw"


def check(name: str, ok: bool, detail: str = "") -> None:
    global _failures
    if not ok:
        _failures += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_LOGIN_FORM = (
    '<html><body><form method="post" action="/login">'
    '<input type="hidden" name="csrf" value="token-123">'
    '<input name="login"><input type="password" name="password">'
    "<button>Zaloguj</button></form></body></html>"
)
_HOME_OK = (
    "<html><body>Wyloguj"
    "<div>Dostęp Premium ważny do <b>01.12.2026 / 12:00</b></div>"
    "<div>Pozostały Limit Premium do wykorzystania: <b>8,5 GB</b></div>"
    "</body></html>"
)


class _Tb7Fixture(http.server.BaseHTTPRequestHandler):
    """Minimal tb7.pl stand-in; one class-level port so links point back to it."""

    port = 0

    def log_message(self, *args):  # keep the test output clean
        return

    def _send(self, body: str, status: int = 200, cookie: str = "") -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
            self._send("Rapidgator.net\nk2s.cc\nrapidgator.net\n# comment\nnot a host\nNitroflare.com\n")
        elif self.path == "/":
            self._send(_HOME_OK if self._authorised() else _LOGIN_FORM)
        elif self.path == "/file.bin":
            payload = b"hello-from-tb7"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self._send("not found", status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if self.path == "/login":
            if form.get("csrf") != ["token-123"]:
                self._send(_LOGIN_FORM + "<div class='alert'>Brak tokenu</div>")
                return
            if form.get("login") == [USERNAME] and form.get("password") == [PASSWORD]:
                self._send(_HOME_OK, cookie="sid=ok; Path=/")
            else:
                self._send(_LOGIN_FORM + "<div class='alert'>Błędne dane logowania</div>", status=200)
            return
        if self.path == "/mojekonto/sciagaj":
            if not self._authorised():
                self._send(_LOGIN_FORM)
                return
            content = (form.get("content") or [""])[0]
            if "unsupported" in content:
                self._send("<div class='alert'>Nieobsługiwany hosting</div>")
                return
            if "inactive" in content:
                self._send("<div class='alert'>Nieaktywne linki</div>")
                return
            if form.get("step") == ["1"]:
                self._send(
                    '<div class="name">Example.File.part1.rar</div>'
                    'Rozmiar: <span class="type red">1,5 GB</span>'
                    '<form><input type="checkbox" name="0"></form>'
                )
                return
            if form.get("step") == ["2"]:
                self._send(
                    f'<a href="http://localhost:{self.port}/file.bin" target="_blank">Pobierz</a>'
                )
                return
        self._send("not found", status=404)


def _start_fixture() -> tuple:
    port = _free_port()
    _Tb7Fixture.port = port
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Tb7Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


class _CaptureStore:
    def __init__(self) -> None:
        self.saved = None

    def save(self, data) -> None:
        self.saved = data


def main() -> int:
    server, base = _start_fixture()
    original_base = tb7.BASE_URL
    tb7.BASE_URL = base
    tb7.forget_sessions()
    creds = {"tb7_username": USERNAME, "tb7_password": PASSWORD}
    try:
        print("Registry metadata")
        specs = providers.all_specs()
        check("four providers are registered", len(specs) == 4, str([s.id for s in specs]))
        check("tb7 is a multihoster with link unlock",
              providers.get("tb7").kind == "multihoster"
              and providers.supports("tb7", providers.CAP_LINK_UNLOCK))
        check("tb7 does not claim torrent support", not providers.is_torrent_provider("tb7"))
        check("AllDebrid supports both slots", providers.eligible_slots("alldebrid") == ["torrent", "link"])
        check("Real-Debrid is torrent-only", providers.eligible_slots("realdebrid") == ["torrent"])
        check("unknown provider has no slots", providers.eligible_slots("nope") == [])

        print("\nCredentials")
        db = {
            "provider": "torbox", "link_provider": "tb7",
            "torbox_api_key": "AAAAAAAAAAAAAAAAAAAA",
            "tb7_username": USERNAME, "tb7_password": PASSWORD,
        }
        cfg = {"debrid": db}
        torrent_name, torrent_creds = providers.torrent_provider(cfg)
        link_name, link_creds = providers.link_provider(cfg)
        check("torrent slot resolves TorBox", torrent_name == "torbox")
        check("link slot resolves tb7", link_name == "tb7")
        check("tb7 credentials are complete", providers.credentials_complete("tb7", link_creds))
        check("primary secret is the password",
              providers.primary_secret("tb7", link_creds) == PASSWORD)
        masked = providers.mask_credentials(db)
        check("password is masked", masked["tb7_password"] == "****" + PASSWORD[-4:],
              masked["tb7_password"])
        check("username stays visible", masked["tb7_username"] == USERNAME)
        check("torrent key is masked", masked["torbox_api_key"].startswith("****"))
        check("masking does not mutate the source", db["tb7_password"] == PASSWORD)

        merged = providers.merge_credential_values(db, {"tb7_password": "****-pw", "tb7_username": "other"})
        check("masked secret keeps the stored value", merged["tb7_password"] == PASSWORD)
        check("non-secret field is updated", merged["tb7_username"] == "other")

        print("\nHoster routing")
        check("tb7 matches a listed hoster", providers.host_matches("tb7", "https://rapidgator.net/file/x"))
        check("tb7 matches a subdomain", providers.host_matches("tb7", "https://cdn.rapidgator.net/file/x"))
        check("tb7 rejects an unlisted host", not providers.host_matches("tb7", "https://example.com/f"))
        check("AllDebrid accepts any host", providers.host_matches("alldebrid", "https://example.com/f"))
        check("Real-Debrid does not unlock links",
              not providers.host_matches("realdebrid", "https://rapidgator.net/file/x"))
        hosts = providers.hosters("tb7")
        check("hoster list is normalised and deduplicated",
              hosts == ["rapidgator.net", "k2s.cc", "nitroflare.com"], str(hosts))

        print("\ntb7 unlock flow")
        result = tb7.resolve_link(creds, "https://rapidgator.net/file/abc")
        check("unlock returns a direct link", result["url"] == f"http://localhost:{server.server_address[1]}/file.bin",
              result.get("url", ""))
        check("unlock returns the file name", result["name"] == "Example.File.part1.rar", result.get("name", ""))
        check("unlock returns the size", result["size"] == int(1.5 * 1000 ** 3), str(result.get("size")))
        check("external link has no session cookie", "Cookie" not in result["headers"],
              str(result.get("headers")))
        check("external link carries a Referer", "Referer" in result["headers"])

        try:
            tb7.resolve_link(creds, "https://rapidgator.net/file/inactive")
        except RuntimeError as exc:
            check("inactive link is reported", "inactive" in str(exc).lower(), str(exc))
        else:
            check("inactive link is reported", False)

        try:
            tb7.resolve_link(creds, "https://rapidgator.net/file/unsupported")
        except RuntimeError as exc:
            check("unsupported hoster is reported", "does not support" in str(exc), str(exc))
        else:
            check("unsupported hoster is reported", False)

        print("\ntb7 login and account")
        account = tb7.account_info(creds)
        check("premium validity is parsed", account.get("premium_until") == "01.12.2026 / 12:00",
              str(account))
        check("remaining transfer is parsed", account.get("traffic_left_bytes") == int(8.5 * 1000 ** 3),
              str(account.get("traffic_left_bytes")))
        check("cached account is available", tb7.cached_account(creds).get("premium") is True)

        bad = {"tb7_username": USERNAME, "tb7_password": "wrong"}
        tb7.forget_sessions()
        try:
            tb7.verify(bad)
        except debrid.ProviderHTTPError as exc:
            check("bad login becomes a 401-style error", exc.status == 401, str(exc))
        else:
            check("bad login becomes a 401-style error", False)

        print("\nStop handling")
        tb7.forget_sessions()
        import threading as _threading
        stopped = _threading.Event()
        stopped.set()
        partial = tb7.resolve_link(creds, "https://rapidgator.net/file/abc", stopped)
        check("a stopped task returns no link", partial["url"] == "", partial.get("url", ""))
        check("the file name is still reported", partial["name"] == "Example.File.part1.rar")

        print("\nTransient secrets are not persisted")
        task = {
            "id": "dl_x", "url": "https://rapidgator.net/file/abc", "status": "completed",
            "_debrid_key": PASSWORD,
            "_files": [{"id": 1, "name": "a.bin", "_link_url": "https://x/y",
                        "_link_url_headers": {"Cookie": "sid=ok"}}],
        }
        store = _CaptureStore()
        download_tasks.write([task], store)
        saved = store.saved[0]
        check("_debrid_key is stripped", "_debrid_key" not in saved)
        check("per-file headers are stripped", "_link_url_headers" not in saved["_files"][0])
        check("in-memory task keeps its headers",
              task["_files"][0]["_link_url_headers"] == {"Cookie": "sid=ok"})
        check("download links are still persisted",
              saved["_files"][0]["_link_url"] == "https://x/y")
    finally:
        tb7.BASE_URL = original_base
        tb7.forget_sessions()
        server.shutdown()
        server.server_close()

    print(f"\n{'FAIL' if _failures else 'OK'} — {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
