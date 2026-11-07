"""Reading OpenPGP key files without gpg.

The fixtures are throwaway ed25519 test keys made with gpg; their fingerprints
below are what ``gpg --show-keys`` prints for them.
"""

from pathlib import Path

import pytest

from runspec_linux_core import pgpkey

DATA = Path(__file__).parent / "data"
ONE = "0B8A8FE4594E7466A8EF613766F1EE2CD85AD90C"
TWO = "AB050B78F2DD817746A942A10626B9874383DE19"


def test_reads_fingerprint_and_creation() -> None:
    keys = pgpkey.read_keys((DATA / "test-key-one.asc").read_bytes())
    assert [k.fingerprint for k in keys] == [ONE]
    assert keys[0].version == 4
    assert keys[0].created == 1794067130
    assert keys[0].rpm_name == "gpg-pubkey-d85ad90c"


def test_reads_every_key_in_a_file() -> None:
    keys = pgpkey.read_keys((DATA / "test-keys-two.asc").read_bytes())
    assert [k.fingerprint for k in keys] == [ONE, TWO]


def test_binary_and_rearmored_read_the_same() -> None:
    armored = (DATA / "test-keys-two.asc").read_bytes()
    binary = pgpkey.dearmor(armored)
    assert not pgpkey.is_armored(binary)
    assert pgpkey.read_keys(binary) == pgpkey.read_keys(armored)
    assert pgpkey.read_keys(pgpkey.armor(binary)) == pgpkey.read_keys(armored)


def test_each_key_keeps_its_own_packets() -> None:
    keys = pgpkey.read_keys((DATA / "test-keys-two.asc").read_bytes())
    assert b"".join(k.packets for k in keys) == pgpkey.dearmor((DATA / "test-keys-two.asc").read_bytes())
    assert [pgpkey.read_keys(pgpkey.armor(k.packets))[0].fingerprint for k in keys] == [ONE, TWO]


def test_several_armor_blocks_in_one_file() -> None:
    one = (DATA / "test-key-one.asc").read_bytes()
    keys = pgpkey.read_keys(one + b"\n" + pgpkey.armor(pgpkey.read_keys((DATA / "test-keys-two.asc").read_bytes())[1].packets))
    assert [k.fingerprint for k in keys] == [ONE, TWO]


def test_armor_checksum_is_crc24() -> None:
    # RFC 4880 CRC-24 of "123456789" is 0x21CF02.
    assert pgpkey._crc24(b"123456789") == 0x21CF02


@pytest.mark.parametrize(
    "data",
    [b"", b"hello, not a key", b"<html>404</html>", b"-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n!!!\n-----END PGP PUBLIC KEY BLOCK-----\n"],
)
def test_rejects_things_that_are_not_keys(data: bytes) -> None:
    with pytest.raises(pgpkey.KeyFormatError):
        pgpkey.read_keys(data)


def test_rejects_truncated_key() -> None:
    binary = pgpkey.dearmor((DATA / "test-key-one.asc").read_bytes())
    with pytest.raises(pgpkey.KeyFormatError):
        pgpkey.read_keys(binary[:-10])


def test_rejects_secret_key_packet() -> None:
    # Old-format header, tag 5 (secret key), one-byte length.
    with pytest.raises(pgpkey.KeyFormatError, match="SECRET"):
        pgpkey.read_keys(bytes([0x80 | (5 << 2), 3, 4, 0, 0]))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0b8a 8fe4 594e 7466 a8ef  6137 66f1 ee2c d85a d90c", ONE),
        ("0x" + ONE, ONE),
        ("0B:8A:8F:E4:59:4E:74:66:A8:EF:61:37:66:F1:EE:2C:D8:5A:D9:0C", ONE),
    ],
)
def test_normalize_fingerprint(value: str, expected: str) -> None:
    assert pgpkey.normalize_fingerprint(value) == expected


@pytest.mark.parametrize("value", ["D85AD90C", "66F1EE2CD85AD90C", "not-hex" * 6])
def test_normalize_fingerprint_needs_the_full_fingerprint(value: str) -> None:
    with pytest.raises(ValueError):
        pgpkey.normalize_fingerprint(value)
