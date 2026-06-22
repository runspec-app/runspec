"""DNS resolution against loopback — no external network needed."""

import pytest

from runspec_webops_core import resolve
from runspec_webops_core.dns import _is_ip
from runspec_webops_core.errors import ResolveError


def test_is_ip() -> None:
    assert _is_ip("127.0.0.1")
    assert _is_ip("::1")
    assert not _is_ip("localhost")


def test_resolve_localhost_name() -> None:
    res = resolve("localhost")
    assert res["is_ip"] is False
    ips = {entry["ip"] for entry in res["addresses"]}
    assert "127.0.0.1" in ips or "::1" in ips


def test_resolve_ip_is_reverse_only() -> None:
    res = resolve("127.0.0.1")
    assert res["is_ip"] is True
    assert "127.0.0.1" in res["reverse"]


def test_resolve_unknown_name_raises() -> None:
    with pytest.raises(ResolveError):
        resolve("nonexistent.invalid")
