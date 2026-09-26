"""Mount-boundary guard for recursive disk scans.

Keeps a scan that walks a directory tree from wandering into other filesystems.
By default it stays on the filesystem it started on (the ``du -x`` /
``find -xdev`` rule). Kernel virtual filesystems (``/proc``, ``/sys``, ``/dev``
…) are never entered, even when crossing mounts is allowed: they hold no data on
disk, and ``/proc/kcore`` alone reports a size of ~128 TiB.

Deliberately duplicated, byte for byte, in ``runspec-linux-core`` and
``runspec-fs-core`` (each core stays dependency-free); a parity test in each
package keeps the two copies identical.
"""

from __future__ import annotations

import os
import re

#: Kernel pseudo filesystems with no disk behind them — never scanned.
VIRTUAL_FSTYPES = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fusectl",
        "hugetlbfs",
        "mqueue",
        "nsfs",
        "proc",
        "pstore",
        "rpc_pipefs",
        "securityfs",
        "selinuxfs",
        "sysfs",
        "tracefs",
    }
)

#: The standard virtual trees and their usual type, used when the mount table
#: can't be read or doesn't list a path (POSIX only).
FALLBACK_VIRTUAL_PREFIXES = {"/proc": "proc", "/sys": "sysfs", "/dev": "devtmpfs"}

_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str:
    """Decode the octal escapes (``\\040`` for a space …) used in mount tables."""
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text: str) -> dict[str, str]:
    """Map mount point → filesystem type from ``/proc/self/mountinfo`` text.

    Each line is ``id parent major:minor root mount-point options [optional …]
    - fstype source super-options``. When a mount point is listed more than once
    the last line wins — it is the mount on top, the one a path resolves to.
    """
    types: dict[str, str] = {}
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if sep and len(fields) >= 5 and tail:
            types[_unescape(fields[4])] = tail[0]
    return types


def parse_proc_mounts(text: str) -> dict[str, str]:
    """Map mount point → filesystem type from ``/proc/mounts`` text
    (``source mount-point fstype options dump pass``)."""
    types: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            types[_unescape(fields[1])] = fields[2]
    return types


def read_mount_types(
    mountinfo: str = "/proc/self/mountinfo",
    mounts: str = "/proc/mounts",
) -> dict[str, str] | None:
    """The live mount table as ``{mount point: fstype}``, or ``None`` when it
    can't be read (not Linux, or a restricted ``/proc``)."""
    for path, parse in ((mountinfo, parse_mountinfo), (mounts, parse_proc_mounts)):
        try:
            with open(path, encoding="utf-8", errors="surrogateescape") as f:
                types = parse(f.read())
        except OSError:
            continue
        if types:
            return types
    return None


class MountGuard:
    """Decides which directories a recursive scan may enter.

    With ``cross_mounts=False`` (the default) the scan stays on the filesystem it
    started on. With ``True`` it may enter other mounted filesystems (a separate
    ``/home`` disk, a network share), but never a virtual one. Each directory the
    scan refuses to enter is counted in :attr:`skipped_mounts`.

    Raises :class:`ValueError` when ``root`` itself is on a virtual filesystem —
    there is nothing on disk there to measure. On Windows only the device
    comparison applies; there is no mount table to consult.
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        cross_mounts: bool = False,
        mount_types: dict[str, str] | None = None,
    ) -> None:
        self.cross_mounts = cross_mounts
        self.skipped_mounts = 0
        self._posix = os.name == "posix"
        if mount_types is not None:
            self._types: dict[str, str] | None = mount_types
        else:
            self._types = read_mount_types() if self._posix else None
        fstype = self._virtual_type(os.fspath(root), containing=True)
        if fstype:
            raise ValueError(f"{os.fspath(root)} is on a virtual filesystem ({fstype}); it holds no files on disk to measure")

    def enter(self, path: str, dev: int, parent_dev: int) -> bool:
        """Whether the scan may descend into directory ``path`` (on device
        ``dev``) from its parent directory (on device ``parent_dev``). A refusal
        is counted in :attr:`skipped_mounts`."""
        if dev == parent_dev:
            return True
        if self.cross_mounts and self._virtual_type(path, containing=False) is None:
            return True
        self.skipped_mounts += 1
        return False

    def _virtual_type(self, path: str, *, containing: bool) -> str | None:
        """The virtual filesystem type at ``path`` — the mount point itself, or
        with ``containing`` the mount the path lies in — or ``None`` if the
        filesystem there is a real one."""
        if not self._posix:
            return None
        real = os.path.realpath(path)
        fstype = self._mount_type(real, containing=containing)
        if fstype is not None:
            return fstype if fstype in VIRTUAL_FSTYPES else None
        for prefix, prefix_type in FALLBACK_VIRTUAL_PREFIXES.items():
            if real == prefix or real.startswith(prefix + "/"):
                return prefix_type
        return None

    def _mount_type(self, real: str, *, containing: bool) -> str | None:
        """The table's fstype for mount point ``real``, or with ``containing``
        for the longest mount point that ``real`` lies under."""
        if not self._types:
            return None
        if not containing:
            return self._types.get(real)
        best: str | None = None
        best_len = -1
        for mount_point, fstype in self._types.items():
            under = mount_point if mount_point.endswith("/") else mount_point + "/"
            if (real == mount_point or real.startswith(under)) and len(mount_point) > best_len:
                best, best_len = fstype, len(mount_point)
        return best
