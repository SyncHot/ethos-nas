"""End-to-end test — USB partition management (NAS-style).

Exercises the real block-device code paths of backend/services/usb_partition_service.py
(initialize / format / create partition / delete partition) against a file-backed
loop device, with lsblk transport faked to "usb" and fstab + mount root redirected
to temp files so nothing on this machine is touched.

Requires: root (losetup/parted/mkfs/mount), a kernel with loop support.
Run:  sudo .venv/bin/python tests/test_usb_partition.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services import usb_mount_service as um
from backend.services.usb_mount_service import UsbMountError
from backend.services import usb_partition_service as up

DISK_SIZE_GB = 1.0
FAILURES = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)
    return bool(ok)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def main():
    if os.geteuid() != 0:
        print("This test must run as root (losetup/parted/mkfs). Use: sudo .venv/bin/python tests/test_usb_partition.py")
        sys.exit(2)

    tmp = tempfile.mkdtemp(prefix="ethos-usb-test-")
    img = os.path.join(tmp, "disk.img")
    fstab_path = os.path.join(tmp, "fstab")
    mnt_root = os.path.join(tmp, "mnt")
    open(fstab_path, "w").close()  # start with an empty fake fstab
    os.makedirs(mnt_root)

    # Fake USB transport for every lsblk node so the loop device passes validation.
    real_lsblk_tree = um._lsblk_tree

    def fake_lsblk_tree():
        tree = real_lsblk_tree()

        def fix(nodes):
            for n in nodes:
                n["tran"] = "usb"
                if n.get("type") == "loop":  # lsblk types loop devices as "loop", not "disk"
                    n["type"] = "disk"
                fix(n.get("children", []) or [])

        fix(tree)
        return tree

    # Allow /dev/loopN (and loopNpM) through the device-name regex — the lsblk
    # transport check above still runs, so nothing else gets loosened.
    import re as _re
    real_dev_re = um._DEV_RE
    um._DEV_RE = _re.compile(
        r"^/dev/(loop\d+(p\d+)?|sd[a-z]+\d*|vd[a-z]+\d*"
        r"|nvme\d+n\d+(p\d+)?|mmcblk\d+(p\d+)?)$"
    )

    # Redirect all privileged state to temp locations.
    um.FSTAB_PATH = fstab_path
    um.MOUNT_ROOT = mnt_root
    um.STATE_PATH = os.path.join(tmp, "usb_mounts.json")
    um._lsblk_tree = fake_lsblk_tree

    loop_dev = None
    try:
        with open(img, "wb") as f:
            f.truncate(int(DISK_SIZE_GB * 1024**3))
        loop_dev = run(["losetup", "-f", "--show", img])
        print(f"Test disk: {loop_dev} ({DISK_SIZE_GB:.0f} GB file-backed)")

        # ── 1. Initialize (destructive) ───────────────────────────────────
        print("\n[1] initialize_disk → ext4 'TESTVOL'")
        res = up.initialize_disk(loop_dev, "ext4", "TESTVOL", auto_mount=True)
        check("init returns a partition", bool(res.get("partition")), str(res))
        p1 = f"{loop_dev}p1"
        check("partition device exists", os.path.exists(p1), p1)
        blkid_out = run(["blkid", "-o", "value", "-s", "TYPE", p1])
        check("filesystem is ext4", blkid_out == "ext4", blkid_out)
        mp1 = os.path.join(mnt_root, "TESTVOL")
        check("mounted at <root>/TESTVOL", _is_mounted(mp1), mp1)
        fstab_lines = [l for l in open(fstab_path).read().splitlines() if l.strip()]
        check("fstab entry written (PARTUUID)", any("PARTUUID=" in l and "nofail" in l for l in fstab_lines), str(fstab_lines))

        # ── 2. Relabel without reformatting (mounted, same path) ───────────
        print("\n[2] relabel_usb → ext4 'TESTV2' (mounted, same path)")
        res = up.relabel_usb(p1, "TESTV2")
        check("relabel reports changed+remounted", res.get("changed") is True and res.get("remounted") is True, str(res))
        blkid_label = run(["blkid", "-o", "value", "-s", "LABEL", p1])
        check("ext4 label updated", blkid_label == "TESTV2", blkid_label)
        check("still mounted at same path <root>/TESTVOL", _is_mounted(mp1), mp1)
        fstab_lines = [l for l in open(fstab_path).read().splitlines() if l.strip()]
        check("fstab entry untouched by relabel", any("/TESTVOL" in l and "PARTUUID=" in l for l in fstab_lines), str(fstab_lines))

        # ── 3. Format the partition (vfat, new label) ─────────────────────
        print("\n[3] format_usb → vfat 'FATTEST'")
        res = up.format_usb(p1, "vfat", "FATTEST")
        check("format reports remounted", res.get("remounted") is True, str(res))
        blkid_out = run(["blkid", "-o", "value", "-s", "TYPE", p1])
        check("filesystem now vfat", blkid_out == "vfat", blkid_out)
        mp2 = os.path.join(mnt_root, "FATTEST")
        check("remounted at <root>/FATTEST", _is_mounted(mp2), mp2)
        fstab_lines = [l for l in open(fstab_path).read().splitlines() if l.strip()]
        check("fstab entry updated to new label/fstype", any("/FATTEST" in l and "vfat" in l for l in fstab_lines), str(fstab_lines))

        # ── 4. Relabel vfat (mounted, same path) ───────────────────────────
        print("\n[4] relabel_usb → vfat 'FATNEW' (mounted, same path)")
        res = up.relabel_usb(p1, "FATNEW")
        check("relabel reports changed+remounted", res.get("changed") is True and res.get("remounted") is True, str(res))
        blkid_label = run(["blkid", "-o", "value", "-s", "LABEL", p1])
        check("vfat label updated (uppercased)", blkid_label == "FATNEW", blkid_label)
        check("still mounted at same path <root>/FATTEST", _is_mounted(mp2), mp2)

        # ── 5. Delete the partition ───────────────────────────────────────
        print("\n[5] delete_partition")
        res = up.delete_partition(p1)
        check("fstab entry removed", res.get("fstab_entries_removed") == 1, str(res))
        check("partition node gone (eventually)", not os.path.exists(p1), p1)

        # ── 6. Create a sized partition + one filling the rest ────────────
        print("\n[6] create_partition 0.3 GB ext4 'PARTA'")
        res = up.create_partition(loop_dev, size_gb=0.3, fstype="ext4", label="PARTA", auto_mount=True)
        check("created partition", bool(res.get("partition")), str(res))
        p1 = f"{loop_dev}p1"
        mp_a = os.path.join(mnt_root, "PARTA")
        check("PARTA mounted", _is_mounted(mp_a), mp_a)

        print("\n[7] create_partition (all remaining space) exfat 'PARTB' + relabel unmounted")
        res = up.create_partition(loop_dev, size_gb=None, fstype="exfat", label="PARTB", auto_mount=True)
        check("created second partition", bool(res.get("partition")), str(res))
        p2 = f"{loop_dev}p2"
        mp_b = os.path.join(mnt_root, "PARTB")
        check("PARTB mounted", _is_mounted(mp_b), mp_b)

        # No free space left → must fail cleanly.
        try:
            up.create_partition(loop_dev, size_gb=None, fstype="ext4", label="NOPE")
            check("third partition rejected (no free space)", False, "no error raised")
        except UsbMountError as e:
            check("third partition rejected (no free space)", True)

        # Unmounted relabel: no remount, fstab untouched.
        um.unmount_usb(p2)  # also removes its fstab entry
        res = up.relabel_usb(p2, "PARTC")
        check("relabel unmounted ok", res.get("changed") is True and res.get("remounted") is False, str(res))
        blkid_label = run(["blkid", "-o", "value", "-s", "LABEL", p2])
        check("exfat label updated (unmounted)", blkid_label == "PARTC", blkid_label)
        check("still unmounted after relabel", not _is_mounted(mp_b), mp_b)

        # ── 8. Re-initialize wipes everything and cleans stale fstab ──────
        print("\n[8] initialize_disk → exfat 'FINAL' (stale-entry cleanup)")
        res = up.initialize_disk(loop_dev, "exfat", "FINAL", auto_mount=True)
        check("re-init returns partition", bool(res.get("partition")), str(res))
        check("old fstab entries cleaned", res.get("fstab_entries_removed", 0) >= 1, str(res))
        blkid_out = run(["blkid", "-o", "value", "-s", "TYPE", f"{loop_dev}p1"])
        check("filesystem now exfat", blkid_out == "exfat", blkid_out)
        mp_f = os.path.join(mnt_root, "FINAL")
        check("mounted at <root>/FINAL", _is_mounted(mp_f), mp_f)

        # ── 9. Mount/unmount cycles keep the same path (no folder churn) ───
        print("\n[9] mount/unmount stability (remembered paths)")
        um.unmount_usb(f"{loop_dev}p1")
        res = um.mount_usb(f"{loop_dev}p1")
        check("remount reuses remembered path", res.get("mountpoint") == mp_f, str(res))

        um.unmount_usb(f"{loop_dev}p1")
        res = um.mount_usb(f"{loop_dev}p1", folder="MyBackup")
        check("explicit folder honored", res.get("mountpoint") == os.path.join(mnt_root, "MyBackup"), str(res))

        um.unmount_usb(f"{loop_dev}p1")
        res = um.mount_usb(f"{loop_dev}p1")
        check("folder choice remembered across cycles", res.get("mountpoint") == os.path.join(mnt_root, "MyBackup"), str(res))

        leftovers = [d for d in os.listdir(mnt_root) if d != "MyBackup"]
        check("no stale/numbered duplicate folders left behind", leftovers == [], str(leftovers))

    finally:
        um._DEV_RE = real_dev_re
        # Cleanup: unmount everything we mounted, detach loop, remove temp dir.
        for entry in um._lsblk_tree():
            mps = [m["path"] for m in entry.get("mountpoints") or []]
            for mp_ in mps:
                if mp_.startswith(mnt_root):
                    try:
                        subprocess.run(["umount", "-l", mp_], capture_output=True, timeout=15)
                    except Exception:
                        pass
        if loop_dev:
            try:
                run(["losetup", "-d", loop_dev])
            except RuntimeError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} check(s) FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("✅ All USB partition management checks passed.")


def _is_mounted(path):
    try:
        out = subprocess.run(["findmnt", "-n", "-o", "TARGET", path], capture_output=True, text=True).stdout.strip()
        return bool(out)
    except Exception:
        return False


class _Tee:
    def __init__(self, *streams):
        self.streams = streams
    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass


if __name__ == "__main__":
    _log = open("/tmp/ethos-usb-test.log", "w")
    sys.stdout = _Tee(sys.__stdout__, _log)
    sys.stderr = _Tee(sys.__stderr__, _log)
    try:
        main()
    finally:
        os.chmod("/tmp/ethos-usb-test.log", 0o644)
