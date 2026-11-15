"""End-to-end test — Code Editor (projects + git).

Part A (API, always runs): recent-projects store (add/dedupe/remove/400 on
non-dir) and the full git flow against a temp repo the test creates: status
(clean → modified/untracked), stage, unstage, commit (+ empty-commit 400),
log growth, path-escape rejection, non-repo detection, push/pull against a
local bare remote (which also sets upstream so ahead/behind populate).

Part B (UI via Playwright, auto-skips when chromium is unavailable): login →
open the editor app → welcome screen with seeded recent project renders →
click it → file tree lists files → click a file → CodeMirror loads its
content → git panel shows branch chip + modified file → Stage flips to
Unstage → commit via the panel leaves "Working tree clean" and logs the new
subject.

Run:  .venv/bin/python tests/test_editor.py   (server must be up)
      ETHOS_TEST_BASE=http://127.0.0.1:9001 to target a non-default port.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from fixture_dir import service_readable_tmpdir, service_writable_tmpdir

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
USER = os.environ.get("ETHOS_TEST_USER", "marcin")
PASS = os.environ.get("ETHOS_TEST_PASS") or os.environ.get("ETHOS_TEST_PASSWORD") or ""
if not PASS:
    raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def api(method, path, token=None, body=None) -> tuple[int, dict | None]:
    """Minimal JSON API client. Returns (status_code, dict_or_None).

    Non-JSON error bodies are wrapped as {"_raw": text} so the second element is
    always a mapping (or None) — callers may .get() on it unconditionally.
    """
    url = BASE + path
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http(s) URLs are allowed for ETHOS_TEST_BASE: {BASE!r}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # nosec B310 — scheme-guarded above (http/https only)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 — scheme-guarded above
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
            return e.code, parsed if isinstance(parsed, dict) else {"_raw": str(parsed)}
        except Exception:
            return e.code, {"_raw": raw}


def login():
    code, data = api("POST", "/api/auth/login", body={"username": USER, "password": PASS})
    if code != 200 or not isinstance(data, dict):
        raise RuntimeError(f"login failed: HTTP {code}")
    return data["token"]


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:200]}")
    return r.stdout


def make_repo(root):
    """Init a repo with one committed file; returns path of that file."""
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "ethos-test@example.com")
    git(root, "config", "user.name", "EthOS Test")
    f = os.path.join(root, "probe.txt")
    with open(f, "w") as fh:
        fh.write("ui editor probe\nline two\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial commit")
    return f


# ── Part A: API E2E ───────────────────────────────────────────────────────────

def part_a(token) -> int:
    failures = 0
    print("\nPart A — API")

    # The server (service user) must write here: it creates new project dirs
    # and pushes to the bare remote, so the fixture must be service-WRITABLE
    # (a 755 readable dir 403s on mkdir — T-0014 follow-up).
    tmpdir = service_writable_tmpdir(prefix="ethos-editor-")
    repo = os.path.join(tmpdir, "proj")
    os.makedirs(repo)
    probe_file = make_repo(repo)
    other_dir = os.path.join(tmpdir, "plain")   # non-repo directory
    os.makedirs(other_dir)
    new_proj = os.path.join(tmpdir, "fresh")    # assigned pre-try: referenced in finally
    git_proj = os.path.join(tmpdir, "freshgit")

    try:
        # ── 1. Recent projects store ─────────────────────────────────────
        code, d = api("POST", "/api/editor/projects/recent", token, {"path": repo})
        if not check("add recent → 200", code == 200 and isinstance(d, dict), f"HTTP {code} {d}"):
            return failures + 1
        recents = (d or {}).get("recents", [])
        if not check("recent contains repo path", any(r["path"] == repo for r in recents)):
            failures += 1

        code, d = api("POST", "/api/editor/projects/recent", token, {"path": repo})
        paths = [r["path"] for r in (d or {}).get("recents", [])]
        if not check("re-add dedupes to one entry", paths.count(repo) == 1 and paths[0] == repo, str(paths)):
            failures += 1

        code, d = api("POST", "/api/editor/projects/recent", token, {"path": other_dir})
        paths = [r["path"] for r in (d or {}).get("recents", [])]
        if not check("second dir added on top", code == 200 and paths[:2] == [other_dir, repo], str(paths)):
            failures += 1

        code, d = api("DELETE", f"/api/editor/projects/recent?path={urllib.parse.quote(other_dir)}", token)
        paths = [r["path"] for r in (d or {}).get("recents", [])]
        if not check("remove recent works", code == 200 and other_dir not in paths, str(paths)):
            failures += 1

        code, d = api("POST", "/api/editor/projects/recent", token, {"path": os.path.join(tmpdir, "nope")})
        if not check("non-directory rejected → 400", code == 400, f"HTTP {code}"):
            failures += 1

        # ── New project endpoint ────────────────────────────────────────
        code, d = api("POST", "/api/editor/projects/new", token, {"path": new_proj})
        if not check("new project → 200 + dir created",
                     code == 200 and (d or {}).get("ok") is True and os.path.isdir(new_proj), f"HTTP {code} {d}"):
            failures += 1
        _, rd = api("GET", "/api/editor/projects/recent", token)
        paths = [r["path"] for r in (rd or {}).get("recents", [])]
        if not check("new project added to recents on top", paths[:1] == [new_proj], str(paths)):
            failures += 1
        code, d = api("POST", "/api/editor/projects/new", token, {"path": git_proj, "git_init": True})
        if not check("new project with git init → is_repo true",
                     code == 200 and (d or {}).get("is_repo") is True
                     and os.path.isdir(os.path.join(git_proj, ".git")), f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/projects/new", token, {"path": new_proj})
        if not check("existing path → 409", code == 409, f"HTTP {code} {d}"):
            failures += 1

        # ── 2. Git status: clean repo ────────────────────────────────────
        code, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        if not check("status → 200 on repo", code == 200 and isinstance(st, dict), f"HTTP {code} {st}"):
            return failures + 1
        st = st or {}
        if not check("is_repo true, branch main", st.get("is_repo") is True and st.get("branch") == "main", str(st)):
            failures += 1
        if not check("clean tree → no files", st.get("files") == [], str(st.get("files"))):
            failures += 1

        # ── 3. Modify + untracked → status shows both ────────────────────
        with open(probe_file, "a") as fh:
            fh.write("modified line\n")
        newf = os.path.join(repo, "new.txt")
        with open(newf, "w") as fh:
            fh.write("brand new\n")

        code, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        by_path = {f["path"]: f["status"] for f in (st or {}).get("files", [])}
        if not check("modified tracked file shown as ' M'", by_path.get("probe.txt") == " M", str(by_path)):
            failures += 1
        if not check("untracked file shown as '??'", by_path.get("new.txt") == "??", str(by_path)):
            failures += 1

        # ── 4. Stage / unstage round-trip ────────────────────────────────
        code, d = api("POST", "/api/editor/git/stage", token, {"repo": repo, "paths": ["probe.txt"]})
        if not check("stage → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1
        _, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        by_path = {f["path"]: f["status"] for f in (st or {}).get("files", [])}
        if not check("after stage → 'M '", by_path.get("probe.txt") == "M ", str(by_path)):
            failures += 1

        code, d = api("POST", "/api/editor/git/unstage", token, {"repo": repo, "paths": ["probe.txt"]})
        _, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        by_path = {f["path"]: f["status"] for f in (st or {}).get("files", [])}
        if not check("after unstage → ' M' again", code == 200 and by_path.get("probe.txt") == " M", str(by_path)):
            failures += 1

        # ── 5. Commit staged changes; empty commit rejected ──────────────
        api("POST", "/api/editor/git/stage", token, {"repo": repo, "paths": ["probe.txt"]})
        code, d = api("POST", "/api/editor/git/commit", token, {"repo": repo, "message": "part-a commit"})
        if not check("commit → ok + hash", code == 200 and (d or {}).get("ok") is True and (d or {}).get("hash"), f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/git/commit", token, {"repo": repo, "message": "empty"})
        if not check("nothing staged → 400", code == 400, f"HTTP {code} {d}"):
            failures += 1

        code, d = api("GET", f"/api/editor/git/log?repo={urllib.parse.quote(repo)}&limit=5", token)
        subjects = [c["subject"] for c in (d or {}).get("commits", [])]
        if not check("log has new commit on top", code == 200 and subjects[:1] == ["part-a commit"], str(subjects)):
            failures += 1

        # ── 6. Safety: path escape + option injection rejected ───────────
        code, d = api("POST", "/api/editor/git/stage", token, {"repo": repo, "paths": ["../evil.txt"]})
        if not check("path escape → 400", code == 400, f"HTTP {code} {d}"):
            failures += 1
        code, d = api("POST", "/api/editor/git/stage", token, {"repo": repo, "paths": ["--all"]})
        if not check("option-like path → 400", code == 400, f"HTTP {code} {d}"):
            failures += 1

        # ── 7. Non-repo directory ────────────────────────────────────────
        code, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(other_dir)}", token)
        if not check("non-repo → is_repo false", code == 200 and (st or {}).get("is_repo") is False, f"HTTP {code} {st}"):
            failures += 1

        # ── 8. Push / pull against a local bare remote ───────────────────
        remote = os.path.join(tmpdir, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
        git(repo, "remote", "add", "origin", remote)
        code, d = api("POST", "/api/editor/git/push", token, {"repo": repo})
        if not check("push → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1

        _, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        if not check("upstream set → ahead/behind integers", (st or {}).get("ahead") == 0 and (st or {}).get("behind") == 0, str(st)):
            failures += 1

        code, d = api("POST", "/api/editor/git/pull", token, {"repo": repo})
        if not check("pull → ok (up to date)", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1

        # ── 9. Project content search ────────────────────────────────────
        search_file = os.path.join(repo, "searchme.py")
        with open(search_file, "w") as fh:
            fh.write("alpha\nneedle_token = 1\nomega\n")
        q = urllib.parse.quote
        code, d = api("GET", f"/api/editor/search?repo={q(repo)}&q=needle_token", token)
        hits = (d or {}).get("results", [])
        if not check("content search finds line",
                     code == 200 and any(h["path"] == "searchme.py" and h["line"] == 2 for h in hits),
                     f"HTTP {code} {d}"):
            failures += 1
        code, d = api("GET", f"/api/editor/search?repo={q(repo)}&q=NEEDLE_TOKEN&case_sensitive=true", token)
        if not check("case-sensitive search → no hits",
                     code == 200 and (d or {}).get("results") == [], f"HTTP {code} {d}"):
            failures += 1
        code, d = api("GET", f"/api/editor/search?repo={q(repo)}&q=%5B&regex=true", token)
        if not check("invalid regex → 400", code == 400, f"HTTP {code} {d}"):
            failures += 1

        # ── 10. Save: stale-mtime conflict detection ─────────────────────
        target = os.path.join(repo, "probe.txt")
        os.chmod(target, 0o666)
        code, info = api("GET", f"/api/files/stat?path={q(target)}", token)
        mtime = (info or {}).get("modified")
        code, d = api("PUT", "/api/files/content", token,
                      {"path": target, "content": "one\n", "encoding": "utf-8", "mtime": mtime})
        if not check("write with fresh mtime → 200", code == 200, f"HTTP {code} {d}"):
            failures += 1
        code, d = api("PUT", "/api/files/content", token,
                      {"path": target, "content": "two\n", "encoding": "utf-8", "mtime": mtime})
        if not check("write with stale mtime → 409", code == 409, f"HTTP {code} {d}"):
            failures += 1
        code, d = api("PUT", "/api/files/content", token,
                      {"path": target, "content": "three\n", "encoding": "utf-8"})
        if not check("write without mtime → 200", code == 200, f"HTTP {code} {d}"):
            failures += 1

    finally:
        # Clean up recents entries this test created.
        for p in (repo, other_dir, new_proj, git_proj):
            api("DELETE", f"/api/editor/projects/recent?path={urllib.parse.quote(p)}", token)
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failures


# ── Part B: UI E2E (Playwright) ──────────────────────────────────────────────

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

        # Seed a dirty repo as a recent project.
        tmpdir = service_readable_tmpdir(prefix="ethos-editor-ui-")
        repo = os.path.join(tmpdir, "uiproj")
        os.makedirs(repo)
        probe_file = make_repo(repo)
        with open(probe_file, "a") as fh:   # leave it modified → ' M' in status
            fh.write("dirty for ui test\n")

        try:
            api("POST", "/api/editor/projects/recent", token, {"path": repo})

            page.goto(BASE, wait_until="networkidle")
            overlay = page.locator("#loginOverlay")
            if overlay.count() and overlay.is_visible():
                page.fill('[data-testid="login-username"]', USER)
                page.fill('[data-testid="login-password"]', PASS)
                page.click('[data-testid="login-submit"]')
                page.wait_for_timeout(2500)

            # Open the editor app → welcome screen with the seeded recent.
            page.evaluate("openApp('editor')")
            try:
                page.wait_for_selector('[data-testid="editor-welcome"]', timeout=10_000)
                check("welcome screen renders", True)
            except Exception as e:
                if not check("welcome screen renders", False, str(e)[:150]):
                    failures += 1

            try:
                page.wait_for_selector('[data-testid="ed-clone"]', state="visible", timeout=5_000)
                check("welcome shows Clone Repository button", True)
            except Exception as e:
                if not check("welcome shows Clone Repository button", False, str(e)[:150]):
                    failures += 1

            row = page.locator(f'.ed-recent-row[data-path="{repo}"]')
            try:
                row.wait_for(state="visible", timeout=10_000)
                check("seeded recent project listed", True)
            except Exception as e:
                if not check("seeded recent project listed", False, str(e)[:150]):
                    failures += 1

            # Click the recent → project view.
            row.click()
            try:
                page.wait_for_selector('[data-testid="ed-project"]', timeout=10_000)
                check("project view opens", True)
            except Exception as e:
                if not check("project view opens", False, str(e)[:150]):
                    failures += 1

            # File tree lists probe.txt; click it → editor loads content.
            file_row = page.locator('.ed-tree-row[data-name="probe.txt"]')
            try:
                file_row.wait_for(state="visible", timeout=10_000)
                check("tree lists probe.txt", True)
            except Exception as e:
                if not check("tree lists probe.txt", False, str(e)[:150]):
                    failures += 1

            file_row.click()
            try:
                page.wait_for_selector('.ed-editor-pane .CodeMirror', state="visible", timeout=10_000)
                check("file opens in editor pane", True)
            except Exception as e:
                if not check("file opens in editor pane", False, str(e)[:150]):
                    failures += 1

            # ── Window title + status-bar language picker ─────────────────
            try:
                title = page.locator('.window-title-text').first.inner_text()
                check("window title names the project", 'proj' in title.lower(), title)
                page.click('[data-testid="ed-lang"]')
                page.wait_for_selector('.ed-tree-menu', timeout=5_000)
                check("language picker lists modes",
                      page.locator('.ed-tree-menu-item').count() > 10)
                page.locator('.ed-tree-menu-item', has_text='Python').first.click()
                page.wait_for_timeout(250)
                check("language picker updates status bar",
                      page.locator('[data-testid="ed-lang"]').inner_text() == 'Python')
                page.click('[data-testid="ed-lang"]')
                page.wait_for_selector('.ed-tree-menu', timeout=5_000)
                page.locator('.ed-tree-menu-item', has_text='Plain Text').first.click()
            except Exception as e:
                if not check("language picker / window title", False, str(e)[:150]):
                    failures += 1

            # Git panel: branch chip + the modified file row.
            # ── Menu bar (File / Edit / View / Terminal / Help) ──
            try:
                page.wait_for_selector('[data-testid="ed-menubar"]', state="visible", timeout=5_000)
                btns = page.locator('[data-testid="ed-menubar"] .ed-menu-btn').count()
                check("menubar renders 5 menus", btns == 5, str(btns))
            except Exception as e:
                if not check("menubar renders", False, str(e)[:150]):
                    failures += 1

            page.click('[data-testid="ed-menubar"] .ed-menu-btn[data-menu="file"]')
            try:
                page.wait_for_selector('.ed-menu', state="visible", timeout=5_000)
                menu_txt = page.locator('.ed-menu').inner_text()
                check("File menu lists Save + New Project…",
                      "Save" in menu_txt and "New Project…" in menu_txt, menu_txt[:150])
            except Exception as e:
                if not check("File menu opens", False, str(e)[:150]):
                    failures += 1
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)

            page.click('[data-testid="ed-menubar"] .ed-menu-btn[data-menu="edit"]')
            try:
                page.wait_for_selector('.ed-menu', state="visible", timeout=5_000)
                menu_txt = page.locator('.ed-menu').inner_text()
                check("Edit menu lists Undo + Cut + Paste",
                      "Undo" in menu_txt and "Cut" in menu_txt and "Paste" in menu_txt, menu_txt[:150])
            except Exception as e:
                if not check("Edit menu opens", False, str(e)[:150]):
                    failures += 1
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)

            # ── Sidebar resize grip ──
            try:
                page.wait_for_selector('[data-testid="ed-side-resize"]', state="visible", timeout=5_000)
                page.evaluate("document.querySelector('[data-testid=\"ed-sidebar\"]').style.width = '264px'")
                sb = page.locator('[data-testid="ed-sidebar"]')
                sb_box = sb.bounding_box()
                grip_box = page.locator('[data-testid="ed-side-resize"]').bounding_box()
                assert sb_box is not None and grip_box is not None, "bounding boxes unavailable"
                before = sb_box["width"]
                gx = grip_box["x"] + grip_box["width"] / 2
                gy = grip_box["y"] + grip_box["height"] / 2
                page.mouse.move(gx, gy)
                page.mouse.down()
                page.mouse.move(grip_box["x"] + 80, gy, steps=4)
                page.mouse.up()
                after_box = sb.bounding_box()
                after = after_box["width"] if after_box else 0
                check("sidebar drag widens sidebar", after > before + 30, f"{before:.0f}→{after:.0f}")
            except Exception as e:
                if not check("sidebar resize grip works", False, str(e)[:150]):
                    failures += 1

            chip = page.locator('[data-testid="ed-sb-branch"]')
            try:
                chip.wait_for(state="visible", timeout=10_000)
                check("branch chip shows main", "main" in (chip.inner_text() or ""), chip.inner_text())
            except Exception as e:
                if not check("branch chip visible", False, str(e)[:150]):
                    failures += 1

            # Reveal the Source Control side view (hidden behind the activity bar).
            page.click('[data-testid="ed-ab-scm"]')

            for sec, label in (("branches", "BRANCHES"), ("remotes", "REMOTES"), ("stash", "STASH")):
                head = page.locator(f'[data-testid="ed-scm-{sec}-head"]')
                try:
                    head.wait_for(state="visible", timeout=5_000)
                    check(f"SCM shows {label} section", label in (head.inner_text() or ""), head.inner_text())
                except Exception as e:
                    if not check(f"SCM shows {label} section", False, str(e)[:150]):
                        failures += 1

            page.locator('[data-testid="ed-scm-branches-head"]').click()
            try:
                page.wait_for_function(
                    "document.querySelector('[data-testid=\"ed-scm-branches\"]')?.textContent?.includes('main') === true",
                    timeout=10_000)
                check("expanding BRANCHES lists current branch main", True)
            except Exception as e:
                if not check("expanding BRANCHES lists current branch main", False, str(e)[:150]):
                    failures += 1

            chip = page.locator('[data-testid="ed-sb-branch"]')
            chip.click()
            try:
                page.wait_for_selector('.ed-tree-menu', state="visible", timeout=5_000)
                menu_txt = page.locator('.ed-tree-menu').inner_text()
                check("branch chip opens picker with main", "main" in menu_txt, menu_txt[:120])
            except Exception as e:
                if not check("branch chip opens picker", False, str(e)[:150]):
                    failures += 1
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)

            git_row = page.locator('.ed-scm-file[data-path="probe.txt"]')
            try:
                git_row.wait_for(state="visible", timeout=10_000)
                check("git panel lists modified probe.txt", True)
            except Exception as e:
                if not check("git panel lists modified probe.txt", False, str(e)[:150]):
                    failures += 1

            # Stage → button flips to Unstage (panel refreshed).
            git_row.hover()  # row actions are hover-gated (display:none → flex)
            git_row.locator('button[title="Stage"]').click()
            try:
                page.wait_for_function(
                    "document.querySelector('.ed-scm-file[data-path=\"probe.txt\"] button[title=\"Unstage\"]') !== null",
                    timeout=10_000)
                check("stage flips to Unstage", True)
            except Exception as e:
                if not check("stage flips to Unstage", False, str(e)[:150]):
                    failures += 1

            # Commit via the panel → clean tree + log entry.
            page.fill('[data-testid="ed-commit-msg"]', "ui test commit")
            page.click('[data-testid="ed-commit"]')
            try:
                page.wait_for_function(
                    "document.querySelector('[data-testid=\"ed-scm-changes\"] .ed-out-empty')?.textContent === 'No changes'",
                    timeout=10_000)
                check("commit → working tree clean", True)
            except Exception as e:
                if not check("commit → working tree clean", False, str(e)[:150]):
                    failures += 1

            # History box updates from a separate /git/log fetch that lands
            # after the changes box — wait for it instead of reading once.
            try:
                page.wait_for_function(
                    "document.querySelector('[data-testid=\"ed-scm-history\"]')?.textContent?.includes('ui test commit') === true",
                    timeout=10_000)
                check("log shows new commit subject", True)
            except Exception as e:
                if not check("log shows new commit subject", False, str(e)[:150]):
                    failures += 1

            # ── New file via tree toolbar → inline input → row + pane ────
            # The Explorer now uses a VS Code-style inline input (no native
            # prompt() dialog), so the click returns immediately.
            # New-file button lives in the Explorer side view; SCM is active now.
            page.click('[data-testid="ed-ab-explorer"]')
            try:
                page.click('[data-testid="ed-new-file"]', timeout=10_000)
                page.wait_for_selector('.ed-tree-input', timeout=10_000)
                check("inline new-file input appears", True)
                page.fill('.ed-tree-input', 'created-by-ui.txt')
                page.keyboard.press('Enter')
                page.wait_for_selector('.ed-tree-row[data-name="created-by-ui.txt"]', timeout=10_000)
                check("new file appears in tree", True)
            except Exception as e:
                if not check("inline new-file input appears", False, str(e)[:150]):
                    failures += 1
                if not check("new file appears in tree", False):
                    failures += 1
            try:
                page.wait_for_selector('.ed-editor-pane[data-path$="created-by-ui.txt"] .CodeMirror', state="visible", timeout=10_000)
                check("new file opens in editor pane", True)
            except Exception as e:
                if not check("new file opens in editor pane", False, str(e)[:150]):
                    failures += 1

            # ── Context menu on a tree row ────────────────────────────────
            page.locator('.ed-tree-row[data-name="created-by-ui.txt"]').click(button="right")
            try:
                page.wait_for_selector('.ed-tree-menu', timeout=5_000)
                check("context menu opens on right-click", True)
            except Exception as e:
                if not check("context menu opens on right-click", False, str(e)[:150]):
                    failures += 1
            menu_txt = page.locator('.ed-tree-menu').inner_text()
            if not check("context menu has Rename + Delete", "Rename" in menu_txt and "Delete" in menu_txt, menu_txt[:200]):
                failures += 1
            page.keyboard.press("Escape")

            # ── Rename from the context menu (inline input) ───────────────
            try:
                page.locator('.ed-tree-row[data-name="created-by-ui.txt"]').click(button="right")
                page.wait_for_selector('.ed-tree-menu', timeout=5_000)
                page.locator('.ed-tree-menu-item', has_text="Rename").click()
                page.wait_for_selector('.ed-tree-input', timeout=5_000)
                page.fill('.ed-tree-input', 'renamed-by-ui.txt')
                page.keyboard.press('Enter')
                page.wait_for_selector('.ed-tree-row[data-name="renamed-by-ui.txt"]', timeout=10_000)
                check("rename action renames the file", True)
            except Exception as e:
                if not check("rename action renames the file", False, str(e)[:150]):
                    failures += 1

            # ── UX polish: badges, active row, find bar, tab menu ─────────
            try:
                check("file-type badges render", page.locator('.ed-file-badge').count() > 0)
                page.locator('.ed-tree-row[data-name="renamed-by-ui.txt"]').click()
                page.wait_for_timeout(400)
                check("active file highlighted in Explorer",
                      page.locator('.ed-tree-row.selected').count() == 1)
                page.click('.ed-menu-btn[data-menu="edit"]')
                page.wait_for_selector('.ed-menu', timeout=5_000)
                page.locator('.ed-menu-item', has_text='Find').first.click()
                page.wait_for_selector('.ed-findbar', state='visible', timeout=5_000)
                page.fill('.ed-findbar-query', 'probe')
                page.wait_for_timeout(400)
                count_txt = page.locator('.ed-findbar-count').inner_text()
                check("find bar shows match count", 'of' in count_txt or 'No results' in count_txt, count_txt)
                page.keyboard.press('Escape')
                check("Escape closes the find bar",
                      page.locator('.ed-findbar').first.is_hidden())
                page.locator('.ed-tab.active').click(button='right')
                page.wait_for_selector('.ed-tree-menu', timeout=5_000)
                tab_menu = page.locator('.ed-tree-menu').inner_text()
                check("tab context menu has Close Others/Close All",
                      'Close Others' in tab_menu and 'Close All' in tab_menu, tab_menu[:200])
                page.keyboard.press('Escape')
            except Exception as e:
                if not check("UX polish checks", False, str(e)[:200]):
                    failures += 1

            # ── Outline view + command palette ───────────────────────────
            try:
                with open(os.path.join(repo, "symbols.py"), "w") as fh:
                    fh.write("def alpha():\n    pass\n\nclass Beta:\n    pass\n")
                # Explorer is already the active view; clicking its icon again
                # would only collapse the sidebar.
                if page.locator('[data-testid="ed-sidebar"].collapsed').count():
                    page.click('[data-testid="ed-ab-explorer"]')
                page.wait_for_timeout(300)
                page.evaluate("document.querySelector('[data-testid=\"ed-tree-refresh\"]').click()")
                page.wait_for_selector('.ed-tree-row[data-name="symbols.py"]', timeout=10_000)
                page.locator('.ed-tree-row[data-name="symbols.py"]').click()
                page.wait_for_timeout(700)
                page.click('[data-testid="ed-ab-outline"]')
                page.wait_for_selector('.ed-outline-row', timeout=5_000)
                check("outline lists symbols", page.locator('.ed-outline-row').count() >= 2)
                page.evaluate(
                    "document.body.dispatchEvent(new KeyboardEvent('keydown',"
                    " {key:'P', ctrlKey:true, shiftKey:true, bubbles:true, cancelable:true}))")
                page.wait_for_selector('[data-testid="ed-command-input"]', timeout=5_000)
                check("command palette opens", page.locator('.ed-quickopen-row').count() > 10)
                page.keyboard.press('Escape')
            except Exception as e:
                if not check("outline / command palette", False, str(e)[:200]):
                    failures += 1

            real_js_errors = [e for e in js_errors if "files/events" not in e]
            if not check("no page JS errors", not real_js_errors, "; ".join(real_js_errors)[:300]):
                failures += 1
        finally:
            api("DELETE", f"/api/editor/projects/recent?path={urllib.parse.quote(repo)}", token)
            shutil.rmtree(tmpdir, ignore_errors=True)
            browser.close()

    return failures


def commit_file(repo, relpath, content, msg):
    """Write a file in the repo and commit it on the current branch."""
    p = os.path.join(repo, relpath)
    os.makedirs(os.path.dirname(p), exist_ok=True) if "/" in relpath else None
    with open(p, "w") as fh:
        fh.write(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)

def part_c(token) -> int:
    """API tests for the pro git endpoints: branches, remotes, fetch,
    upstream, stash, discard, merge, clone, remote-tracking checkout."""
    failures = 0
    print("\nPart C — API (pro git)")

    tmpdir = service_writable_tmpdir(prefix="ethos-editor-pro-")
    repo = os.path.join(tmpdir, "pro")
    os.makedirs(repo)
    make_repo(repo)  # main + committed probe.txt

    bare = os.path.join(tmpdir, "bare.git")
    clone_dest = os.path.join(tmpdir, "cloned")

    try:
        # ── Branches ──────────────────────────────────────────────
        code, d = api("GET", f"/api/editor/git/branches?repo={urllib.parse.quote(repo)}", token)
        if not check("branches → 200, current main", code == 200 and (d or {}).get("current") == "main"
                     and any(b["name"] == "main" for b in (d or {}).get("branches", [])), f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/git/branch", token, {"repo": repo, "name": "feature"})
        if not check("create branch feature → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1
        _, d = api("GET", f"/api/editor/git/branches?repo={urllib.parse.quote(repo)}", token)
        names = [b["name"] for b in (d or {}).get("branches", [])]
        if not check("feature listed, still on main", "feature" in names and (d or {}).get("current") == "main", str(names)):
            failures += 1

        code, d = api("POST", "/api/editor/git/branch", token, {"repo": repo, "name": "hotfix", "check_out": True})
        if not check("create+checkout hotfix → current hotfix", code == 200 and (d or {}).get("branch") == "hotfix", f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/git/branch/rename", token, {"repo": repo, "name": "hotfix", "new_name": "hotfix2"})
        if not check("rename hotfix → hotfix2", code == 200 and (d or {}).get("branch") == "hotfix2", f"HTTP {code} {d}"):
            failures += 1

        # back to main before deleting anything
        code, d = api("POST", "/api/editor/git/checkout", token, {"repo": repo, "ref": "main"})
        if not check("checkout back to main", code == 200 and (d or {}).get("branch") == "main", f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/git/branch/delete", token, {"repo": repo, "name": "hotfix2"})
        if not check("delete hotfix2 → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/git/branch/delete", token, {"repo": repo, "name": "main"})
        if not check("delete current branch → 400", code == 400, f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/git/branch", token, {"repo": repo, "name": "bad..name"})
        if not check("invalid branch name → 400", code == 400, f"HTTP {code} {d}"):
            failures += 1

        # ── Remotes ───────────────────────────────────────────────
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        git(repo, "remote", "add", "origin", bare)
        code, d = api("GET", f"/api/editor/git/remotes?repo={urllib.parse.quote(repo)}", token)
        remotes = {r["name"]: r for r in (d or {}).get("remotes", [])}
        if not check("remotes lists origin", code == 200 and "origin" in remotes, f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/git/remote/add", token, {"repo": repo, "name": "up", "url": bare})
        if not check("add remote up → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1
        code, d = api("GET", f"/api/editor/git/remotes?repo={urllib.parse.quote(repo)}", token)
        remotes = {r["name"]: r for r in (d or {}).get("remotes", [])}
        if not check("up listed with fetch_url", "up" in remotes and remotes["up"]["fetch_url"] == bare, str(remotes.get("up"))):
            failures += 1

        code, d = api("POST", "/api/editor/git/remote/set-url", token, {"repo": repo, "name": "up", "url": os.path.join(tmpdir, "up.git")})
        if not check("set-url up → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/git/remote/remove", token, {"repo": repo, "name": "up"})
        if not check("remove remote up → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/git/remote/add", token, {"repo": repo, "name": "bad..name", "url": bare})
        if not check("invalid remote name → 400", code == 400, f"HTTP {code} {d}"):
            failures += 1

        # ── Fetch + push ──────────────────────────────────────────
        code, d = api("POST", "/api/editor/git/fetch", token, {"repo": repo})
        if not check("fetch → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1
        code, d = api("POST", "/api/editor/git/push", token, {"repo": repo})
        if not check("push → ok (sets upstream)", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1

        # ── Upstream ──────────────────────────────────────────────
        code, d = api("POST", "/api/editor/git/upstream", token, {"repo": repo, "remote": "origin"})
        if not check("set upstream origin/main → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1
        _, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        if not check("status ahead/behind populated", (st or {}).get("ahead") == 0 and (st or {}).get("behind") == 0, str(st)):
            failures += 1

        # ── Stash ─────────────────────────────────────────────────
        with open(os.path.join(repo, "probe.txt"), "a") as fh:
            fh.write("stash me\n")
        untracked = os.path.join(repo, "loose.txt")
        with open(untracked, "w") as fh:
            fh.write("loose\n")

        code, d = api("POST", "/api/editor/git/stash", token, {"repo": repo, "message": "wip", "include_untracked": True})
        if not check("stash push (untracked) → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1
        _, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        if not check("tree clean after stash", (st or {}).get("files") == [], str((st or {}).get("files"))):
            failures += 1

        code, d = api("GET", f"/api/editor/git/stash?repo={urllib.parse.quote(repo)}", token)
        stashes = (d or {}).get("stashes", [])
        if not check("stash list has 1 'wip'", code == 200 and len(stashes) == 1 and "wip" in stashes[0]["message"]
                     and stashes[0]["index"] == 0, f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/git/stash/apply", token, {"repo": repo, "index": 0})
        if not check("stash apply → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1
        _, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        by_path = {f["path"]: f["status"] for f in (st or {}).get("files", [])}
        if not check("apply restores changes + untracked", by_path.get("probe.txt") == " M" and by_path.get("loose.txt") == "??", str(by_path)):
            failures += 1
        _, d = api("GET", f"/api/editor/git/stash?repo={urllib.parse.quote(repo)}", token)
        if not check("apply keeps the entry (1 left)", len((d or {}).get("stashes", [])) == 1, str(d)):
            failures += 1

        code, d = api("POST", "/api/editor/git/stash/drop", token, {"repo": repo, "index": 0})
        if not check("stash drop → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1
        _, d = api("GET", f"/api/editor/git/stash?repo={urllib.parse.quote(repo)}", token)
        if not check("stash empty after drop", (d or {}).get("stashes") == [], str(d)):
            failures += 1

        # second scenario: pop (restores AND removes in one step)
        with open(os.path.join(repo, "probe.txt"), "a") as fh:
            fh.write("pop me\n")
        api("POST", "/api/editor/git/stash", token, {"repo": repo, "message": "pop-wip"})
        code, d = api("POST", "/api/editor/git/stash/pop", token, {"repo": repo, "index": 0})
        if not check("stash pop → ok (restores + removes)", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1
        _, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        by_path = {f["path"]: f["status"] for f in (st or {}).get("files", [])}
        if not check("pop restored the change", by_path.get("probe.txt") == " M", str(by_path)):
            failures += 1
        _, d = api("GET", f"/api/editor/git/stash?repo={urllib.parse.quote(repo)}", token)
        if not check("stash empty after pop", (d or {}).get("stashes") == [], str(d)):
            failures += 1

        # cleanup for the next tests
        api("POST", "/api/editor/git/discard", token, {"repo": repo, "paths": ["probe.txt", "loose.txt"]})

        # ── Discard ───────────────────────────────────────────────
        with open(os.path.join(repo, "probe.txt"), "a") as fh:
            fh.write("discard me\n")
        stray = os.path.join(repo, "stray.txt")
        with open(stray, "w") as fh:
            fh.write("stray\n")
        code, d = api("POST", "/api/editor/git/discard", token, {"repo": repo, "paths": ["probe.txt", "stray.txt"]})
        if not check("discard tracked+untracked → ok(2)", code == 200 and (d or {}).get("discarded") == 2, f"HTTP {code} {d}"):
            failures += 1
        if not check("stray.txt removed from disk", not os.path.exists(stray), "still present"):
            failures += 1
        _, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        if not check("tree clean after discard", (st or {}).get("files") == [], str((st or {}).get("files"))):
            failures += 1

        # staged addition discarded
        staged = os.path.join(repo, "staged-add.txt")
        with open(staged, "w") as fh:
            fh.write("added\n")
        git(repo, "add", "staged-add.txt")
        code, d = api("POST", "/api/editor/git/discard", token, {"repo": repo, "paths": ["staged-add.txt"]})
        if not check("discard staged addition → ok(1)", code == 200 and (d or {}).get("discarded") == 1, f"HTTP {code} {d}"):
            failures += 1
        if not check("staged-add.txt removed", not os.path.exists(staged), "still present"):
            failures += 1

        # ── Merge (clean fast-forward) ────────────────────────────
        api("POST", "/api/editor/git/checkout", token, {"repo": repo, "ref": "main"})
        git(repo, "checkout", "-qb", "ff-branch")
        commit_file(repo, "ff.txt", "ff content\n", "ff commit")
        api("POST", "/api/editor/git/checkout", token, {"repo": repo, "ref": "main"})
        code, d = api("POST", "/api/editor/git/merge", token, {"repo": repo, "branch": "ff-branch"})
        if not check("merge ff-branch → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1
        if not check("ff.txt present after merge", os.path.exists(os.path.join(repo, "ff.txt")), "missing"):
            failures += 1

        # ── Merge (conflict → ok:false + merging, then abort) ─────
        commit_file(repo, "conflict.txt", "main version\n", "main edits conflict.txt")
        git(repo, "checkout", "-qb", "conflict-branch", "HEAD~1")
        commit_file(repo, "conflict.txt", "branch version\n", "branch edits conflict.txt")
        api("POST", "/api/editor/git/checkout", token, {"repo": repo, "ref": "main"})
        code, d = api("POST", "/api/editor/git/merge", token, {"repo": repo, "branch": "conflict-branch"})
        if not check("conflict merge → ok:false", code == 200 and (d or {}).get("ok") is False, f"HTTP {code} {d}"):
            failures += 1
        _, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        if not check("status.merging true during conflict", (st or {}).get("merging") is True, str(st)):
            failures += 1
        code, d = api("POST", "/api/editor/git/merge/abort", token, {"repo": repo})
        if not check("merge abort → ok", code == 200 and (d or {}).get("ok") is True, f"HTTP {code} {d}"):
            failures += 1
        _, st = api("GET", f"/api/editor/git/status?repo={urllib.parse.quote(repo)}", token)
        if not check("status.merging false after abort", (st or {}).get("merging") is False, str(st)):
            failures += 1

        # ── Checkout of remote-tracking ref ───────────────────────
        git(repo, "checkout", "-qb", "remote-only", "main")
        commit_file(repo, "ro.txt", "remote only\n", "remote-only commit")
        git(repo, "push", "-q", "origin", "remote-only")
        git(repo, "checkout", "-q", "main")
        code, d = api("POST", "/api/editor/git/checkout", token, {"repo": repo, "ref": "origin/remote-only"})
        if not check("checkout origin/remote-only → local remote-only", code == 200 and (d or {}).get("branch") == "remote-only", f"HTTP {code} {d}"):
            failures += 1

        # ── Clone ─────────────────────────────────────────────────
        code, d = api("POST", "/api/editor/git/clone", token, {"url": bare, "path": clone_dest})
        if not check("clone bare → ok + path", code == 200 and (d or {}).get("ok") is True and (d or {}).get("path") == clone_dest,
                     f"HTTP {code} {d}"):
            failures += 1
        if not check("clone target is a repo", os.path.isdir(os.path.join(clone_dest, ".git")), "no .git"):
            failures += 1
        _, rd = api("GET", "/api/editor/projects/recent", token)
        if not check("clone added to recents on top", [r["path"] for r in (rd or {}).get("recents", [])][:1] == [clone_dest], str((rd or {}).get("recents", [])[:1])):
            failures += 1

        code, d = api("POST", "/api/editor/git/clone", token, {"url": bare, "path": clone_dest})
        if not check("clone to existing path → 409", code == 409, f"HTTP {code} {d}"):
            failures += 1

        code, d = api("POST", "/api/editor/git/clone", token, {"url": bare, "path": os.path.join(tmpdir, "no-such-parent", "x")})
        if not check("clone into missing parent → 400", code == 400, f"HTTP {code} {d}"):
            failures += 1

        api("DELETE", f"/api/editor/projects/recent?path={urllib.parse.quote(clone_dest)}", token)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failures

def main() -> int:
    print(f"Code Editor E2E — {BASE}")
    token = login()

    total = part_a(token)
    total += part_c(token)
    total += part_b(token)

    print(f"\n{'=' * 50}\nRESULT: {'PASS' if total == 0 else f'{total} FAILURE(S)'}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
