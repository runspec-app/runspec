"""TLS certificate + connection inspection — stdlib ``ssl`` / ``socket`` only.

The inspection connection deliberately disables verification (``CERT_NONE``): a
cert *checker* must still complete the handshake against an expired, self-signed
or otherwise untrusted endpoint in order to report on it. The trust result is
captured separately by a second, *verifying* connection and surfaced via
``authorized`` / ``authorization_error`` rather than raising.

The network I/O is confined to :func:`connect_peer`; the reporting functions
below are pure so they can be unit-tested without a socket.
"""

from __future__ import annotations

import hashlib
import ssl
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from runspec_webops_core.errors import ConnectError
from runspec_webops_core.proxy import open_socket, resolve_proxy

# Certificate freshness buckets.
OK = "ok"
EXPIRING = "expiring"
EXPIRED = "expired"

# ── data model ────────────────────────────────────────────────────────────────


@dataclass
class CertInfo:
    """One certificate's parsed fields plus its SHA-256 fingerprint."""

    subject: str
    issuer: str
    valid_from: str
    valid_to: str
    alt_names: list[str] = field(default_factory=list)
    serial_number: str = ""
    fingerprint_sha256: str = ""
    self_signed: bool = False


@dataclass
class TlsPeer:
    """The result of a single TLS inspection connection."""

    chain: list[CertInfo]
    protocol: str | None
    cipher_name: str | None
    cipher_version: str | None
    authorized: bool
    authorization_error: str | None

    @property
    def leaf(self) -> CertInfo | None:
        return self.chain[0] if self.chain else None


# ── pure reporting ────────────────────────────────────────────────────────────


def days_until(not_after: str, now: float | None = None) -> int:
    """Whole days until an X.509 ``notAfter`` string (``'Jun  4 23:59:59 2026 GMT'``).

    Negative when already expired. Raises ``ValueError`` when unparseable.
    """
    expiry = ssl.cert_time_to_seconds(not_after)
    if now is None:
        now = datetime.now(timezone.utc).timestamp()
    return int((expiry - now) // 86_400)


def cert_status(days_remaining: int, warn_days: int) -> str:
    """Bucket a certificate by days-to-expiry. Pure."""
    if days_remaining < 0:
        return EXPIRED
    if days_remaining <= warn_days:
        return EXPIRING
    return OK


def _rdn_get(name: tuple, key: str) -> str:
    """Pull a value out of an ``ssl`` RDN structure (tuple of relative-DN sets)."""
    for rdn in name or ():
        for pair in rdn:
            if len(pair) == 2 and pair[0] == key:
                return str(pair[1])
    return ""


def _common_name(name: tuple) -> str:
    return _rdn_get(name, "commonName")


def _issuer_label(name: tuple) -> str:
    return _rdn_get(name, "commonName") or _rdn_get(name, "organizationName")


def _alt_names(san: tuple | None) -> list[str]:
    """Flatten an ``ssl`` ``subjectAltName`` tuple into a list of DNS names."""
    return [str(value) for (kind, value) in (san or ()) if kind == "DNS"]


def cert_info_from_ssl(info: dict, der: bytes) -> CertInfo:
    """Build a :class:`CertInfo` from an ``ssl`` cert dict plus the DER bytes.

    ``info`` is the shape returned by ``getpeercert()`` / ``Certificate.get_info()``.
    ``self_signed`` is decided structurally (subject == issuer) rather than by a
    trust check, so it holds for an inspection-only connection.
    """
    subject = _common_name(info.get("subject", ()))
    issuer = _issuer_label(info.get("issuer", ()))
    return CertInfo(
        subject=subject,
        issuer=issuer,
        valid_from=str(info.get("notBefore", "")),
        valid_to=str(info.get("notAfter", "")),
        alt_names=_alt_names(info.get("subjectAltName")),
        serial_number=str(info.get("serialNumber", "")),
        fingerprint_sha256=_fingerprint(der),
        self_signed=info.get("subject", ()) == info.get("issuer", ()),
    )


def _fingerprint(der: bytes) -> str:
    """Colon-separated uppercase SHA-256 of a DER certificate (OpenSSL style)."""
    hexed = hashlib.sha256(der).hexdigest().upper()
    return ":".join(hexed[i : i + 2] for i in range(0, len(hexed), 2))


def leaf_report(peer: TlsPeer, warn_days: int, now: float | None = None) -> dict:
    """The cert-check view: leaf cert fields + freshness status + trust result."""
    leaf = peer.leaf
    if leaf is None:
        raise ConnectError("no peer certificate was presented")
    try:
        days = days_until(leaf.valid_to, now)
        status = cert_status(days, warn_days)
    except ValueError:
        days, status = -1, EXPIRED
    return {
        "subject": leaf.subject,
        "issuer": leaf.issuer,
        "valid_from": leaf.valid_from,
        "valid_to": leaf.valid_to,
        "days_remaining": days,
        "status": status,
        "alt_names": leaf.alt_names,
        "serial_number": leaf.serial_number,
        "fingerprint_sha256": leaf.fingerprint_sha256,
        "authorized": peer.authorized,
        "authorization_error": peer.authorization_error,
    }


def chain_report(peer: TlsPeer, now: float | None = None) -> list[dict]:
    """The cert-chain view: one entry per cert, leaf → … → root."""
    out: list[dict] = []
    for link in peer.chain:
        try:
            days = days_until(link.valid_to, now)
        except ValueError:
            days = -1
        out.append(
            {
                "subject": link.subject,
                "issuer": link.issuer,
                "valid_to": link.valid_to,
                "days_remaining": days,
                "self_signed": link.self_signed,
            }
        )
    return out


# ── network I/O ───────────────────────────────────────────────────────────────


def client_ssl_context(insecure: bool = False, ca_bundle: str | None = None) -> ssl.SSLContext:
    """Build a verifying client SSL context, optionally trusting a custom CA bundle.

    ``ca_bundle`` is a path to a PEM file of extra CA certificate(s) to verify
    against — for a private/internal CA or a TLS-intercepting proxy. ``insecure``
    turns verification off entirely (``CERT_NONE``), which overrides ``ca_bundle``.
    Raises :class:`ConnectError` when the bundle path can't be loaded.
    """
    try:
        ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    except OSError as e:
        raise ConnectError(f"could not load CA bundle {ca_bundle!r}: {e}") from e
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _peer_chain(ssock: ssl.SSLSocket) -> list[CertInfo]:
    """Return the server-presented certificate chain (leaf first).

    Prefers the public ``SSLSocket.get_unverified_chain()`` (Python 3.13+),
    falls back to the underlying ``_sslobj`` method (3.10–3.12), and finally to
    the leaf-only certificate via ``getpeercert`` when neither is available.
    """
    getter = getattr(ssock, "get_unverified_chain", None)
    if getter is None:
        sslobj = getattr(ssock, "_sslobj", None)
        getter = getattr(sslobj, "get_unverified_chain", None) if sslobj is not None else None

    if getter is not None:
        try:
            raw = getter()
        except Exception:
            raw = None
        if raw:
            out: list[CertInfo] = []
            for cert in raw:
                der = ssl.PEM_cert_to_DER_cert(cert.public_bytes())
                out.append(cert_info_from_ssl(cert.get_info(), der))
            return out

    # Fallback: only the leaf is recoverable on this interpreter.
    leaf_der = ssock.getpeercert(binary_form=True)
    if not leaf_der:
        return []
    return [cert_info_from_ssl(_decode_der(leaf_der), leaf_der)]


def _decode_der(der: bytes) -> dict:
    """Parse a DER certificate into the ``getpeercert`` dict via the stdlib decoder."""
    pem = ssl.DER_cert_to_PEM_cert(der)
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
        fh.write(pem)
        path = fh.name
    try:
        import _ssl  # noqa: PLC0415 — stdlib helper, imported lazily for the fallback path

        return dict(_ssl._test_decode_cert(path))  # type: ignore[attr-defined]
    finally:
        Path(path).unlink(missing_ok=True)


def _verify_trust(host: str, port: int, servername: str, timeout: float, proxy: str | None = None, ca_bundle: str | None = None) -> tuple[bool, str | None]:
    """Second connection with full verification — captures the trust verdict.

    ``ca_bundle`` adds a custom CA to the trust store (private CA / TLS proxy).
    """
    ctx = client_ssl_context(ca_bundle=ca_bundle)
    try:
        with open_socket(host, port, timeout, proxy) as sock, ctx.wrap_socket(sock, server_hostname=servername):
            return True, None
    except ssl.SSLCertVerificationError as e:
        return False, getattr(e, "verify_message", None) or str(e)
    except ssl.SSLError as e:
        return False, str(e)
    except (OSError, TimeoutError) as e:
        # The unverified connection already succeeded, so a failure here is the
        # trust handshake itself (e.g. a TLS alert), not unreachability.
        return False, str(e)


def connect_peer(host: str, port: int = 443, timeout: float = 10.0, servername: str | None = None, proxy: str | None = None, ca_bundle: str | None = None) -> TlsPeer:
    """Open a TLS connection and capture the cert chain, protocol, cipher, trust.

    ``proxy`` overrides proxy resolution (``"direct"`` forces none; ``None``
    auto-discovers from the environment and, on Windows, the system config). When
    a proxy applies the connection is CONNECT-tunnelled, so the certificate
    inspected is the real host's, not the proxy's. ``ca_bundle`` is a PEM path
    used only for the trust verdict — the inspection connection is unverified
    regardless, so the chain is still reported for an untrusted endpoint.

    Raises :class:`ConnectError` only when the endpoint is unreachable — an
    untrusted / expired certificate is reported, not raised.
    """
    sni = servername or host
    resolved = resolve_proxy(f"https://{host}:{port}", proxy)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with open_socket(host, port, timeout, resolved) as sock, ctx.wrap_socket(sock, server_hostname=sni) as ssock:
            protocol = ssock.version()
            cipher = ssock.cipher()
            chain = _peer_chain(ssock)
    except (OSError, TimeoutError, ssl.SSLError) as e:
        raise ConnectError(f"connection to {host}:{port} failed: {e}") from e

    authorized, auth_error = _verify_trust(host, port, sni, timeout, resolved, ca_bundle)
    return TlsPeer(
        chain=chain,
        protocol=protocol,
        cipher_name=cipher[0] if cipher else None,
        cipher_version=cipher[1] if cipher else None,
        authorized=authorized,
        authorization_error=auth_error,
    )
