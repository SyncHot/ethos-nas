"""tb7.pl multihoster client (blocking, stdlib only).

tb7.pl is a Polish multihoster: one premium account generates a direct link for
a file hosted on a supported file hoster (RapidGator, K2S, Nitroflare, …). The
public site has no documented API, so this client speaks the same web flow the
site itself uses:

1. ``GET /login`` → ``POST /login`` with ``login`` + ``password`` (cookie session).
2. ``POST /mojekonto/sciagaj`` ``step=1&content=<url>`` → file name/size page.
3. ``POST /mojekonto/sciagaj`` ``step=2&0=on`` → page with the direct link,
   rendered as ``<a href="…">Pobierz</a>``.

The premium validity and remaining daily transfer are scraped from the logged-in
home page. The hoster list is the JDownloader-compatible ``/jdhostingi.txt``
file, cached for 12 hours with a static fallback.

Everything here runs inside the download worker threads. Credentials are never
logged and the session cookie is only attached to links served by tb7 itself.

The pyload project was used as a protocol reference only — this is an
independent implementation (EthOS NAS is MIT, pyload is GPLv3).
"""

import html as _html
import http.cookiejar
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from backend.config import settings
from backend.services.debrid import ProviderHTTPError

logger = logging.getLogger("ethos-nas.services.tb7")

BASE_URL = settings.TB7_BASE_URL.rstrip("/")
LOGIN_PATH = "/login"
UNLOCK_PATH = "/mojekonto/sciagaj"
HOSTERS_PATH = "/jdhostingi.txt"

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 EthOS-DownloadStation/2.0")
_REQUEST_TIMEOUT = 30
_HOSTERS_TIMEOUT = 10  # the catalogue endpoint must not hang while tb7 is offline
_HOSTERS_TTL = 12 * 3600
_ACCOUNT_TTL = 300
_SESSION_TTL = 30 * 60

# Used when the live hoster list cannot be fetched. Kept in sync with
# https://tb7.pl/jdhostingi.txt (the file repeats domains in mixed case).
_FALLBACK_HOSTERS = (
    "ddownload.com", "fboom.me", "hot4share.com", "katfile.com", "k2s.cc",
    "nitroflare.com", "pobieraj.net", "pobieraj.to", "rapidgator.net",
    "twojplik.pl", "twojplik.to", "uploadgig.com", "uploady.io", "wrzucaj.pl",
    "wrzuta.net",
)

_NAME_RE = re.compile(r'<div[^>]*class="[^"]*\bname\b[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
_SIZE_RE = re.compile(r"Rozmiar:\s*<span[^>]*>\s*([\d.,]+)\s*([A-Za-z]+)", re.I)
_DOWNLOAD_RE = re.compile(
    r'<a\s[^>]*href="(https?://[^"]+)"[^>]*>\s*(?:<[^>]+>\s*)*Pobierz\s*(?:<[^>]+>\s*)*</a>',
    re.I | re.S,
)
_ALERT_RE = re.compile(
    r'<div[^>]*class="[^"]*(?:alert|error|blad|błąd|komunikat|notice)[^"]*"[^>]*>(.*?)</div>',
    re.I | re.S,
)
_VALID_RE = re.compile(r"Dostęp Premium ważny do\s*<b>\s*([^<]+?)\s*</b>", re.I | re.S)
_TRAFFIC_RE = re.compile(
    r"Pozostały Limit Premium[^<]{0,120}<b>\s*([\d.,]+)\s*([A-Za-z]+)", re.I | re.S
)
_HIDDEN_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_INPUT_ATTR_RE = re.compile(r'([a-zA-Z_:][\w:.-]*)\s*=\s*"([^"]*)"')
_LOGIN_FORM_RE = re.compile(r'name="(?:login|password)"', re.I)

_UNIT_BYTES = {
    "b": 1,
    "kb": 1000, "kib": 1024,
    "mb": 1000 ** 2, "mib": 1024 ** 2,
    "gb": 1000 ** 3, "gib": 1024 ** 3,
    "tb": 1000 ** 4, "tib": 1024 ** 4,
}


class _Session:
    """One logged-in cookie session per tb7 username (memory only)."""

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self._password = password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.logged_in = False
        self.created_at = time.time()
        self.lock = threading.Lock()

    def fresh(self) -> bool:
        return self.logged_in and (time.time() - self.created_at) < _SESSION_TTL


_sessions: dict = {}
_sessions_lock = threading.Lock()

_hosters_cache: dict = {"at": 0.0, "hosts": [], "ttl": _HOSTERS_TTL}
_hosters_lock = threading.Lock()

_account_cache: dict = {}
_account_lock = threading.Lock()


# ── Small helpers ────────────────────────────────────────────────────────────

def _host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _clean_html(fragment: str) -> str:
    """Visible text of an HTML fragment (bounded, entity-decoded)."""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", fragment or "", flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()[:300]


def parse_size(value: str, unit: str) -> int:
    """'10,5' + 'GB' → bytes; unknown units return 0."""
    try:
        number = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0
    factor = _UNIT_BYTES.get(str(unit or "").strip().lower(), 0)
    return int(number * factor) if factor else 0


def _friendly_error(page: str) -> str:
    """Map the Polish error text tb7 renders into an English task error."""
    text = _clean_html(page).lower()
    if not text:
        return ""
    if "nieaktyw" in text or "nie aktywn" in text or "usunięt" in text:
        return "tb7.pl reports the link as inactive or removed"
    if "nie obsług" in text or "nieobsług" in text or "nieobslug" in text:
        return "tb7.pl does not support this hoster"
    if "limit" in text and ("wykorzyst" in text or "przekrocz" in text or "brak" in text):
        return "tb7.pl premium transfer limit exceeded"
    if "premium" in text and ("wygas" in text or "nieaktyw" in text or "brak" in text):
        return "tb7.pl premium access is not active"
    if "hasło" in text or "haslo" in text or "zaloguj" in text or "login" in text:
        return "tb7.pl rejected the login"
    return ""


def _page_error(page: str, fallback: str) -> str:
    friendly = _friendly_error(page)
    if friendly:
        return friendly
    for match in _ALERT_RE.finditer(page or ""):
        text = _clean_html(match.group(1))
        if text:
            return text
    return fallback


def _looks_logged_in(page: str) -> bool:
    return "wyloguj" in (page or "").lower()


def _looks_logged_out(page: str) -> bool:
    text = (page or "")
    return not _looks_logged_in(text) and bool(_LOGIN_FORM_RE.search(text))


def _hidden_fields(page: str) -> dict:
    fields = {}
    for tag in _HIDDEN_INPUT_RE.findall(page or ""):
        attrs = {k.lower(): v for k, v in _INPUT_ATTR_RE.findall(tag)}
        if str(attrs.get("type") or "").lower() != "hidden":
            continue
        name = attrs.get("name")
        if not name or name in ("login", "password"):
            continue
        fields[name] = attrs.get("value") or ""
    return fields


def _request(session: _Session, path: str, data: dict = None) -> str:
    """GET (no data) or POST form data; returns the decoded response body."""
    url = path if path.startswith(("http://", "https://")) else BASE_URL + path
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    req.add_header("User-Agent", _UA)
    req.add_header("Accept-Language", "pl,en;q=0.8")
    try:
        with session.opener.open(req, timeout=_REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = _clean_html(exc.read().decode("utf-8", "replace"))
        except Exception:
            pass  # best-effort error body — status alone is still useful
        if exc.code in (401, 403):
            raise ProviderHTTPError(401, detail or "tb7.pl rejected the credentials") from exc
        raise ProviderHTTPError(exc.code, detail or f"tb7.pl answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise RuntimeError(f"tb7.pl is unreachable: {exc}") from exc


def _login(session: _Session) -> None:
    """Log in; raises ProviderHTTPError(401) when tb7 rejects the credentials."""
    page = _request(session, LOGIN_PATH)
    data = _hidden_fields(page)
    data.update({"login": session.username, "password": session._password})
    page = _request(session, LOGIN_PATH, data)
    if _looks_logged_in(page):
        session.logged_in = True
        session.created_at = time.time()
        logger.info("tb7.pl: session established")
        return
    detail = _friendly_error(page) or "tb7.pl did not accept the username/password"
    # Never log the credentials or the body (it may echo the login field).
    raise ProviderHTTPError(401, detail)


def _session_for(creds: dict) -> _Session:
    username = str((creds or {}).get("tb7_username") or "").strip()
    password = str((creds or {}).get("tb7_password") or "")
    if not username or not password:
        raise RuntimeError("tb7.pl username and password are not configured (set them in Settings)")
    with _sessions_lock:
        session = _sessions.get(username)
        if session is None or session._password != password:
            session = _sessions[username] = _Session(username, password)
        if session.fresh():
            return session
        with session.lock:
            if not session.fresh():
                _login(session)
        return session


def _call(creds: dict, path: str, data: dict = None) -> str:
    """Request a logged-in page; a dropped session is re-established once."""
    session = _session_for(creds)
    with session.lock:
        if not session.fresh():
            _login(session)
        page = _request(session, path, data)
        if _looks_logged_out(page):
            _login(session)
            page = _request(session, path, data)
        return page


def _session_cookie(session: _Session) -> str:
    base_host = _host(BASE_URL)
    pairs = []
    for cookie in session.jar:
        domain = (cookie.domain or "").lstrip(".").lower()
        if domain and (base_host == domain or base_host.endswith("." + domain)):
            pairs.append(f"{cookie.name}={cookie.value}")
    return "; ".join(pairs)


# ── Public API ───────────────────────────────────────────────────────────────

def resolve_link(creds: dict, url: str, stop=None) -> dict:
    """Generate a direct premium link for one file-hoster URL.

    Returns ``{url, name, size, headers}``; ``url`` is empty when the task was
    stopped between the two form steps (the worker then skips phase 3).
    """
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise RuntimeError("tb7.pl can only resolve http(s) file-hoster links")

    page = _call(creds, UNLOCK_PATH, {"step": "1", "content": url})
    if _looks_logged_out(page):
        raise ProviderHTTPError(401, "tb7.pl rejected the login")

    name_match = _NAME_RE.search(page)
    size_match = _SIZE_RE.search(page)
    name = _clean_html(name_match.group(1)) if name_match else ""
    size = parse_size(size_match.group(1), size_match.group(2)) if size_match else 0
    if not name and not size and not re.search(r'name="0"', page):
        raise RuntimeError(_page_error(page, "tb7.pl did not recognise this link"))

    if stop is not None and stop.is_set():
        return {"url": "", "name": name, "size": size, "headers": {}}

    page = _call(creds, UNLOCK_PATH, {"step": "2", "0": "on"})
    if "nieaktywne linki" in page.lower():
        raise RuntimeError("tb7.pl reports the link as inactive or removed")
    link_match = _DOWNLOAD_RE.search(page)
    if not link_match:
        raise RuntimeError(_page_error(page, "tb7.pl returned no download link"))
    link = _html.unescape(link_match.group(1))

    fallback = os.path.basename(parsed.path.rstrip("/")) or "download.bin"
    name = name or fallback
    headers = {"Referer": BASE_URL + UNLOCK_PATH}
    # The session cookie is a credential: only send it back to tb7 itself.
    if _host(link) == _host(BASE_URL):
        cookie = _session_cookie(_session_for(creds))
        if cookie:
            headers["Cookie"] = cookie
    return {"url": link, "name": name, "size": size, "headers": headers}


def _parse_account(page: str) -> dict:
    account: dict = {}
    valid = _VALID_RE.search(page or "")
    if valid:
        account["premium_until"] = valid.group(1).strip()
    traffic = _TRAFFIC_RE.search(page or "")
    if traffic:
        account["traffic_left_bytes"] = parse_size(traffic.group(1), traffic.group(2))
    account["premium"] = bool(account.get("premium_until")) or bool(account.get("traffic_left_bytes"))
    return account


def account_info(creds: dict) -> dict:
    """Current premium validity / remaining transfer (cached for 5 minutes)."""
    username = str((creds or {}).get("tb7_username") or "").strip()
    now = time.time()
    with _account_lock:
        cached = _account_cache.get(username)
        if cached and now - cached[0] < _ACCOUNT_TTL:
            return dict(cached[1])
    page = _call(creds, "/")
    account = _parse_account(page)
    if not account.get("premium_until") and not account.get("traffic_left_bytes"):
        logger.warning("tb7.pl: account summary not found on the home page")
    with _account_lock:
        _account_cache[username] = (now, dict(account))
    return account


def cached_account(creds: dict) -> dict:
    """Last account summary without any network call (for the provider list)."""
    username = str((creds or {}).get("tb7_username") or "").strip()
    with _account_lock:
        cached = _account_cache.get(username)
    return dict(cached[1]) if cached else {}


def verify(creds: dict) -> dict:
    """Log in and return ``{ok, account}``; raises ProviderHTTPError(401) on a bad login."""
    page = _call(creds, "/")
    account = _parse_account(page)
    with _account_lock:
        _account_cache[str((creds or {}).get("tb7_username") or "").strip()] = (time.time(), dict(account))
    return {"ok": True, "account": account}


def hosters() -> list:
    """Supported hoster domains from ``/jdhostingi.txt`` (12 h cache)."""
    now = time.time()
    with _hosters_lock:
        if _hosters_cache["at"] and now - _hosters_cache["at"] < _hosters_cache["ttl"]:
            return list(_hosters_cache["hosts"])
    hosts: list = []
    ttl = _HOSTERS_TTL
    try:
        req = urllib.request.Request(BASE_URL + HOSTERS_PATH, method="GET")
        req.add_header("User-Agent", _UA)
        with urllib.request.urlopen(req, timeout=_HOSTERS_TIMEOUT) as resp:  # nosec B310 — fixed constant URL
            raw = resp.read().decode("utf-8", "replace")
        for line in raw.splitlines():
            host = line.strip().lower()
            if not host or host.startswith("#"):
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}", host):
                continue
            if host not in hosts:
                hosts.append(host)
    except Exception as exc:  # noqa: BLE001 — a stale static list is better than no routing
        logger.warning("tb7.pl hoster list unavailable (%s) — using the built-in list", exc)
        hosts = list(_FALLBACK_HOSTERS)
        ttl = 300  # retry sooner than a successful fetch
    if hosts:
        with _hosters_lock:
            _hosters_cache["at"] = now
            _hosters_cache["hosts"] = list(hosts)
            _hosters_cache["ttl"] = ttl
    return hosts


def forget_sessions() -> None:
    """Drop cached sessions/caches (tests and credential changes)."""
    with _sessions_lock:
        _sessions.clear()
    with _account_lock:
        _account_cache.clear()
    with _hosters_lock:
        _hosters_cache["at"] = 0.0
        _hosters_cache["hosts"] = []
        _hosters_cache["ttl"] = _HOSTERS_TTL
