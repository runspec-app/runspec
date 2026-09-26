"""The mount-boundary guard behind directory_size is a verbatim twin of
runspec-linux-core's (where its unit tests live)."""

from __future__ import annotations

from pathlib import Path

import pytest

from runspec_fs_core import _mounts


def test_identical_to_the_runspec_linux_core_copy() -> None:
    """Each core stays dependency-free, so neither can import _mounts.py from the other."""
    twin = Path(__file__).resolve().parents[2] / "runspec-linux-core" / "runspec_linux_core" / "_mounts.py"
    if not twin.is_file():
        pytest.skip("runspec-linux-core source is not alongside (not a monorepo checkout)")
    assert Path(_mounts.__file__).read_bytes() == twin.read_bytes(), "keep runspec_fs_core/_mounts.py and runspec_linux_core/_mounts.py identical"
