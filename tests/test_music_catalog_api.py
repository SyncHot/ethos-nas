#!/usr/bin/env python3
"""Music catalogs + radio favourites API.

Boots its own isolated instance (throwaway port + ETHOS_DATA_DIR + test JWT
secret), so it never touches production data and needs no external fixture.
No yt-dlp calls: search suggestions and catalogs only.

    .venv/bin/python tests/test_music_catalog_api.py
"""

import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "music-catalog-test-secret"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _token(role: str = "user") -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": "catalog-test", "username": "catalog-test", "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base: str, method: str, path: str, token: str | None = None, body=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read(500).decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"detail": raw}
    except OSError as exc:
        return 0, {"detail": str(exc)}


def _wait_ready(base: str, proc: subprocess.Popen, log_path: Path, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if _call(base, "GET", "/api/system/status")[0] == 401:
            return True
        time.sleep(0.3)
    return False


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def main() -> int:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-music-catalog-"))
    log_path = data_dir / "server.log"
    env = {
        **os.environ,
        "ETHOS_PORT": str(port),
        "ETHOS_DATA_DIR": str(data_dir),
        "ETHOS_JWT_SECRET": SECRET,
        "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
    }
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )

    try:
        print(f"\n[live] isolated instance on {base}")
        if not _wait_ready(base, proc, log_path):
            print(log_path.read_text()[-3000:])
            print("  [FAIL] isolated instance did not become ready")
            return 1

        user = _token()

        print("\n[auth]")
        check("radio catalog requires a token", _call(base, "GET", "/api/music/radio/stations")[0] == 401)
        check("podcast catalog requires a token", _call(base, "GET", "/api/music/podcasts/catalog")[0] == 401)

        print("\n[radio catalog]")
        status, body = _call(base, "GET", "/api/music/radio/stations", user)
        stations = body.get("stations") or []
        categories = body.get("categories") or []
        check("200", status == 200, str(status))
        check(">= 150 stations", len(stations) >= 150, str(len(stations)))
        check("categories returned", len(categories) >= 4, str(categories))
        check("station shape", all(
            s.get("id") and s.get("name") and s.get("category") and s.get("stream_url")
            for s in stations[:20]
        ))
        check("rich metadata present", any(s.get("logo") for s in stations), "no logos")
        check("all categories used", all(any(s["category"] == c for s in stations) for c in categories))

        status, body = _call(base, "GET", f"/api/music/radio/stations?category={_q('Religijne')}", user)
        filtered = body.get("stations") or []
        check("category filter", status == 200 and filtered and all(s["category"] == "Religijne" for s in filtered),
              str([s["category"] for s in filtered][:5]))

        status, body = _call(base, "GET", f"/api/music/radio/stations?q={_q('trojka')}", user)
        hits = body.get("stations") or []
        check("diacritics-free radio search", status == 200 and hits and "troj" in hits[0]["name"].lower().replace("ó", "o"),
              str([s["name"] for s in hits[:3]]))

        status, body = _call(base, "GET", f"/api/music/radio/stations?q={_q('zzzzzz')}", user)
        check("no-match radio search", status == 200 and (body.get("total") or 0) == 0, str(body.get("total")))

        print("\n[podcast catalog]")
        status, body = _call(base, "GET", "/api/music/podcasts/catalog", user)
        podcasts = body.get("items") or []
        check("200", status == 200, str(status))
        check(">= 120 podcasts", (body.get("total") or 0) >= 120, str(body.get("total")))
        check("categories returned", len(body.get("categories") or []) >= 10, str(len(body.get("categories") or [])))
        check("podcast shape", all(
            p.get("id") and p.get("name") and p.get("rss_url") and p.get("category") for p in podcasts[:20]
        ))
        check("covers present", sum(1 for p in podcasts if p.get("cover_url")) > len(podcasts) * 0.8)

        status, body = _call(base, "GET", f"/api/music/podcasts/catalog?q={_q('kryminatorium')}", user)
        found = body.get("items") or []
        check("podcast search", status == 200 and found and "kryminator" in found[0]["name"].lower(),
              str([p["name"] for p in found[:3]]))

        status, body = _call(base, "GET", f"/api/music/podcasts/catalog?category={_q('True crime')}", user)
        cat_items = body.get("items") or []
        check("podcast category filter", status == 200 and cat_items and all(p["category"] == "True crime" for p in cat_items),
              str([p["category"] for p in cat_items][:5]))

        status, page1 = _call(base, "GET", "/api/music/podcasts/catalog?limit=10", user)
        check("pagination page size", status == 200 and len(page1.get("items") or []) == 10, str(len(page1.get("items") or [])))
        check("pagination has_more", page1.get("has_more") is True)
        status, page2 = _call(base, "GET", "/api/music/podcasts/catalog?limit=10&offset=10", user)
        ids1 = {p["id"] for p in page1.get("items") or []}
        ids2 = {p["id"] for p in page2.get("items") or []}
        check("pages are disjoint", bool(ids1) and bool(ids2) and not (ids1 & ids2))

        print("\n[search suggestions]")
        status, body = _call(base, "GET", f"/api/music/search/suggest?q={_q('trójka')}", user)
        items = body.get("items") or []
        check("suggest 200", status == 200, str(status))
        check("suggest returns radio hit", any(i.get("type") == "radio" for i in items),
              str([i.get("type") for i in items]))
        check("suggest item shape", all(i.get("type") and i.get("title") for i in items))

        print("\n[radio state]")
        status, body = _call(base, "GET", "/api/music/radio/state", user)
        check("state starts empty", status == 200 and body.get("favorites") == [] and body.get("recents") == [], str(body))

        status, body = _call(base, "PUT", "/api/music/radio/state", user, {
            "favorites": ["abc123def456", "abc123def456", "bad id!"],
            "recents": [f"id{i:02d}" for i in range(12)],
        })
        check("favorites validated + deduped", status == 200 and body.get("favorites") == ["abc123def456"], str(body.get("favorites")))
        check("recents capped", status == 200 and len(body.get("recents") or []) == 8, str(len(body.get("recents") or [])))

        status, body = _call(base, "GET", "/api/music/radio/state", user)
        check("state persisted", body.get("favorites") == ["abc123def456"] and len(body.get("recents") or []) == 8, str(body))

        status, body = _call(base, "GET", "/api/music/state", user)
        check("radio section in /state", isinstance(body.get("radio"), dict) and body["radio"].get("favorites") == ["abc123def456"], str(body.get("radio")))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
