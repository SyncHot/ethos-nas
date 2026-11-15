#!/usr/bin/env python3
"""Unit tests for backend/services/debrid.py (offline, no network).

Run:  .venv/bin/python tests/test_debrid_service.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backend.services.debrid as debrid  # noqa: E402

_failures = 0


def check(name, ok, detail=""):
    global _failures
    if not ok:
        _failures += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    print("debrid service")
    original = debrid._http_json

    try:
        # ── Dispatch: unknown providers are rejected, not silently ignored ───
        for fn, args, label in (
            (debrid.add_magnet, ("nope", "k", "magnet:?xt=urn:btih:" + "a" * 40), "add_magnet"),
            (debrid.torrent_status, ("nope", "k", 1), "torrent_status"),
            (debrid.select_files, ("nope", "k", 1, [1]), "select_files"),
            (debrid.delete_torrent, ("nope", "k", 1), "delete_torrent"),
        ):
            try:
                fn(*args)
            except RuntimeError:
                check(f"{label} rejects an unknown provider", True)
            else:
                check(f"{label} rejects an unknown provider", False)
        check("verify returns False for an unknown provider", debrid.verify("nope", "k") is False)
        try:
            debrid.download_link("alldebrid", "k", 1)
        except RuntimeError:
            check("download_link has no per-file API for AllDebrid", True)
        else:
            check("download_link has no per-file API for AllDebrid", False)

        # ── add_magnet dispatch shapes ───────────────────────────────────────
        debrid._http_json = lambda *a, **k: {"id": 5}
        check("Real-Debrid add_magnet returns the id", debrid.add_magnet("realdebrid", "k", "magnet:x") == 5)

        debrid._http_json = lambda *a, **k: {"status": "success", "data": {"magnets": [{"id": 6}]}}
        check("AllDebrid add_magnet returns the id", debrid.add_magnet("alldebrid", "k", "magnet:x") == 6)

        debrid._http_json = lambda *a, **k: {"success": True, "data": {"id": 7}}
        check("TorBox add_magnet returns the id", debrid.add_magnet("torbox", "k", "magnet:x") == 7)

        # ── verify dispatch ──────────────────────────────────────────────────
        debrid._http_json = lambda *a, **k: {"id": 1}
        check("Real-Debrid verify accepts a user object", debrid.verify("realdebrid", "k") is True)
        debrid._http_json = lambda *a, **k: {"status": "success", "data": {"user": {"username": "x"}}}
        check("AllDebrid verify accepts a user object", debrid.verify("alldebrid", "k") is True)
        debrid._http_json = lambda *a, **k: {"success": True}
        check("TorBox verify accepts success", debrid.verify("torbox", "k") is True)
        debrid._http_json = lambda *a, **k: {"id": 0}
        check("Real-Debrid verify rejects an empty response", debrid.verify("realdebrid", "k") is False)

        # A provider that answers 200-with-error must still read as a bad key.
        debrid._http_json = lambda *a, **k: {"status": "error",
                                            "error": {"code": "AUTH_BAD_APIKEY", "message": "The auth apikey is invalid"}}
        try:
            debrid.verify("alldebrid", "k")
        except debrid.ProviderHTTPError as exc:
            check("AllDebrid error body becomes a 401", exc.status == 401, str(exc))
            check("AllDebrid error message is kept", "auth apikey is invalid" in exc.detail, exc.detail)
        else:
            check("AllDebrid error body becomes a 401", False)

        seen = {}
        def _spy(url, *a, **k):
            seen["url"] = url
            return {"success": True}
        debrid._http_json = _spy
        check("TorBox verify uses the /user/me endpoint",
              debrid.verify("torbox", "k") is True and seen["url"].endswith("/v1/api/user/me"), seen.get("url", ""))

        # ── Provider errors are surfaced ─────────────────────────────────────
        debrid._http_json = lambda *a, **k: {"success": False, "detail": "no torrent"}
        try:
            debrid.add_magnet("torbox", "k", "magnet:x")
        except RuntimeError as exc:
            check("TorBox error detail reaches the caller", "no torrent" in str(exc), str(exc))
        else:
            check("TorBox error detail reaches the caller", False)
    finally:
        debrid._http_json = original

    # ── HTTP errors carry the provider's own message and status ──────────────
    err = debrid.ProviderHTTPError(401, debrid._provider_error_detail('{"error": "bad_token", "error_code": 8}'))
    check("RD bad_token detail is readable", err.detail == "bad_token (code 8)", err.detail)
    check("ProviderHTTPError keeps the status", err.status == 401)
    check("ProviderHTTPError is a RuntimeError", isinstance(err, RuntimeError))
    ad = debrid._provider_error_detail('{"status": "error", "error": {"code": "AUTH_BAD_APIKEY", "message": "Invalid API key"}}')
    check("AllDebrid nested error message is used", ad == "Invalid API key", ad)
    check("non-JSON error bodies fall back to the raw text",
          debrid._provider_error_detail("<html>403</html>") == "<html>403</html>")
    check("TorBox error + detail are combined",
          debrid._provider_error_detail('{"success": false, "error": "BAD_TOKEN", "detail": "Your token is invalid"}')
          == "BAD_TOKEN: Your token is invalid")
    check("empty bodies do not crash", debrid._provider_error_detail("") == "")

    print(f"\n{'FAIL' if _failures else 'OK'} — {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
