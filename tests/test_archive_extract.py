"""Archive extraction — File Manager "Extract here" / "Extract to…".

Covers the v2 extraction contract (docs/archive-extract.md):

  * static guards  — the JS format list matches the backend registry, the menu
    offers both entries, `extract` is excluded from the duplicate WS toast, and
    extraction never builds a shell string or logs a password.
  * live API       — folder mode, collision suffixes, chosen destination,
    folder name, "extract here" with skip/overwrite, tar.gz, bare .gz,
    zip-slip rejection, encrypted ZIP, split-volume resolution, and the
    validation paths (404/400/422).

Self-cleaning: everything lives in one service-writable /tmp fixture that is
removed at the end.

Run:  .venv/bin/python tests/test_archive_extract.py   (server must be up)
      ETHOS_TEST_BASE=http://127.0.0.1:9002 to target an isolated instance.
      ETHOS_TEST_API_KEY=<key> logs in without PAM (seeded users.json).
"""
import io
import json
import os
import re
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile

from fixture_dir import service_writable_tmpdir

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = os.environ.get("ETHOS_TEST_PASSWORD", "")
API_KEY = os.environ.get("ETHOS_TEST_API_KEY", "")
if not PASS and not API_KEY:
    raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")


def _login_body() -> dict:
    return {"api_key": API_KEY} if API_KEY else {"username": USER, "password": PASS}


def api(method: str, path: str, token: str | None = None, body: dict | None = None):
    """JSON request helper → (status, parsed body or None)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None
        except ValueError:
            return e.code, None


def make_zip(path: str, members: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> str:
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, payload in members.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o644 << 16
            zf.writestr(info, payload)
    return path


def make_tar_gz(path: str, members: dict[str, bytes]) -> str:
    with tarfile.open(path, "w:gz") as tf:
        for name, payload in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    return path


def static_checks(failures: list[str]) -> None:
    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    print("Static guards")
    sys.path.insert(0, ROOT)
    from backend.services import files_service  # noqa: E402

    js = open(os.path.join(ROOT, "frontend/static/apps/files.js"), encoding="utf-8").read()
    py = open(os.path.join(ROOT, "backend/services/files_service.py"), encoding="utf-8").read()
    router = open(os.path.join(ROOT, "backend/routers/files.py"), encoding="utf-8").read()

    js_exts_block = re.search(r"const _ARCHIVE_EXTS = new Set\(\[(.*?)\]\);", js, re.S)
    check("frontend archive list found", js_exts_block is not None)
    js_exts = set(re.findall(r"'(\.\w+)'", js_exts_block.group(1))) if js_exts_block else set()
    check("frontend list matches the backend registry",
          js_exts == set(files_service.ARCHIVE_EXTS),
          f"js-only={sorted(js_exts - files_service.ARCHIVE_EXTS)} "
          f"py-only={sorted(files_service.ARCHIVE_EXTS - js_exts)}")

    js_multi_block = re.search(r"const _TAR_MULTI_SUFFIXES = \[(.*?)\];", js, re.S)
    js_multi = set(re.findall(r"'(\.tar\.\w+)'", js_multi_block.group(1))) if js_multi_block else set()
    check("multi-suffix tar list matches",
          js_multi == set(files_service._TAR_MULTI_SUFFIXES),
          f"js-only={sorted(js_multi - set(files_service._TAR_MULTI_SUFFIXES))}")

    check("split-volume patterns are shared", "const _SPLIT_PATTERNS" in js)
    for probe in ("show.7z.001", "Kurs.part01.rar", "Kurs.r00"):
        info = files_service.split_volume_info(probe)
        check(f"backend resolves split volume {probe}", info is not None, str(info))

    check("context menu offers Extract here", "fm-ctx-extract'" in js)
    check("context menu offers Extract to…", "fm-ctx-extract-to" in js)
    check("destination dialog has a folder picker", "pickFolder(" in js and "fm-extract-browse" in js)
    check("extract is excluded from the duplicate WS toast",
          "'delete_soft', 'delete_hard', 'restore', 'extract'" in js)

    check("no shell=True in the extraction service", "shell=True" not in py)
    check("no shell=True in the files router", "shell=True" not in router)
    check("extraction runs off the event loop", "asyncio.to_thread" in router)
    check("destination goes through the path scope", "_sanitize(body.dest_dir)" in router)
    check("missing extractor maps to 409", 'status_code=409' in router
          and "missing_requirement" in router)
    logged_secret = [line for line in (py + router).splitlines()
                     if "logger." in line and "password" in line.lower()]
    check("passwords are never logged", not logged_secret, str(logged_secret[:2]))


def main() -> int:
    failures: list[str] = []
    static_checks(failures)

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    status, login = api("POST", "/api/auth/login", body=_login_body())
    if status != 200 or not login:
        print(f"  [FAIL] login — HTTP {status}")
        return 1
    token = login.get("token") or login.get("access_token")
    check("login", bool(token))
    if not token:
        return 1

    fixture = service_writable_tmpdir("ethos-extract-")
    try:
        work = os.path.join(fixture, "src")
        os.makedirs(work, exist_ok=True)
        os.chmod(work, 0o777)

        print("Folder mode (default)")
        nested = make_zip(os.path.join(work, "simple.zip"),
                          {"a/one.txt": b"1", "a/b/two.txt": b"2"})
        status, body = api("POST", "/api/files/extract", token, {"path": nested})
        check("zip → 200", status == 200, f"HTTP {status} {body}")
        dest = (body or {}).get("dest_dir") or ""
        check("extracts into a folder named after the archive",
              dest == os.path.join(work, "simple"), dest)
        check("nested files landed", os.path.isfile(os.path.join(dest, "a/b/two.txt")))
        check("count reported", (body or {}).get("extracted_count") == 2, str(body))
        check("tool reported", (body or {}).get("tool") == "stdlib", str(body))

        status, body = api("POST", "/api/files/extract", token, {"path": nested})
        check("re-extract gets a numeric suffix",
              status == 200 and os.path.basename((body or {}).get("dest_dir", "")) == "simple (1)",
              str(body))

        print("Chosen destination + folder name")
        target = os.path.join(fixture, "out")
        os.makedirs(target, exist_ok=True)
        os.chmod(target, 0o777)
        status, body = api("POST", "/api/files/extract", token,
                           {"path": nested, "dest_dir": target, "subfolder": "custom"})
        check("custom folder created", status == 200
              and (body or {}).get("dest_dir") == os.path.join(target, "custom"), str(body))
        check("custom folder has content",
              os.path.isfile(os.path.join(target, "custom", "a", "one.txt")))

        print("Extract here (subfolder='')")
        here = os.path.join(target, "here")
        os.makedirs(here, exist_ok=True)
        os.chmod(here, 0o777)
        conflict = os.path.join(here, "one.txt")
        with open(conflict, "w") as fh:
            fh.write("original")
        os.chmod(conflict, 0o666)
        flat = make_zip(os.path.join(work, "flat.zip"),
                        {"one.txt": b"replacement", "two.txt": b"new"})
        status, body = api("POST", "/api/files/extract", token,
                           {"path": flat, "dest_dir": here, "subfolder": ""})
        check("existing file kept by default", status == 200
              and (body or {}).get("skipped_existing") == 1, str(body))
        check("existing content untouched", open(conflict).read() == "original")
        check("new file written", os.path.isfile(os.path.join(here, "two.txt")))

        status, body = api("POST", "/api/files/extract", token,
                           {"path": flat, "dest_dir": here, "subfolder": "", "overwrite": True})
        check("overwrite replaces the file", status == 200, str(body))
        check("overwritten content", open(conflict).read() == "replacement")

        print("tar.gz and bare .gz")
        tgz = make_tar_gz(os.path.join(work, "bundle.tar.gz"), {"dir/x.txt": b"hello"})
        status, body = api("POST", "/api/files/extract", token, {"path": tgz})
        check("tar.gz → 200", status == 200, str(body))
        check("tar.gz into stem folder",
              os.path.basename((body or {}).get("dest_dir", "")) == "bundle"
              and os.path.isfile(os.path.join((body or {}).get("dest_dir", ""), "dir", "x.txt")))
        gz = os.path.join(work, "note.txt.gz")
        with __import__("gzip").open(gz, "wb") as fh:
            fh.write(b"plain")
        status, body = api("POST", "/api/files/extract", token, {"path": gz})
        check("bare .gz becomes a file", status == 200
              and os.path.isfile(os.path.join(work, "note.txt")), str(body))

        print("Safety")
        slip = make_zip(os.path.join(work, "slip.zip"), {"../pwned.txt": b"x"})
        status, body = api("POST", "/api/files/extract", token, {"path": slip})
        check("zip-slip → 400", status == 400, f"HTTP {status} {body}")
        check("nothing escaped", not os.path.exists(os.path.join(fixture, "pwned.txt")))

        print("Validation")
        status, _ = api("POST", "/api/files/extract", token, {"path": os.path.join(work, "nope.zip")})
        check("missing file → 404", status == 404, f"HTTP {status}")
        txt = os.path.join(work, "notes.txt")
        with open(txt, "w") as fh:
            fh.write("x")
        os.chmod(txt, 0o666)
        status, _ = api("POST", "/api/files/extract", token, {"path": txt})
        check("non-archive → 400", status == 400, f"HTTP {status}")
        status, _ = api("POST", "/api/files/extract", token,
                        {"path": nested, "subfolder": "a/b"})
        check("subfolder with slash → 400", status == 400, f"HTTP {status}")
        status, _ = api("POST", "/api/files/extract", token,
                        {"path": nested, "dest_dir": os.path.join(fixture, "missing")})
        check("unknown destination → 400", status == 400, f"HTTP {status}")
        status, _ = api("POST", "/api/files/extract", token, {"path": nested, "bogus": 1})
        check("unknown body field → 422", status == 422, f"HTTP {status}")

        print("Encrypted ZIP")
        enc = os.path.join(work, "secret.zip")
        made = False
        if shutil.which("zip"):
            made = os.system(f"cd {work} && zip -q -P hunter2 {enc} flat.zip 2>/dev/null") == 0
        if made:
            status, body = api("POST", "/api/files/extract", token, {"path": enc})
            check("encrypted without password → 400", status == 400, f"HTTP {status} {body}")
            check("error asks for the password",
                  "password" in str((body or {}).get("detail", "")).lower(), str(body))
            status, body = api("POST", "/api/files/extract", token,
                               {"path": enc, "subfolder": "decoded", "password": "hunter2"})
            check("encrypted with password → 200", status == 200, str(body))
            check("decrypted content present",
                  os.path.isfile(os.path.join(work, "decoded", "flat.zip")))
            status, body = api("POST", "/api/files/extract", token,
                               {"path": enc, "subfolder": "decoded2", "password": "hunter2"})
            check("password never echoed in errors", "hunter2" not in json.dumps(body or {}),
                  str(body))
        else:
            print("  [SKIP] encrypted ZIP (no `zip` CLI to build the fixture)")

        print("Split volumes")
        for name in ("Course.part01.rar", "Course.part02.rar"):
            path = os.path.join(work, name)
            with open(path, "wb") as fh:
                fh.write(b"not a real rar")
            os.chmod(path, 0o666)
        status, body = api("POST", "/api/files/extract", token,
                           {"path": os.path.join(work, "Course.part02.rar")})
        detail = str((body or {}).get("detail", ""))
        check("a later volume is accepted (first volume resolved)",
              status in (400, 409) and "Not a supported archive type" not in detail,
              f"HTTP {status} {body}")
        check("error names the first volume", "part01" in detail.lower() or "Course" in detail,
              detail[:120])
    finally:
        shutil.rmtree(fixture, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
