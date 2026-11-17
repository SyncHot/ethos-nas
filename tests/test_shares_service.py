"""Unit test — Samba share guest-write auto-fix (shares_service._apply_guest_write_fix).

Public writable shares must automatically get `force user`/`force group` set to the
directory owner, so guest sessions can write without manual smb.conf edits.

Run:  .venv/bin/python tests/test_shares_service.py   (no server needed)
"""
import configparser
import grp
import os
import pwd
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["ETHOS_DATA_DIR"] = tempfile.mkdtemp(prefix="ethos-shares-service-")
os.environ["ETHOS_JWT_SECRET"] = "shares-service-test-secret"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _cfg(fields: dict) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp["global"] = {"workgroup": "WORKGROUP"}
    cp["TestShare"] = fields
    return cp


def main():
    from backend.services import shares_service as ss

    me = pwd.getpwuid(os.getuid())

    with tempfile.TemporaryDirectory() as td:
        target = os.path.join(td, "data")
        os.mkdir(target)

        # 1. Guest-writable share → force user/group set to the directory owner
        cfg = _cfg({"path": target, "guest ok": "yes", "read only": "no"})
        ss._apply_guest_write_fix(cfg, "TestShare")
        assert cfg["TestShare"]["force user"] == me.pw_name, dict(cfg["TestShare"])
        assert "force group" in cfg["TestShare"], dict(cfg["TestShare"])

        # 2. Read-only share → untouched
        cfg = _cfg({"path": target, "guest ok": "yes", "read only": "yes"})
        ss._apply_guest_write_fix(cfg, "TestShare")
        assert "force user" not in cfg["TestShare"]

        # 3. Non-guest share → untouched
        cfg = _cfg({"path": target, "guest ok": "no", "read only": "no"})
        ss._apply_guest_write_fix(cfg, "TestShare")
        assert "force user" not in cfg["TestShare"]

    # 4. Missing path → non-fatal, no force user added
    cfg = _cfg({"path": "/nonexistent/xyz123", "guest ok": "yes", "read only": "no"})
    ss._apply_guest_write_fix(cfg, "TestShare")
    assert "force user" not in cfg["TestShare"]

    # 5. _owner_names resolves the real owner of a path
    with tempfile.TemporaryDirectory() as td:
        st = os.stat(td)
        expected = (pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name)
        assert ss._owner_names(td) == expected

    # 6. Failed reload restores the previous config before returning an error
    original_conf = ss.SMB_CONF
    original_write = ss._write_config
    original_reload = ss._reload_samba
    try:
        with tempfile.TemporaryDirectory() as td:
            ss.SMB_CONF = os.path.join(td, "smb.conf")
            with open(ss.SMB_CONF, "w", encoding="utf-8") as f:
                f.write("previous config\n")
            ss._write_config = lambda _config: ss._write_smb_content("candidate config\n")
            reload_calls = []

            def fail_once():
                reload_calls.append(True)
                if len(reload_calls) == 1:
                    raise RuntimeError("reload failed")

            ss._reload_samba = fail_once
            try:
                ss._apply_config(_cfg({"path": "/tmp"}))
                assert False, "expected reload failure"
            except RuntimeError as exc:
                assert "prior config state was restored" in str(exc)
            with open(ss.SMB_CONF, encoding="utf-8") as f:
                assert f.read() == "previous config\n"
            assert len(reload_calls) == 2
    finally:
        ss.SMB_CONF = original_conf
        ss._write_config = original_write
        ss._reload_samba = original_reload

    # 7. Status recognizes the unified samba.service unit
    original_status = ss._samba_status
    original_run = ss.subprocess.run
    try:
        ss._samba_status = lambda: {"installed": True, "missing_tools": [], "install_hint": ""}

        def fake_systemctl(args, **_kwargs):
            active = args == ["systemctl", "is-active", "samba"]
            enabled = args == ["systemctl", "is-enabled", "samba"]
            return subprocess.CompletedProcess(args, 0 if active or enabled else 3,
                                               stdout="active\n" if active else "enabled\n" if enabled else "inactive\n")

        ss.subprocess.run = fake_systemctl
        status = ss.get_status()
        assert status["active"] is True and status["enabled"] is True
        assert status["service"] == "samba"
    finally:
        ss._samba_status = original_status
        ss.subprocess.run = original_run

    print("OK — shares_service guest-write auto-fix tests passed")


if __name__ == "__main__":
    main()
