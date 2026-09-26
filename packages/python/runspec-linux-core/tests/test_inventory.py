"""Hardware inventory helpers: block_devices (lsblk) and network_interfaces (ip addr)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from runspec_linux_core import CommandError, ToolNotFoundError, block_devices, network, network_interfaces, system

# util-linux >= 2.33 emits typed JSON; MOUNTPOINTS (>= 2.37) is a list that can hold nulls.
LSBLK_TYPED = {
    "blockdevices": [
        {"name": "loop0", "kname": "loop0", "type": "loop", "size": 4096, "rota": False, "rm": False, "mountpoints": ["/snap/core/1"]},
        {
            "name": "sda",
            "kname": "sda",
            "type": "disk",
            "size": 500107862016,
            "rota": False,
            "rm": False,
            "tran": "sata",
            "model": "Samsung SSD 860 EVO 500GB",
            "serial": "S3Z1NB0K",
            "fstype": None,
            "mountpoints": [None],
            "children": [
                {"name": "sda1", "kname": "sda1", "type": "part", "size": 536870912, "rota": False, "rm": False, "fstype": "vfat", "uuid": "AB12-CD34", "mountpoints": ["/boot/efi"]},
                {
                    "name": "sda2",
                    "kname": "sda2",
                    "type": "part",
                    "size": 499570991104,
                    "rota": False,
                    "rm": False,
                    "fstype": "LVM2_member",
                    "mountpoints": [None],
                    "children": [{"name": "vg-root", "kname": "dm-0", "type": "lvm", "size": 107374182400, "rota": False, "rm": False, "fstype": "ext4", "mountpoints": ["/"]}],
                },
            ],
        },
    ]
}

# Older util-linux: every value a string, a single MOUNTPOINT column, padded model names.
LSBLK_STRINGS = {
    "blockdevices": [
        {"name": "sdb", "kname": "sdb", "type": "disk", "size": "4000787030016", "rota": "1", "rm": "0", "tran": "sata", "model": "WDC WD40EFRX    ", "mountpoint": None},
        {"name": "sdc", "kname": "sdc", "type": "disk", "size": "64023257088", "rota": "0", "rm": "1", "tran": "usb", "mountpoint": "/media/usb"},
    ]
}


def _fake_lsblk(monkeypatch: pytest.MonkeyPatch, *responses: subprocess.CompletedProcess[str]) -> list[list[str]]:
    calls: list[list[str]] = []
    queue = list(responses)

    def fake_run(argv, capture_output=None, text=None):  # noqa: A002
        calls.append(argv)
        return queue.pop(0)

    monkeypatch.setattr(system, "which", lambda name: "/usr/bin/lsblk")
    monkeypatch.setattr(system.subprocess, "run", fake_run)
    return calls


def _ok(payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")


def test_block_devices_nests_the_tree_and_hides_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_lsblk(monkeypatch, _ok(LSBLK_TYPED))
    devices = block_devices()["devices"]
    assert [d["name"] for d in devices] == ["sda"]  # loop0 hidden
    sda = devices[0]
    assert sda["rotational"] is False  # an SSD
    assert sda["transport"] == "sata" and sda["model"] == "Samsung SSD 860 EVO 500GB"
    assert sda["size_gib"] == 465.8
    assert "mountpoints" not in sda and "fstype" not in sda  # empty fields dropped
    efi, pv = sda["children"]
    assert efi["mountpoints"] == ["/boot/efi"] and efi["uuid"] == "AB12-CD34"
    assert pv["children"][0] == {
        "name": "vg-root",
        "kname": "dm-0",
        "type": "lvm",
        "size_bytes": 107374182400,
        "size_gib": 100.0,
        "rotational": False,
        "removable": False,
        "fstype": "ext4",
        "mountpoints": ["/"],
    }
    assert calls[0][-1].endswith(",MOUNTPOINTS")


def test_block_devices_include_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_lsblk(monkeypatch, _ok(LSBLK_TYPED))
    assert [d["name"] for d in block_devices(include_loop=True)["devices"]] == ["loop0", "sda"]


def test_block_devices_normalises_old_string_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_lsblk(monkeypatch, _ok(LSBLK_STRINGS))
    hdd, usb = block_devices()["devices"]
    assert hdd["rotational"] is True and hdd["size_bytes"] == 4000787030016 and hdd["model"] == "WDC WD40EFRX"
    assert usb["rotational"] is False and usb["removable"] is True and usb["mountpoints"] == ["/media/usb"]


def test_block_devices_falls_back_to_the_single_mountpoint_column(monkeypatch: pytest.MonkeyPatch) -> None:
    unknown = subprocess.CompletedProcess([], 1, stdout="", stderr="lsblk: unknown column: MOUNTPOINTS\n")
    calls = _fake_lsblk(monkeypatch, unknown, _ok(LSBLK_STRINGS))
    assert len(block_devices()["devices"]) == 2
    assert calls[1][-1].endswith(",MOUNTPOINT")


def test_block_devices_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_lsblk(monkeypatch, subprocess.CompletedProcess([], 1, stdout="", stderr="lsblk: failed to access sysfs"))
    with pytest.raises(CommandError, match="sysfs"):
        block_devices()
    monkeypatch.setattr(system, "which", lambda name: None)
    with pytest.raises(ToolNotFoundError):
        block_devices()


@pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("lsblk"), reason="needs lsblk")
def test_block_devices_live() -> None:
    assert isinstance(block_devices()["devices"], list)


# ── network_interfaces ───────────────────────────────────────────────────────

IP_ADDR = [
    {
        "ifname": "lo",
        "operstate": "UNKNOWN",
        "link_type": "loopback",
        "address": "00:00:00:00:00:00",
        "mtu": 65536,
        "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host"}],
    },
    {
        "ifname": "enp1s0",
        "operstate": "UP",
        "link_type": "ether",
        "address": "6c:4b:90:aa:bb:cc",
        "mtu": 1500,
        "addr_info": [
            {"family": "inet", "local": "192.168.1.21", "prefixlen": 24, "scope": "global", "dynamic": True},
            {"family": "inet6", "local": "fe80::6e4b:90ff:feaa:bbcc", "prefixlen": 64, "scope": "link"},
        ],
    },
    {"ifname": "enp2s0", "operstate": "DOWN", "link_type": "ether", "address": "6c:4b:90:aa:bb:dd", "mtu": 1500, "addr_info": []},
    {
        "ifname": "cni0",
        "operstate": "UP",
        "link_type": "ether",
        "address": "a2:11:22:33:44:55",
        "mtu": 1450,
        "addr_info": [{"family": "inet", "local": "10.42.0.1", "prefixlen": 24, "scope": "global"}],
    },
    {"ifname": "veth1a2b", "operstate": "UP", "link_type": "ether", "address": "9e:00:00:00:00:01", "mtu": 1450, "master": "cni0", "addr_info": []},
    {"ifname": "wg0", "operstate": "UNKNOWN", "link_type": "none", "mtu": 1420, "addr_info": [{"family": "inet", "local": "10.8.0.2", "prefixlen": 32, "scope": "global"}]},
    {},
]


@pytest.fixture
def fake_net(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """``ip -json address`` returns IP_ADDR; a fake /sys/class/net where enp1s0
    (1 Gb/s, e1000e) and enp2s0 (no link) are physical cards."""
    drivers = tmp_path / "drivers"
    for nic, speed in (("enp1s0", "1000"), ("enp2s0", "-1")):
        (tmp_path / nic / "device").mkdir(parents=True)
        (tmp_path / nic / "speed").write_text(f"{speed}\n")
        (drivers / "e1000e").mkdir(parents=True, exist_ok=True)
        os.symlink(drivers / "e1000e", tmp_path / nic / "device" / "driver")
    monkeypatch.setattr(network, "SYS_CLASS_NET", str(tmp_path))
    monkeypatch.setattr(network, "which", lambda name: "/usr/sbin/ip")
    monkeypatch.setattr(network.subprocess, "run", lambda argv, capture_output=None, text=None: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(IP_ADDR), stderr=""))
    return tmp_path


def test_network_interfaces_rows(fake_net: Path) -> None:
    rows = {row["name"]: row for row in network_interfaces()["interfaces"]}
    assert list(rows) == ["lo", "enp1s0", "enp2s0", "cni0", "veth1a2b", "wg0"]  # the empty object is skipped
    assert rows["enp1s0"] == {
        "name": "enp1s0",
        "state": "UP",
        "physical": True,
        "type": "ether",
        "mac": "6c:4b:90:aa:bb:cc",
        "mtu": 1500,
        "addresses": [
            {"family": "inet", "address": "192.168.1.21", "prefix": 24, "scope": "global", "dynamic": True},
            {"family": "inet6", "address": "fe80::6e4b:90ff:feaa:bbcc", "prefix": 64, "scope": "link"},
        ],
        "speed_mbps": 1000,
        "driver": "e1000e",
    }
    assert "speed_mbps" not in rows["enp2s0"]  # -1: no link
    assert rows["lo"]["physical"] is False and "mac" not in rows["lo"]
    assert rows["veth1a2b"]["master"] == "cni0" and rows["veth1a2b"]["addresses"] == []
    assert "mac" not in rows["wg0"]


def test_network_interfaces_physical_only(fake_net: Path) -> None:
    assert [row["name"] for row in network_interfaces(physical_only=True)["interfaces"]] == ["enp1s0", "enp2s0"]


def test_network_interfaces_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network, "which", lambda name: "/usr/sbin/ip")
    monkeypatch.setattr(network.subprocess, "run", lambda argv, capture_output=None, text=None: subprocess.CompletedProcess(argv, 255, stdout="", stderr='Option "-json" is unknown'))
    with pytest.raises(CommandError, match="json"):
        network_interfaces()
    monkeypatch.setattr(network, "which", lambda name: None)
    with pytest.raises(ToolNotFoundError):
        network_interfaces()


@pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("ip"), reason="needs iproute2")
def test_network_interfaces_live() -> None:
    assert any(row["name"] == "lo" for row in network_interfaces()["interfaces"])
