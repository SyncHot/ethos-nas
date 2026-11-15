"""End-to-end test — Public Share Links.

Part A (API, always runs): PAM login → create links for a temp file and folder →
verify public token endpoints work WITHOUT auth (exact bytes, dir listing,
per-file download), traversal is rejected, max_downloads enforcement returns 410,
expiry metadata is sane, and revocation makes the URL 404. Self-cleaning: deletes
its own links and temp tree at the end.

Part B (UI via Playwright, auto-skips when chromium is unavailable): login → open
the Shared Links app → seeded row renders with status chip → creating a link from
the form adds a new row.

Run:  .venv/bin/python tests/test_share_links.py   (server must be up)
      ETHOS_TEST_BASE=http://127.0.0.1:9001 to target a non-default port.
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
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from fixture_dir import service_readable_tmpdir

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


def http_get(url):
    """Raw GET without auth. Returns (status_code, body_bytes)."""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def login():
    code, data = api("POST", "/api/auth/login", body={"username": USER, "password": PASS})
    if code != 200 or not isinstance(data, dict):
        raise RuntimeError(f"login failed: HTTP {code}")
    return data["token"]


# ── Part A: API E2E ───────────────────────────────────────────────────────────

def part_a(token) -> int:
    failures = 0
    print("\nPart A — API")

    payload_a = b"ethos-share-probe-A\n" * 64
    payload_b = "hello from shared folder\n".encode()
    tmpdir = service_readable_tmpdir(prefix="ethos-shares-")
    file_a = os.path.join(tmpdir, "probeA.bin")
    sub = os.path.join(tmpdir, "subfolder")
    file_b = os.path.join(sub, "probeB.txt")
    os.makedirs(sub)
    with open(file_a, "wb") as f:
        f.write(payload_a)
    with open(file_b, "wb") as f:
        f.write(payload_b)

    created_ids = []
    try:
        # ── 1. Create a file link (no options) ────────────────────────────
        code, link = api("POST", "/api/share-links", token, {"path": file_a})
        if not check("create file link → 200", code == 200 and isinstance(link, dict), f"HTTP {code} {link}"):
            return failures + 1
        created_ids.append(link["id"])
        tok = link["token"]
        pub_url = f"{BASE}/api/share-links/public/{tok}"

        if not check("link has required fields", all(k in link for k in ("id", "path", "name", "token", "created_at"))):
            failures += 1
        if not check("default name is basename", link.get("name") == "probeA.bin", str(link.get("name"))):
            failures += 1
        if not check("response includes url", (link.get("url") or "").endswith(f"/public/{tok}"), str(link.get("url"))):
            failures += 1

        # ── 2. Public download WITHOUT auth returns exact bytes ───────────
        status, body = http_get(pub_url)
        if not check("public file GET (no JWT) → 200", status == 200, f"HTTP {status}"):
            failures += 1
        if not check("downloaded bytes match source", body == payload_a, f"{len(body)} vs {len(payload_a)} bytes"):
            failures += 1

        code, links = api("GET", "/api/share-links", token)
        mine = next((l for l in (links or {}).get("links", []) if l["id"] == link["id"]), None)
        if not check("list shows the new link with status=active", mine and mine.get("status") == "active"):
            failures += 1
        if not check("download counter incremented to 1", mine and mine.get("downloads") == 1, str(mine)):
            failures += 1

        # ── 3. Directory link: listing + per-file download + traversal ────
        code, dlink = api("POST", "/api/share-links", token, {"path": sub, "name": "shared-folder"})
        if not check("create dir link → 200", code == 200 and isinstance(dlink, dict), f"HTTP {code}"):
            failures += 1
            return failures
        created_ids.append(dlink["id"])
        dtok = dlink["token"]

        status, raw = http_get(f"{BASE}/api/share-links/public/{dtok}")
        listing = json.loads(raw.decode()) if status == 200 else {}
        names = [f["name"] for f in listing.get("files", [])]
        if not check("public dir GET → JSON listing", status == 200 and listing.get("kind") == "directory" and "probeB.txt" in names, raw[:200].decode(errors="replace")):
            failures += 1

        status, body = http_get(f"{BASE}/api/share-links/public/{dtok}/download?file=probeB.txt")
        if not check("per-file download from shared dir", status == 200 and body == payload_b, f"HTTP {status}"):
            failures += 1

        status, _ = http_get(f"{BASE}/api/share-links/public/{dtok}/download?file=../probeA.bin")
        if not check("traversal ../ rejected (400)", status == 400, f"HTTP {status}"):
            failures += 1
        status, _ = http_get(f"{BASE}/api/share-links/public/{dtok}/download?file=/etc/passwd")
        if not check("absolute path rejected (400)", status == 400, f"HTTP {status}"):
            failures += 1

        # ── 4. max_downloads enforcement → 410 after cap ──────────────────
        code, mlink = api("POST", "/api/share-links", token, {"path": file_a, "max_downloads": 1})
        if not check("create link with max_downloads=1", code == 200 and isinstance(mlink, dict), f"HTTP {code}"):
            failures += 1
            return failures
        created_ids.append(mlink["id"])
        murl = f"{BASE}/api/share-links/public/{mlink['token']}"
        s1, _ = http_get(murl)
        s2, body2 = http_get(murl)
        if not check("first download within cap → 200", s1 == 200, f"HTTP {s1}"):
            failures += 1
        if not check("second download over cap → 410", s2 == 410, f"HTTP {s2} {body2[:120]!r}"):
            failures += 1

        # ── 5. Expiry metadata ────────────────────────────────────────────
        code, elink = api("POST", "/api/share-links", token, {"path": file_a, "expires_in_days": 30})
        if not check("create link with expires_in_days=30", code == 200 and isinstance(elink, dict), f"HTTP {code}"):
            failures += 1
            return failures
        created_ids.append(elink["id"])
        exp = elink.get("expires_at")
        ok_exp = False
        if exp:
            try:
                dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                delta = dt - datetime.now(timezone.utc)
                ok_exp = timedelta(days=29, hours=12) < delta <= timedelta(days=30, hours=1)
            except ValueError:
                pass
        if not check("expires_at ≈ now + 30 days", ok_exp, str(exp)):
            failures += 1

        # ── 6. Validation errors ──────────────────────────────────────────
        code, data = api("POST", "/api/share-links", token, {"path": os.path.join(tmpdir, "does-not-exist")})
        if not check("missing path → 400", code == 400, f"HTTP {code}"):
            failures += 1
        code, data = api("POST", "/api/share-links", token, {"path": file_a, "max_downloads": 0})
        if not check("max_downloads=0 → 422/400", code in (400, 422), f"HTTP {code}"):
            failures += 1

        # ── 7. Revocation ─────────────────────────────────────────────────
        status, _ = http_get(pub_url)
        if not check("link still live before delete", status == 200, f"HTTP {status}"):
            failures += 1
        code, data = api("DELETE", f"/api/share-links/{link['id']}", token)
        if not check("delete link → ok", code == 200 and (data or {}).get("ok"), f"HTTP {code}"):
            failures += 1
        status, _ = http_get(pub_url)
        if not check("revoked URL → 404", status == 404, f"HTTP {status}"):
            failures += 1
        code, data = api("DELETE", "/api/share-links/nonexistent-id-1234567890", token)
        if not check("delete unknown id → 404", code == 404, f"HTTP {code}"):
            failures += 1

    finally:
        for lid in created_ids:
            api("DELETE", f"/api/share-links/{lid}", token)
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failures


# ── Part B: UI (Playwright) ──────────────────────────────────────────────────

def part_a2_multi(token) -> int:
    """Multi-file links (Photo Station multi-share): ONE link → gallery page,
    per-file download, thumbnails, and all-as-zip."""
    failures = 0
    print("\nPart A2 — API: multi-file links")

    from PIL import Image

    tmpdir = service_readable_tmpdir(prefix="ethos-shares-multi-")
    d1 = os.path.join(tmpdir, "trip-a")
    d2 = os.path.join(tmpdir, "trip-b")
    os.makedirs(d1); os.makedirs(d2)

    def make_png(path, rgb):
        Image.new("RGB", (80, 60), rgb).save(path)
        return path

    f1 = make_png(os.path.join(d1, "sunset.png"), (255, 120, 0))
    f2 = make_png(os.path.join(d1, "beach.png"), (0, 160, 255))
    # duplicate basename in a different folder → display-name collision
    f3 = make_png(os.path.join(d2, "sunset.png"), (0, 200, 80))

    created_ids = []
    try:
        code, link = api("POST", "/api/share-links", token,
                         {"paths": [f1, f2, f3], "name": "Trip photos",
                          "expires_in_days": 7})
        if not check("create multi link → 200", code == 200 and isinstance(link, dict), f"HTTP {code} {link}"):
            return failures + 1
        created_ids.append(link["id"])
        tok = link["token"]
        murl = f"{BASE}/api/share-links/public/{tok}"

        if not check("multi link stores files list (3, deduped)", len(link.get("files", [])) == 3 and link.get("path") == "", str(link.get("files"))):
            failures += 1
        if not check("custom label kept", link.get("name") == "Trip photos", str(link.get("name"))):
            failures += 1

        status, raw = http_get(murl)
        page = raw.decode(errors="replace")
        if not check("public multi GET (no JWT) → HTML gallery", status == 200 and "<html" in page.lower() and "ITEMS" in page, f"HTTP {status} {page[:150]!r}"):
            failures += 1
        for needle in ("sunset.png", "beach.png", "sunset (2).png", "/zip"):
            if not check(f"gallery page references {needle!r}", needle in page):
                failures += 1

        # per-file download by display name (incl. the collision-renamed one)
        status, body = http_get(murl + "/download?file=beach.png")
        with open(f2, "rb") as fh:
            if not check("per-file download beach.png", status == 200 and body == fh.read(), f"HTTP {status}"):
                failures += 1
        status, _ = http_get(murl + "/download?file=sunset%20(2).png")
        if not check("collision-renamed file downloadable", status == 200, f"HTTP {status}"):
            failures += 1
        status, _ = http_get(murl + "/download?file=nope.png")
        if not check("unknown file → 404", status == 404, f"HTTP {status}"):
            failures += 1

        # thumbnail: real PNG → WebP bytes (or SVG fallback — both acceptable)
        status, tbytes = http_get(murl + "/thumb?file=beach.png")
        # RIFF prefix covers WebP; SVG placeholder is an acceptable fallback
        ok_thumb = status == 200 and (tbytes[:4] in (b"RIFF", b"<svg") or tbytes.startswith(b"<?xml"))
        if not check("thumbnail → 200 webp/svg", ok_thumb, f"HTTP {status} head={tbytes[:8]!r}"):
            failures += 1

        # zip: valid archive with all three files and exact contents
        status, zbytes = http_get(murl + "/zip")
        if not check("download-all zip → 200", status == 200, f"HTTP {status}"):
            failures += 1
        else:
            import io as _io, zipfile as _zf
            try:
                zf = _zf.ZipFile(_io.BytesIO(zbytes))
                names = set(zf.namelist())
                ok_names = names == {"sunset.png", "beach.png", "sunset (2).png"}
                if not check("zip contains all 3 entries (collision renamed)", ok_names, str(names)):
                    failures += 1
                with open(f3, "rb") as fh:
                    if not check("zip entry bytes match source", zf.read("sunset (2).png") == fh.read()):
                        failures += 1
            except Exception as e:
                if not check("zip is a valid archive", False, str(e)):
                    failures += 1

        code, links = api("GET", "/api/share-links", token)
        mine = next((l for l in (links or {}).get("links", []) if l["id"] == link["id"]), None)
        # downloads so far: beach.png + sunset(2) + zip = 3
        if not check("download counter counts per-file + zip", mine and mine.get("downloads") == 3, str(mine)):
            failures += 1

        code, data = api("POST", "/api/share-links", token,
                         {"paths": [f1, os.path.join(tmpdir, "missing.png")]})
        if not check("multi with missing file → 400", code == 400, f"HTTP {code}"):
            failures += 1
        code, data = api("POST", "/api/share-links", token, {"paths": []})
        if not check("empty paths → 422/400", code in (400, 422), f"HTTP {code}"):
            failures += 1
        code, data = api("POST", "/api/share-links", token, {})
        if not check("neither path nor paths → 400", code == 400, f"HTTP {code}"):
            failures += 1

    finally:
        for lid in created_ids:
            api("DELETE", f"/api/share-links/{lid}", token)
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failures

def part_b(token) -> int:
    print("\nPart B — UI (Playwright)")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [SKIP] playwright not installed")
        return 0

    failures = 0
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

        # Seed one link so the table is deterministic.
        tmpdir = service_readable_tmpdir(prefix="ethos-shares-ui-")
        probe = os.path.join(tmpdir, "uiprobe.txt")
        with open(probe, "w") as f:
            f.write("ui share probe\n")
        code, seed = api("POST", "/api/share-links", token, {"path": probe})
        seeded_id = seed["id"] if isinstance(seed, dict) else None

        try:
            page.goto(BASE, wait_until="networkidle")
            overlay = page.locator("#loginOverlay")
            if overlay.count() and overlay.is_visible():
                page.fill('[data-testid="login-username"]', USER)
                page.fill('[data-testid="login-password"]', PASS)
                page.click('[data-testid="login-submit"]')
                page.wait_for_timeout(2500)

            # Open the Shared Links app directly.
            page.evaluate("openApp('shared-links')")
            row = page.locator(f'[data-testid="sl-row-{seeded_id}"]')
            try:
                row.wait_for(state="visible", timeout=10_000)
                check("seeded link row renders", True)
            except Exception as e:
                if not check("seeded link row renders", False, str(e)[:150]):
                    failures += 1

            chip = page.locator(f'[data-testid="sl-row-{seeded_id}"] .sl-chip')
            if not check("status chip shows Active", chip.count() > 0 and "Active" in (chip.inner_text() or ""), chip.inner_text() if chip.count() else "no chip"):
                failures += 1

            # Create a second link from the form.
            page.fill('[data-testid="sl-new-path"]', probe)
            page.click('[data-testid="sl-create-btn"]')
            try:
                page.wait_for_selector('[data-testid^="sl-row-"]:not([data-testid="sl-row-' + seeded_id + '"])', timeout=10_000)
                check("form create adds a new row", True)
            except Exception as e:
                if not check("form create adds a new row", False, str(e)[:150]):
                    failures += 1

            real_js_errors = [e for e in js_errors if "files/events" not in e]
            if not check("no page JS errors", not real_js_errors, "; ".join(real_js_errors)[:300]):
                failures += 1
        finally:
            # Clean up every link this test created.
            code, data = api("GET", "/api/share-links", token)
            for l in (data or {}).get("links", []):
                if l.get("path", "").startswith(tmpdir):
                    api("DELETE", f"/api/share-links/{l['id']}", token)
            shutil.rmtree(tmpdir, ignore_errors=True)
            browser.close()

    return failures


def main() -> int:
    print(f"Public Share Links E2E — {BASE}")
    token = login()

    total = part_a(token)
    total += part_a2_multi(token)
    total += part_b(token)

    print(f"\n{'=' * 50}\nRESULT: {'PASS' if total == 0 else f'{total} FAILURE(S)'}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
