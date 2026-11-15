"""Playwright e2e — Music app: add a found track to a new playlist ("Rolki").

Verifies the hover-reveal fix for the track-row ⋮ menu (frontend/static/apps/music.js):
  1. ⋮ is hidden by default on result rows, visible on row hover;
  2. ⋮ → "Dodaj do playlisty…" dialog opens from search results;
  3. creating a new playlist "Rolki" and adding the track works end-to-end;
  4. the track shows up in the Biblioteka tab (card + detail view);
  5. server state (/api/music/state, shared with the Android app) contains it.

Run:  ETHOS_TEST_PASSWORD=… .venv/bin/python tests/test_music_add_to_playlist.py
      (server must be up on :9000; the PAM credential is passed via env, not stored here)
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = "http://localhost:9000"
assert BASE.startswith(("http://", "https://")), f"BASE must be an http(s) URL, got {BASE!r}"
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = os.environ.get("ETHOS_TEST_PASSWORD", "")
# Unique per run — the server allows duplicate playlist names, so a fixed name
# accumulated orphan playlists and made later runs open the wrong card.
PLAYLIST_NAME = f"Rolki-{int(time.time())}"
SEARCH_QUERY = "never gonna give you up rick astley"


def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        js_errors: list[str] = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))

        def api(path: str, token: str):
            r = page.request.get(BASE + path, headers={"Authorization": f"Bearer {token}"})
            try:
                return r.status, r.json()
            except Exception:
                return r.status, {}

        def api_del(path: str, token: str):
            r = page.request.delete(BASE + path, headers={"Authorization": f"Bearer {token}"})
            try:
                return r.status, r.json()
            except Exception:
                return r.status, {}

        if not PASS:
            print("ETHOS_TEST_PASSWORD env var not set — cannot log in")
            browser.close()
            return 1

        print("Step 1: PAM login + open Music app")
        page.goto(BASE, wait_until="networkidle")
        page.fill('[data-testid="login-username"]', USER)
        page.fill('[data-testid="login-password"]', PASS)
        page.click('[data-testid="login-submit"]')
        page.wait_for_selector("#loginOverlay", state="hidden", timeout=20_000)
        check("JWT stored after login", bool(page.evaluate("localStorage.getItem('token') || ''")))

        page.evaluate("window.openApp('music')")
        page.wait_for_selector(".em-root", timeout=15_000)
        check("Music app rendered", True)

        print("\nStep 2: search for a track (Szukaj tab)")
        page.locator("nav.em-nav button", has_text="Szukaj").click()
        field = page.locator(".em-searchfield")
        field.wait_for(timeout=10_000)
        field.fill(SEARCH_QUERY)
        rows = page.locator(".em-content .em-row")
        try:
            rows.first.wait_for(state="visible", timeout=90_000)
            check("search returned results", True)
        except Exception as e:
            check("search returned results", False, str(e))
            browser.close()
            return 1

        song_title = page.locator(".em-content .em-row-title").first.inner_text().strip()
        print(f"      first result: {song_title!r}")

        print("\nStep 3: ⋮ button visibility (the fix)")
        more_btn = rows.first.locator("button[title='Opcje']")
        page.mouse.move(10, 450)  # neutral spot — not over any row
        hidden_default = page.evaluate(
            """() => getComputedStyle(document.querySelector('.em-content .em-row button[title="Opcje"]')).visibility""")
        check("⋮ hidden by default", hidden_default == "hidden", f"got {hidden_default!r}")

        rows.first.hover()
        visible_hover = page.evaluate(
            """() => getComputedStyle(document.querySelector('.em-content .em-row button[title="Opcje"]')).visibility""")
        check("⋮ visible on row hover", visible_hover == "visible", f"got {visible_hover!r}")

        print("\nStep 4: open menu → Dodaj do playlisty…")
        more_btn.click()
        menu = page.locator(".em-menu")
        menu.wait_for(state="visible", timeout=5_000)
        check("menu opened with 'Dodaj do playlisty…'",
              menu.locator(".em-menuitem", has_text="Dodaj do playlisty").count() == 1)

        page.locator(".em-menu .em-menuitem", has_text="Dodaj do playlisty").click()
        dlg = page.locator(".em-dialog")
        dlg.wait_for(state="visible", timeout=5_000)
        check("add-to-playlist dialog opened", True)

        print(f"\nStep 5: create new playlist {PLAYLIST_NAME!r} and add the track")
        dlg.locator("input[type=text]").fill(PLAYLIST_NAME)
        page.click(".em-dialog .em-btn:not(.ghost)")  # "Utwórz i dodaj"
        try:
            dlg.wait_for(state="detached", timeout=10_000)
            check("dialog closed after create+add", True)
        except Exception as e:
            check("dialog closed after create+add", False, str(e))

        token = page.evaluate("localStorage.getItem('token') || ''")
        try:
            print("\nStep 6: Biblioteka tab shows the new playlist with the track")
            page.locator("nav.em-nav button", has_text="Biblioteka").click()
            card = page.locator(".em-card", has=page.locator(".em-card-label", has_text=PLAYLIST_NAME))
            try:
                card.first.wait_for(state="visible", timeout=10_000)
                check(f"card {PLAYLIST_NAME!r} in library grid", True)
            except Exception as e:
                check(f"card {PLAYLIST_NAME!r} in library grid", False, str(e))

            card.first.click()
            detail_title = page.locator(".em-detail-title")
            try:
                detail_title.wait_for(state="visible", timeout=10_000)
                check("playlist detail opened with correct title",
                      PLAYLIST_NAME in (detail_title.inner_text() or ""), detail_title.inner_text())
            except Exception as e:
                check("playlist detail opened with correct title", False, str(e))

            n_rows = page.locator(".em-content .em-row").count()
            row_titles = [t.strip() for t in page.locator(".em-content .em-row-title").all_inner_texts()]
            check("detail lists exactly 1 track (the added song)",
                  n_rows == 1 and song_title in row_titles, f"rows={n_rows} titles={row_titles}")

            print("\nStep 7: server state shared with the Android app")
            s, st = api("/api/music/state", token)
            pls = [pl for pl in (st.get("playlists") or []) if pl.get("name") == PLAYLIST_NAME]
            check(f"GET /api/music/state has playlist {PLAYLIST_NAME!r}", s == 200 and len(pls) == 1, f"s={s}")
            tracks = pls[0].get("tracks", []) if pls else []
            check("server-side track matches the added song",
                  any(t.get("title") == song_title for t in tracks), str([t.get("title") for t in tracks]))

            print("\nStep 8: page JS errors")
            check("no uncaught page JS errors", not js_errors, "; ".join(js_errors[:3]))
        finally:
            # Cleanup: delete the test playlist so repeated runs don't accumulate
            # orphans in the shared music state (unique name → at most one match).
            s, st = api("/api/music/state", token)
            created = [pl for pl in (st.get("playlists") or []) if pl.get("name") == PLAYLIST_NAME]
            deleted = 0
            for pl in created:
                s2, body = api_del(f"/api/music/playlists/{pl['id']}", token)
                if s2 == 200 and body.get("deleted"):
                    deleted += 1
            print("\nStep 9: cleanup — test playlist removed")
            check("cleanup: no leftover test playlist",
                  deleted == len(created), f"created={len(created)} deleted={deleted}")

        browser.close()

    print(f"\n{'=' * 60}\nRESULT: {'ALL PASS' if not failures else str(len(failures)) + ' FAILURES'}"
          + ("\n" + "\n".join(" - " + f for f in failures) if failures else ""))
    return len(failures)


if __name__ == "__main__":
    sys.exit(main())
