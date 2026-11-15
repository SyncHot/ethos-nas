"""API + WebSocket test — Files mutation broadcasts & trash hardening.

Flow: PAM login (API) → subscribe to /ws/files/events → perform mutations via
the API and assert that every one emits a WS mutation event other windows can
act on: mkdir, touch, rename (with old+new names), hard delete, move-to-trash
(delete_soft), restore. Also verifies the trash-restore fallback (original
parent dir deleted → item restored into the user's home dir, flagged in the
response) and that empty-trash counts items, not .meta.json sidecars.

Unlike the Playwright e2e tests this one exercises the WebSocket layer
directly, so it catches regressions in broadcast_mutation wiring even when no
second browser window is open.

Self-cleaning: all files live in /tmp/ethos-broadcast-test* and the fallback
restore file is removed at the end. The destructive empty-trash step is
skipped unless ETHOS_TEST_ALLOW_TRASH_EMPTY=1, because it permanently deletes
whatever else is in the trash (user data on a production instance).

Run:  ETHOS_TEST_PASSWORD=… .venv/bin/python tests/test_file_station_broadcasts.py
      (server must be up on :9000, or set ETHOS_TEST_BASE=http://host:port)
"""
import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile

import httpx
import websockets

BASE = os.environ.get("ETHOS_TEST_BASE", "http://127.0.0.1:9000")
assert BASE.startswith(("http://", "https://")), f"BASE must be an http(s) URL, got {BASE!r}"
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = os.environ.get("ETHOS_TEST_PASSWORD", "")
ALLOW_TRASH_EMPTY = os.environ.get("ETHOS_TEST_ALLOW_TRASH_EMPTY") == "1"


def main() -> int:
    return asyncio.run(_run())


async def _run() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    ws_url_base = BASE.replace("https://", "wss://").replace("http://", "ws://")

    # Test-only scratch dir (self-cleaning, outside the app data dir).
    testdir = tempfile.mkdtemp(prefix="ethos-broadcast-test-")
    testfile2 = os.path.join(tempfile.gettempdir(), "ethos-broadcast-test2")

    # --- login ---
    async with httpx.AsyncClient(base_url=BASE, timeout=15) as c:
        r = await c.post("/api/auth/login", json={"username": USER, "password": PASS})
        if r.status_code != 200:
            print(f"  [FAIL] login — {r.status_code} {r.text}")
            return 1
        token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    events: list[dict] = []
    async with websockets.connect(f"{ws_url_base}/ws/files/events?token={token}", max_size=2**22) as ws:
        async def pump():
            async for raw in ws:
                events.append(json.loads(raw))

        pump_task = asyncio.create_task(pump())

        async def wait_event(action: str, path_fragment: str, timeout: float = 5.0) -> dict | None:
            """Wait for a mutation event with the given action whose path matches.

            Scans ALL events: the WS event can arrive before the HTTP response
            returns (the broadcast happens before the handler finishes).
            """
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                for ev in events:
                    if ev.get("action") == action and path_fragment in ev.get("path", ""):
                        return ev
                await asyncio.sleep(0.05)
            return None

        async with httpx.AsyncClient(base_url=BASE, headers=headers, timeout=15) as c:
            # --- mkdir ---
            r = await c.post("/api/files/mkdir", json={"path": f"{testdir}/subdir"})
            check("mkdir API ok", r.status_code == 200, r.text)
            ev = await wait_event("mkdir", testdir)
            check("mkdir WS event", ev is not None, f"events={events[-3:]}")
            if ev:
                check("mkdir event item name",
                      any(i.get("name") == "subdir" for i in ev.get("items", [])), json.dumps(ev))

            # --- touch ---
            r = await c.post("/api/files/touch", json={"path": f"{testdir}/newfile.txt"})
            check("touch API ok", r.status_code == 200, r.text)
            ev = await wait_event("touch", testdir)
            check("touch WS event", ev is not None, f"events={events[-3:]}")

            # --- rename ---
            r = await c.put("/api/files/rename",
                            json={"path": f"{testdir}/newfile.txt", "new_name": "renamed.txt"})
            check("rename API ok", r.status_code == 200, r.text)
            ev = await wait_event("rename", testdir)
            check("rename WS event", ev is not None, f"events={events[-3:]}")
            if ev:
                check("rename event has old+new",
                      any(i.get("old_name") == "newfile.txt" and i.get("name") == "renamed.txt"
                          for i in ev.get("items", [])), json.dumps(ev))

            # --- hard delete ---
            r = await c.request("DELETE", "/api/files", json={"path": f"{testdir}/renamed.txt"})
            check("delete API ok", r.status_code == 200, r.text)
            ev = await wait_event("delete_hard", testdir)
            check("delete_hard WS event", ev is not None, f"events={events[-3:]}")

            # --- move to trash (delete_soft) ---
            r = await c.post("/api/files/touch", json={"path": f"{testdir}/trashme.txt"})
            r = await c.post("/api/files/trash", json={"path": f"{testdir}/trashme.txt"})
            check("trash API ok", r.status_code == 200, r.text)
            ev = await wait_event("delete_soft", testdir)
            check("delete_soft WS event", ev is not None, f"events={events[-3:]}")

            # --- restore, original parent intact ---
            r = await c.post("/api/files/trash/restore", json={"name": "trashme.txt"})
            check("restore API ok", r.status_code == 200, r.text)
            d = r.json()
            check("restore no fallback", d.get("fallback_home") is False, json.dumps(d))
            check("restore back in place", os.path.exists(f"{testdir}/trashme.txt"))
            ev = await wait_event("restore", testdir)
            check("restore WS event", ev is not None, f"events={events[-3:]}")

            # --- restore fallback: trash again, delete parent, restore ---
            r = await c.post("/api/files/trash", json={"path": f"{testdir}/trashme.txt"})
            check("re-trash API ok", r.status_code == 200, r.text)
            shutil.rmtree(testdir)  # original parent gone
            r = await c.post("/api/files/trash/restore", json={"name": "trashme.txt"})
            check("fallback restore API ok", r.status_code == 200, r.text)
            d = r.json()
            check("fallback_home flag", d.get("fallback_home") is True, json.dumps(d))
            check("fallback dest exists", os.path.exists(d.get("restored_to", "")), json.dumps(d))
            fallback_file = d.get("restored_to", "")

            # --- empty trash: items counted, .meta.json sidecars not ---
            if ALLOW_TRASH_EMPTY:
                r = await c.post("/api/files/touch", json={"path": testfile2})
                r = await c.post("/api/files/trash", json={"path": testfile2})
                check("pre-empty trash ok", r.status_code == 200, r.text)
                r = await c.delete("/api/files/trash")
                check("empty trash API ok", r.status_code == 200, r.text)
                d = r.json()
                check("empty has failed_count field", "failed_count" in d, json.dumps(d))
                check("empty failed_count 0", d.get("failed_count") == 0, json.dumps(d))
            else:
                print("  [SKIP] empty-trash count (destructive — set ETHOS_TEST_ALLOW_TRASH_EMPTY=1)")

        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task

    # clean up the fallback-restore file from the user's home
    if fallback_file and os.path.exists(fallback_file):
        try:
            shutil.rmtree(fallback_file) if os.path.isdir(fallback_file) else os.remove(fallback_file)
        except OSError as exc:
            print(f"  [WARN] could not remove fallback file {fallback_file}: {exc}")

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
