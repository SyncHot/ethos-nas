"""Unit test — Samba share guest-write auto-fix (shares_service._apply_guest_write_fix).

Public writable shares must automatically get `force user`/`force group` set to the
directory owner, so guest sessions can write without manual smb.conf edits.

Run:  .venv/bin/python tests/test_shares_service.py   (no server needed)
"""
import configparser
import grp
import os
import pwd
import sys
import tempfile

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

    print("OK — shares_service guest-write auto-fix tests passed")


if __name__ == "__main__":
    main()
