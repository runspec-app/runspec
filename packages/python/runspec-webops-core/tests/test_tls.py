"""Pure TLS reporting logic — no socket required."""

from datetime import datetime, timezone

import pytest

from runspec_webops_core import cert_status, chain_report, days_until, leaf_report
from runspec_webops_core.errors import ConnectError
from runspec_webops_core.tls import (
    EXPIRED,
    EXPIRING,
    OK,
    TlsPeer,
    _alt_names,
    _issuer_label,
    cert_info_from_ssl,
)

# A fixed "now" so day arithmetic is deterministic: 2026-01-01T00:00:00Z.
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()


def test_days_until_future_and_past() -> None:
    assert days_until("Jan 31 00:00:00 2026 GMT", NOW) == 30
    assert days_until("Dec  2 00:00:00 2025 GMT", NOW) == -30


def test_days_until_unparseable_raises() -> None:
    with pytest.raises(ValueError):
        days_until("not a date", NOW)


def test_cert_status_buckets() -> None:
    assert cert_status(90, 30) == OK
    assert cert_status(10, 30) == EXPIRING
    assert cert_status(0, 30) == EXPIRING
    assert cert_status(-1, 30) == EXPIRED


def test_alt_names_keeps_dns_only() -> None:
    san = (("DNS", "example.com"), ("DNS", "www.example.com"), ("IP Address", "10.0.0.1"))
    assert _alt_names(san) == ["example.com", "www.example.com"]
    assert _alt_names(None) == []


def test_issuer_label_prefers_cn_then_org() -> None:
    assert _issuer_label(((("commonName", "Some CA"),),)) == "Some CA"
    assert _issuer_label(((("organizationName", "Acme"),),)) == "Acme"


def _ssl_info(cn: str, issuer_cn: str, not_after: str) -> dict:
    return {
        "subject": ((("commonName", cn),),),
        "issuer": ((("commonName", issuer_cn),),),
        "notBefore": "Jan  1 00:00:00 2025 GMT",
        "notAfter": not_after,
        "subjectAltName": (("DNS", cn),),
        "serialNumber": "ABCDEF",
    }


def test_cert_info_self_signed_detection() -> None:
    self_signed = cert_info_from_ssl(_ssl_info("root", "root", "Jan  1 00:00:00 2030 GMT"), b"der")
    assert self_signed.self_signed is True
    leaf = cert_info_from_ssl(_ssl_info("leaf", "issuing-ca", "Jan  1 00:00:00 2030 GMT"), b"der")
    assert leaf.self_signed is False
    # Fingerprint is a colon-separated uppercase hex digest of the DER.
    assert leaf.fingerprint_sha256.count(":") == 31
    assert leaf.fingerprint_sha256.isupper()


def test_leaf_report_status_and_fields() -> None:
    leaf = cert_info_from_ssl(_ssl_info("example.com", "issuing-ca", "Jan 31 00:00:00 2026 GMT"), b"der")
    peer = TlsPeer(chain=[leaf], protocol="TLSv1.3", cipher_name="AES", cipher_version="TLSv1.3", authorized=False, authorization_error="self signed")
    rep = leaf_report(peer, warn_days=30, now=NOW)
    assert rep["subject"] == "example.com"
    assert rep["days_remaining"] == 30
    assert rep["status"] == EXPIRING
    assert rep["alt_names"] == ["example.com"]
    assert rep["authorized"] is False
    assert rep["authorization_error"] == "self signed"


def test_leaf_report_unparseable_date_is_expired() -> None:
    leaf = cert_info_from_ssl(_ssl_info("x", "y", "garbage"), b"der")
    peer = TlsPeer(chain=[leaf], protocol=None, cipher_name=None, cipher_version=None, authorized=True, authorization_error=None)
    rep = leaf_report(peer, warn_days=30, now=NOW)
    assert rep["status"] == EXPIRED
    assert rep["days_remaining"] == -1


def test_leaf_report_no_cert_raises() -> None:
    peer = TlsPeer(chain=[], protocol=None, cipher_name=None, cipher_version=None, authorized=False, authorization_error=None)
    with pytest.raises(ConnectError):
        leaf_report(peer, warn_days=30, now=NOW)


def test_chain_report_marks_root() -> None:
    leaf = cert_info_from_ssl(_ssl_info("leaf", "ca", "Jan 31 00:00:00 2026 GMT"), b"a")
    root = cert_info_from_ssl(_ssl_info("ca", "ca", "Jan  1 00:00:00 2030 GMT"), b"b")
    peer = TlsPeer(chain=[leaf, root], protocol="TLSv1.3", cipher_name="X", cipher_version="TLSv1.3", authorized=True, authorization_error=None)
    rep = chain_report(peer, now=NOW)
    assert [link["self_signed"] for link in rep] == [False, True]
    assert rep[0]["days_remaining"] == 30
