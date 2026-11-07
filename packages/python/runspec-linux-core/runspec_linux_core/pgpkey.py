"""Read OpenPGP public key files without gpg: armor, de-armor, fingerprints.

``package-repo add`` needs the fingerprints of a downloaded repository key so it
can show them and refuse a key that isn't pinned. ``gpg`` is often missing on a
minimal Debian host (Grafana's own install guide says to install it first), so
this reads the packets directly. Only the public-key and public-subkey packets
are looked at; nothing is verified here — apt / rpm do the signature checking.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

_BEGIN = "-----BEGIN PGP PUBLIC KEY BLOCK-----"
_END = "-----END PGP PUBLIC KEY BLOCK-----"

_TAG_PUBLIC_KEY = 6
_TAG_PUBLIC_SUBKEY = 14


class KeyFormatError(ValueError):
    """The data is not an OpenPGP public key (or is damaged)."""


@dataclass(frozen=True)
class PublicKey:
    """One primary key from a key file, with its subkeys."""

    fingerprint: str  # upper-case hex, no spaces
    version: int
    created: int  # Unix seconds
    subkeys: tuple[str, ...]
    packets: bytes = b""  # this key's own packets (key, user ids, signatures, subkeys)

    @property
    def rpm_name(self) -> str:
        """The name rpm gives this key once imported (``gpg-pubkey-<id8>``), v4 keys only."""
        return "gpg-pubkey-" + self.fingerprint[-8:].lower()


def is_armored(data: bytes) -> bool:
    return _BEGIN.encode() in data


def dearmor(data: bytes) -> bytes:
    """Return the binary packets of an armored key file (every block, in order).

    Binary input is returned unchanged.
    """
    if not is_armored(data):
        if data and data[0] & 0x80:
            return data
        raise KeyFormatError("not an OpenPGP public key (no 'BEGIN PGP PUBLIC KEY BLOCK' and not binary)")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise KeyFormatError("armored key file is not plain ASCII") from exc
    out = b""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() != _BEGIN:
            i += 1
            continue
        i += 1
        block: list[str] = []
        while i < len(lines) and lines[i].strip() != _END:
            block.append(lines[i].strip())
            i += 1
        if i >= len(lines):
            raise KeyFormatError("armored key block has no END line")
        i += 1
        # Armor headers ("Version: ...", "Comment: ...") end at the first blank line.
        if "" in block:
            block = block[block.index("") + 1 :]
        body = [line for line in block if line and not (line.startswith("=") and len(line) == 5)]
        try:
            out += base64.b64decode("".join(body), validate=True)
        except ValueError as exc:
            raise KeyFormatError("armored key block is not valid base64") from exc
    if not out:
        raise KeyFormatError("armored key file holds no key data")
    return out


def _crc24(data: bytes) -> int:
    crc = 0xB704CE
    for byte in data:
        crc ^= byte << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
    return crc & 0xFFFFFF


def armor(packets: bytes) -> bytes:
    """Wrap binary key packets in one ASCII-armored public key block."""
    b64 = base64.b64encode(packets).decode("ascii")
    body = "\n".join(b64[i : i + 64] for i in range(0, len(b64), 64))
    crc = base64.b64encode(_crc24(packets).to_bytes(3, "big")).decode("ascii")
    return f"{_BEGIN}\n\n{body}\n={crc}\n{_END}\n".encode("ascii")


def _packets(data: bytes):
    """Yield ``(tag, body, raw)`` for each packet; ``raw`` includes the header."""
    i = 0
    n = len(data)
    while i < n:
        start = i
        ctb = data[i]
        i += 1
        if not ctb & 0x80:
            raise KeyFormatError(f"bad packet header at byte {i - 1}")
        if ctb & 0x40:  # new format
            tag = ctb & 0x3F
            if i >= n:
                raise KeyFormatError("truncated packet length")
            first = data[i]
            i += 1
            if first < 192:
                length = first
            elif first < 224:
                if i >= n:
                    raise KeyFormatError("truncated packet length")
                length = ((first - 192) << 8) + data[i] + 192
                i += 1
            elif first == 255:
                length = int.from_bytes(data[i : i + 4], "big")
                i += 4
            else:
                raise KeyFormatError("partial-length packets don't appear in key files")
        else:  # old format
            tag = (ctb >> 2) & 0x0F
            kind = ctb & 0x03
            if kind == 3:
                length = n - i
            else:
                size = (1, 2, 4)[kind]
                length = int.from_bytes(data[i : i + size], "big")
                i += size
        body = data[i : i + length]
        if len(body) != length:
            raise KeyFormatError("truncated packet")
        i += length
        yield tag, body, data[start:i]


def _fingerprint(body: bytes) -> str:
    version = body[0] if body else 0
    if version == 4:
        digest = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body).hexdigest()  # noqa: S324 - OpenPGP v4 fingerprint
    elif version == 5:
        digest = hashlib.sha256(b"\x9a" + len(body).to_bytes(4, "big") + body).hexdigest()
    elif version == 6:
        digest = hashlib.sha256(b"\x9b" + len(body).to_bytes(4, "big") + body).hexdigest()
    else:
        raise KeyFormatError(f"OpenPGP key version {version} is not supported (v4, v5 and v6 are)")
    return digest.upper()


def read_keys(data: bytes) -> list[PublicKey]:
    """Return the primary keys (with subkey fingerprints) in a key file, armored or binary."""
    packets = dearmor(data)
    keys: list[PublicKey] = []
    current: dict | None = None

    def close() -> None:
        if current:
            keys.append(PublicKey(**{**current, "subkeys": tuple(current["subkeys"])}))

    for tag, body, raw in _packets(packets):
        if tag in (5, 7):
            raise KeyFormatError("this is a SECRET key — use the public key file")
        if tag == _TAG_PUBLIC_KEY:
            close()
            if len(body) < 5:
                raise KeyFormatError("public key packet too short")
            current = {
                "fingerprint": _fingerprint(body),
                "version": body[0],
                "created": int.from_bytes(body[1:5], "big"),
                "subkeys": [],
                "packets": raw,
            }
            continue
        if current is None:
            raise KeyFormatError("key file doesn't start with a public key")
        if tag == _TAG_PUBLIC_SUBKEY:
            current["subkeys"].append(_fingerprint(body))
        current["packets"] += raw
    close()
    if not keys:
        raise KeyFormatError("no public key in the key file")
    return keys


def normalize_fingerprint(value: str) -> str:
    """Upper-case hex with spaces/colons removed; must be a full v4 (40) or v5/v6 (64) fingerprint."""
    fpr = value.replace(" ", "").replace(":", "").upper()
    if fpr.startswith("0X"):
        fpr = fpr[2:]
    if len(fpr) not in (40, 64) or any(c not in "0123456789ABCDEF" for c in fpr):
        raise ValueError(f"not a full key fingerprint (40 or 64 hex digits): {value!r}")
    return fpr
