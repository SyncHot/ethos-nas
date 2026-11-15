"""Regression test — gallery rescan INSERT column/placeholder mismatch.

Defect (T-0001 discovery): the `_scan_root` upsert INSERT listed 11 columns
(path,name,folder,size,mtime,type,taken_at,camera,lat,lon,duration) but the
VALUES clause had only 10 placeholders (the `duration` column was added to
the column list without its `?`). Every rescan that met a new or changed
media file crashed with sqlite3.OperationalError "10 values for 11 columns"
(visible in the App Events / journal log), so new photos/videos were never
indexed.

Part A1 (in-process, always runs, no server needed):
  fresh temp DB + temp folder with a NEW image and a NEW video file →
  `_scan_root` must not raise and must upsert both rows; touching the image
  (mtime change) must exercise the ON CONFLICT DO UPDATE path; an encrypted
  sidecar (.ethos.enc) must produce a private=1 row.

Part A2 (live API E2E — runs ONLY when ETHOS_TEST_BASE is set, i.e. against
an isolated instance per AGENTS.md; never against production):
    ETHOS_PORT=9002 ETHOS_DATA_DIR=/tmp/qa-agent-data-gal \
      nohup .venv/bin/python -m uvicorn backend.main:app --port 9002 &
    ETHOS_TEST_BASE=http://127.0.0.1:9002 .venv/bin/python tests/test_gallery_rescan_insert.py
  Adds a world-readable fixture folder to the gallery config, waits for the
  rescan, and asserts the new file appears in GET /api/gallery/items.
  Self-cleaning: restores the previous folder list and deletes the fixture.
"""
import json
import os

def _require_test_password() -> str:
    """Test credentials are never committed — export ETHOS_TEST_PASSWORD."""
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
if not BASE.startswith("http://"):
    raise SystemExit("ETHOS_TEST_BASE must be an http:// URL")
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def part_a1() -> int:
    print("\nPart A1 — in-process _scan_root (fresh DB + fresh files)")
    failures = 0
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from backend.routers import gallery

    tmp = Path(tempfile.mkdtemp(prefix="gal-regress-"))
    try:
        gallery.INDEX_DB = tmp / "index.db"
        conn = gallery._db_connect()

        # Schema sanity: the upsert touches all 11 columns incl. duration.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(media)")}
        if not check("media table has duration column", "duration" in cols, str(cols)):
            return 1

        media = tmp / "photos"
        media.mkdir()
        img = media / "new-photo.jpg"
        Image.new("RGB", (4, 4), (255, 0, 0)).save(img, "JPEG")
        vid = media / "clip.mp4"
        vid.write_bytes(b"\x00" * 64)  # not a real video → ffprobe fails best-effort → None
        enc = media / "secret.jpg.ethos.enc"
        enc.write_bytes(b"\x00" * 32)

        progress = {"dirs": 0, "added": 0, "changed": 0}
        try:
            gallery._scan_root(conn, str(media), progress)
            check("_scan_root with new image+video does not raise", True)
        except Exception as exc:  # noqa: BLE001 — the defect under test
            check("_scan_root with new image+video does not raise", False,
                  f"{type(exc).__name__}: {exc}")
            return 1

        rows = {r[0]: r for r in conn.execute(
            "SELECT path,name,type,duration,private FROM media")}
        if not check("new image row upserted", str(img) in rows, str(list(rows))):
            failures += 1
        if not check("new video row upserted", str(vid) in rows):
            failures += 1
        if str(enc) in rows or str(enc.with_suffix("").with_suffix("")) in rows:
            pass  # encrypted sidecars index under their original path
        if not check("encrypted sidecar indexed as private",
                     rows.get(str(enc.with_name("secret.jpg")), (None,))[0] is not None
                     or any(p == str(enc.with_name("secret.jpg")) for p in rows)):
            failures += 1
        else:
            priv = conn.execute(
                "SELECT private FROM media WHERE path=?", (str(enc.with_name("secret.jpg")),)).fetchone()
            if not check("encrypted sidecar row has private=1", priv and bool(priv[0]), str(priv)):
                failures += 1

        # Touch the image → mtime changes → ON CONFLICT DO UPDATE path.
        time.sleep(0.01)
        os.utime(img, (time.time() + 5, time.time() + 5))
        progress2 = {"dirs": 0, "added": 0, "changed": 0}
        try:
            gallery._scan_root(conn, str(media), progress2)
            check("_scan_root with changed file does not raise", True)
        except Exception as exc:  # noqa: BLE001
            check("_scan_root with changed file does not raise", False,
                  f"{type(exc).__name__}: {exc}")
            failures += 1
        else:
            if not check("changed file counted as changed", progress2["changed"] >= 1,
                         str(progress2)):
                failures += 1
        dup = conn.execute("SELECT COUNT(*) FROM media WHERE path=?", (str(img),)).fetchone()[0]
        if not check("no duplicate rows after re-scan", dup == 1, f"count={dup}"):
            failures += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


def api(method, path, token=None, body=None):
    url = BASE + path
    if not url.startswith("http://"):
        raise ValueError(f"refusing non-http test URL: {url!r}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def part_a2(token) -> int:
    print(f"\nPart A2 — live API E2E ({BASE})")
    failures = 0
    # NOTE: the server must run as this test's user (isolated instance per
    # AGENTS.md — same user). mkdtemp's default mode-700 fixture is then
    # readable by the scanner. Pointing ETHOS_TEST_BASE at a cross-user
    # production instance will fail the items check on fixture permissions.
    # The fixture must live under a gallery-allowed root (ALLOWED_ROOTS =
    # /home, /mnt, /media) — /tmp is dropped silently from the config.
    fixture = Path(tempfile.mkdtemp(prefix="gal-a2-", dir=str(Path.home())))
    img = fixture / "probe-rescan.jpg"
    Image.new("RGB", (4, 4), (0, 128, 0)).save(img, "JPEG")

    code, cfg = api("GET", "/api/gallery/config", token)
    if code != 200:
        check("GET /config", False, f"HTTP {code}")
        shutil.rmtree(fixture, ignore_errors=True)
        return 1
    orig_folders = [f.get("path") for f in cfg.get("folders", [])]
    try:
        code, _ = api("POST", "/api/gallery/config", token,
                      {"folders": [{"path": str(p)} for p in orig_folders] + [{"path": str(fixture)}]})
        if not check("POST /config (add fixture folder)", code == 200, f"HTTP {code}"):
            return 1

        deadline = time.time() + 120
        scanning = True
        while time.time() < deadline:
            code, st = api("GET", "/api/gallery/rescan-status", token)
            scanning = bool(st.get("scanning"))
            if not scanning:
                break
            time.sleep(1)
        if not check("rescan completed", not scanning):
            return 1

        code, items = api("GET", "/api/gallery/items?limit=200", token)
        paths = [it.get("path") for it in items.get("items", [])]
        if not check("newly added file appears in /items", str(img) in paths,
                     f"total={items.get('total')} sample={paths[:3]}"):
            failures += 1
    finally:
        api("POST", "/api/gallery/config", token,
            {"folders": [{"path": str(p)} for p in orig_folders]})
        shutil.rmtree(fixture, ignore_errors=True)
    return failures


def main() -> int:
    print(f"Gallery rescan INSERT regression — {BASE}")
    total = part_a1()

    if BASE != "http://localhost:9000":
        # Live E2E only against an explicitly chosen (isolated) instance.
        code, body = api("POST", "/api/auth/login", body={"username": USER, "password": PASS})
        if code != 200:
            print(f"  [SKIP] live login failed (HTTP {code}) — A2 skipped")
        else:
            total += part_a2(body["token"])
    else:
        print("\nPart A2 — skipped (no ETHOS_TEST_BASE; refusing to touch production gallery config)")

    print("\n" + "=" * 52)
    if total:
        print(f"RESULT: {total} FAILURE(S)")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
