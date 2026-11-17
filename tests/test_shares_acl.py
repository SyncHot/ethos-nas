#!/usr/bin/env python3
"""Share ACL and permission-mask tests without touching the live smb.conf."""

import configparser
import grp
import os
import pwd
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["ETHOS_DATA_DIR"] = tempfile.mkdtemp(prefix="ethos-shares-acl-")
os.environ["ETHOS_JWT_SECRET"] = "shares-acl-test-secret"
sys.path.insert(0, str(ROOT))

from backend.services import shares_service as ss  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    original = {
        "require": ss.require_samba,
        "load": ss._load_config,
        "write": ss._write_config,
        "reload": ss._reload_samba,
        "getpw": pwd.getpwnam,
        "getgr": grp.getgrnam,
    }
    saved = {}
    try:
        config = configparser.ConfigParser(interpolation=None)
        config["global"] = {"workgroup": "WORKGROUP"}
        ss.require_samba = lambda: {"installed": True}
        ss._load_config = lambda: config
        ss._write_config = lambda value: saved.update(config=value)
        ss._reload_samba = lambda: None
        pwd.getpwnam = lambda name: pwd.struct_passwd((name, "x", 1000, 1000, "", "/home/" + name, "/bin/bash"))
        grp.getgrnam = lambda name: grp.struct_group((name, "x", 1000, ["alice"]))

        with tempfile.TemporaryDirectory() as shared_dir:
            created = ss.add_share(
                "Media", shared_dir, read_users=["alice"], write_users=["@storage"],
                create_mask="644", directory_mask="0750",
            )
        check("share creation reports success", created["status"] == "added")
        section = saved["config"]["Media"]
        check("ACL limits valid users to selected identities",
              section["valid users"] == "alice @storage", dict(section))
        check("read users become read-only entries", section["read list"] == "alice", dict(section))
        check("write users become write entries", section["write list"] == "@storage", dict(section))
        check("custom ACLs default to read-only", section["read only"] == "yes", dict(section))
        check("three-digit masks are normalized", section["create mask"] == "0644", dict(section))
        check("directory mask is preserved", section["directory mask"] == "0750", dict(section))

        listed = ss.get_shares()
        media = next(item for item in listed if item["name"] == "Media")
        check("share listing exposes ACL arrays", media["read_users"] == ["alice"]
              and media["write_users"] == ["@storage"], str(media))


        config["PercentPath"] = {"path": "/srv/%U/%S", "read only": "yes"}
        parsed = ss.get_shares()
        percent_share = next(item for item in parsed if item["name"] == "PercentPath")
        check("Samba percent substitutions load literally", percent_share["path"] == "/srv/%U/%S")

        writes_before = len(saved)
        try:
            ss.add_share("BadMask", "/tmp", create_mask="0999")
            mask_rejected = False
        except ValueError:
            mask_rejected = True
        check("invalid octal mask is rejected", mask_rejected)
        check("invalid mask is rejected before config write", len(saved) == writes_before)

        try:
            ss.update_share("Media", read_users=["alice"], write_users=["alice"])
            overlap_rejected = False
        except ValueError:
            overlap_rejected = True
        check("same principal cannot be reader and writer", overlap_rejected)

        try:
            ss.update_share("Media", read_only=True)
            readonly_writer_rejected = False
        except ValueError:
            readonly_writer_rejected = True
        check("read-only share cannot retain write-list entries", readonly_writer_rejected)

        try:
            ss.add_share("GuestAcl", "/tmp", guest_ok=True, valid_users="alice")
            guest_acl_rejected = False
        except ValueError:
            guest_acl_rejected = True
        check("guest access cannot be combined with valid users", guest_acl_rejected)

        legacy = ss._parse_valid_users("alice, DOMAIN\\user, %U")
        check("safe legacy Samba valid_users tokens remain supported",
              legacy == ["alice", "DOMAIN\\user", "%U"], str(legacy))

        def missing_user(_name):
            raise KeyError(_name)
        pwd.getpwnam = missing_user
        try:
            ss.update_share("Media", read_users=["unknown"])
            unknown_rejected = False
        except ValueError:
            unknown_rejected = True
        check("unknown Unix account cannot enter an ACL", unknown_rejected)

        try:
            ss.update_share("Media", path="relative/path")
            relative_rejected = False
        except ValueError:
            relative_rejected = True
        check("share paths must be absolute directories", relative_rejected)

        for operation in (ss.update_share, ss.delete_share):
            try:
                operation("global", comment="unsafe") if operation is ss.update_share else operation("global")
                special_rejected = False
            except ValueError:
                special_rejected = True
            check(f"{operation.__name__} refuses the global section", special_rejected)
    finally:
        ss.require_samba = original["require"]
        ss._load_config = original["load"]
        ss._write_config = original["write"]
        ss._reload_samba = original["reload"]
        pwd.getpwnam = original["getpw"]
        grp.getgrnam = original["getgr"]

    mode_config = configparser.ConfigParser(interpolation=None)
    mode_config["DefaultMode"] = {"path": "/srv/default"}
    mode_config["WritableAlias"] = {"path": "/srv/writeable", "writeable": "yes"}
    default_mode = ss._share_is_read_only(mode_config, "DefaultMode")
    writable_alias = ss._share_is_read_only(mode_config, "WritableAlias")
    check("Samba default share mode is read-only", default_mode)
    check("Samba writeable alias is honored", not writable_alias)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())