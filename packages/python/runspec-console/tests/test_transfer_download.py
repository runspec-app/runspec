"""
test_transfer_download.py — the remote→local download path (_transfer_one pull).

paramiko's ``sftp.get`` asserts the downloaded size equals the remote ``stat()``
size, which is always 0 for procfs/sysfs even though those files stream real
bytes — so a ``/proc/cpuinfo`` pull used to always fail with "size mismatch".
``_sftp_download`` streams via ``getfo`` (no assertion) for real files and a
bounded raw read for special (size-0) files. A scripted fake SFTP stands in for
paramiko so the logic is exercised without a server.
"""

from __future__ import annotations

import stat
import types

import pytest

from runspec_console.bridge import Bridge


class _DlBridge:
    """Light harness: the real download methods, a recording _dispatch."""

    _transfer_one = Bridge._transfer_one
    _sftp_download = Bridge._sftp_download
    _download_special = Bridge._download_special
    _SPECIAL_FILE_CAP = Bridge._SPECIAL_FILE_CAP

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def _dispatch(self, name, detail):
        self.events.append((name, detail))


class _FakeStat:
    def __init__(self, mode: int, size: int):
        self.st_mode = mode
        self.st_size = size


class _FakeRemoteFile:
    def __init__(self, data: bytes):
        self._data = data

    def read(self, n: int) -> bytes:
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeSftp:
    """Stands in for a paramiko SFTPClient for one file."""

    def __init__(self, mode: int, size: int, data: bytes):
        self._mode = mode
        self._size = size
        self._data = data
        self.getfo_calls: list[str] = []

    def stat(self, _path: str) -> _FakeStat:
        return _FakeStat(self._mode, self._size)

    def getfo(self, remotepath: str, fl, callback=None) -> None:
        self.getfo_calls.append(remotepath)
        fl.write(self._data)
        if callback:
            callback(len(self._data), len(self._data))

    def open(self, _path: str, _mode: str) -> _FakeRemoteFile:
        return _FakeRemoteFile(self._data)


def _item(tmp_path, src: str, name: str):
    return types.SimpleNamespace(src=src, dst=str(tmp_path / name), name=name)


def test_download_regular_uses_getfo(tmp_path):
    b = _DlBridge()
    data = b"127.0.0.1 localhost\n"
    sftp = _FakeSftp(stat.S_IFREG | 0o644, len(data), data)
    item = _item(tmp_path, "/etc/hosts", "hosts")

    b._transfer_one(sftp, "pull", item, "t", 0, 1)

    assert sftp.getfo_calls == ["/etc/hosts"]
    assert (tmp_path / "hosts").read_bytes() == data


def test_download_special_proc_streams_full_content(tmp_path):
    # procfs reports st_size 0 but streams real bytes — the regression case.
    b = _DlBridge()
    data = b"processor\t: 0\n" * 50
    sftp = _FakeSftp(stat.S_IFREG | 0o444, 0, data)
    item = _item(tmp_path, "/proc/cpuinfo", "cpuinfo")

    b._transfer_one(sftp, "pull", item, "t", 0, 1)

    assert (tmp_path / "cpuinfo").read_bytes() == data
    # special files bypass getfo (bounded raw read) and report total 0.
    assert sftp.getfo_calls == []
    assert b.events and all(detail["total"] == 0 for _, detail in b.events)


def test_download_special_over_cap_raises(tmp_path):
    b = _DlBridge()
    b._SPECIAL_FILE_CAP = 128  # tiny cap for the test
    sftp = _FakeSftp(stat.S_IFREG, 0, b"x" * 4096)
    item = _item(tmp_path, "/proc/kcore", "kcore")

    with pytest.raises(OSError, match="streaming/special file"):
        b._transfer_one(sftp, "pull", item, "t", 0, 1)


def test_download_directory_rejected(tmp_path):
    b = _DlBridge()
    sftp = _FakeSftp(stat.S_IFDIR | 0o755, 4096, b"")
    item = _item(tmp_path, "/var/log", "log")

    with pytest.raises(IsADirectoryError):
        b._transfer_one(sftp, "pull", item, "t", 0, 1)
