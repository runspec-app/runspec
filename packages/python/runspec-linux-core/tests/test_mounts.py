"""The mount-boundary guard shared by find_large_files and (via its verbatim twin
in runspec-fs-core) the filesystem runnable's directory-size verb."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from runspec_linux_core import _mounts
from runspec_linux_core._mounts import MountGuard, parse_mountinfo, parse_proc_mounts, read_mount_types

posix_only = pytest.mark.skipif(os.name != "posix", reason="mount tables are POSIX-only")
linux_proc = pytest.mark.skipif(not sys.platform.startswith("linux") or not os.path.ismount("/proc"), reason="needs a Linux /proc mount")

MOUNTINFO = (
    "22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw\n"
    "23 22 0:21 / /proc rw,nosuid shared:12 - proc proc rw\n"
    "24 22 0:22 / /sys rw,nosuid shared:7 - sysfs sysfs rw\n"
    "30 22 8:17 / /mnt/backup\\040disk rw,relatime shared:30 master:2 - ext4 /dev/sdb1 rw\n"
    "31 24 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n"
)
TABLE = {"/": "ext4", "/proc": "proc", "/sys": "sysfs", "/sys/fs/cgroup": "cgroup2", "/home": "ext4", "/net": "nfs4"}


def test_parse_mountinfo_types_and_escapes() -> None:
    types = parse_mountinfo(MOUNTINFO)
    assert types["/"] == "ext4"
    assert types["/proc"] == "proc"
    assert types["/mnt/backup disk"] == "ext4"  # \040 decoded; optional fields skipped
    assert types["/sys/fs/cgroup"] == "cgroup2"


def test_parse_mountinfo_last_entry_wins_and_skips_junk() -> None:
    text = "1 0 8:1 / /data rw - ext4 /dev/sdc1 rw\nnot a mount line\n2 0 0:40 / /data rw - tmpfs tmpfs rw\n"
    assert parse_mountinfo(text) == {"/data": "tmpfs"}


def test_parse_proc_mounts() -> None:
    text = "/dev/sda2 / ext4 rw,relatime 0 0\nproc /proc proc rw 0 0\n/dev/sdb1 /srv/my\\040share xfs rw 0 0\n"
    assert parse_proc_mounts(text) == {"/": "ext4", "/proc": "proc", "/srv/my share": "xfs"}


def test_read_mount_types_falls_back_to_proc_mounts(tmp_path: Path) -> None:
    mounts = tmp_path / "mounts"
    mounts.write_text("proc /proc proc rw 0 0\n")
    assert read_mount_types(str(tmp_path / "missing"), str(mounts)) == {"/proc": "proc"}
    assert read_mount_types(str(tmp_path / "missing"), str(tmp_path / "also-missing")) is None


def test_same_device_is_always_entered() -> None:
    guard = MountGuard("/", mount_types=TABLE)
    assert guard.enter("/usr", 1, 1)
    assert guard.skipped_mounts == 0


def test_default_stays_on_one_filesystem() -> None:
    guard = MountGuard("/", mount_types=TABLE)
    assert not guard.enter("/home", 2, 1)
    assert not guard.enter("/proc", 3, 1)
    assert guard.skipped_mounts == 2


@posix_only
def test_cross_mounts_enters_real_filesystems_only() -> None:
    guard = MountGuard("/", cross_mounts=True, mount_types=TABLE)
    assert guard.enter("/home", 2, 1)
    assert guard.enter("/net", 4, 1)
    assert not guard.enter("/proc", 3, 1)
    assert guard.skipped_mounts == 1


@posix_only
def test_prefix_fallback_when_the_table_is_unreadable() -> None:
    guard = MountGuard("/", cross_mounts=True, mount_types={})
    assert not guard.enter("/sys", 5, 1)  # no table: /sys is recognised by its path
    assert guard.enter("/mnt/usb", 6, 1)
    assert guard.skipped_mounts == 1


@posix_only
@pytest.mark.parametrize("root", ["/proc", "/proc/self", "/sys/fs/cgroup"])
def test_root_on_a_virtual_filesystem_is_refused(root: str) -> None:
    with pytest.raises(ValueError, match="virtual filesystem"):
        MountGuard(root, mount_types=TABLE)


@posix_only
def test_root_on_a_real_filesystem_is_accepted() -> None:
    assert MountGuard("/home/someone", mount_types=TABLE).skipped_mounts == 0


@linux_proc
def test_live_proc_is_never_entered_from_root() -> None:
    root_dev = os.stat("/").st_dev
    proc_dev = os.lstat("/proc").st_dev
    for cross_mounts in (False, True):
        guard = MountGuard("/", cross_mounts=cross_mounts)
        assert not guard.enter("/proc", proc_dev, root_dev)
    with pytest.raises(ValueError, match="virtual filesystem"):
        MountGuard("/proc")


def test_identical_to_the_runspec_fs_core_copy() -> None:
    """_mounts.py is a deliberate verbatim twin of runspec-fs-core's: each core
    stays dependency-free, so neither can import it from the other."""
    twin = Path(__file__).resolve().parents[2] / "runspec-fs-core" / "runspec_fs_core" / "_mounts.py"
    if not twin.is_file():
        pytest.skip("runspec-fs-core source is not alongside (not a monorepo checkout)")
    assert Path(_mounts.__file__).read_bytes() == twin.read_bytes(), "keep runspec_linux_core/_mounts.py and runspec_fs_core/_mounts.py identical"
