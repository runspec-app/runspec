"""DNS resolution — forward (A/AAAA) and reverse (PTR) via stdlib ``socket``."""

from __future__ import annotations

import socket

from runspec_webops_core.errors import ResolveError


def _is_ip(value: str) -> bool:
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, value)
            return True
        except OSError:
            continue
    return False


def _reverse(ip: str) -> list[str]:
    """PTR names for an address. Returns ``[]`` when there is no reverse record."""
    try:
        name, aliases, _ = socket.gethostbyaddr(ip)
    except OSError:
        return []
    return [name, *aliases]


def resolve(host: str, want_reverse: bool = False) -> dict:
    """Resolve ``host``.

    A bare IP yields its PTR records. A name yields its A/AAAA addresses and
    canonical name; with ``want_reverse`` each address is also reverse-resolved.
    Raises :class:`ResolveError` when the name cannot be resolved.
    """
    if _is_ip(host):
        return {"host": host, "is_ip": True, "reverse": {host: _reverse(host)}}

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP, flags=socket.AI_CANONNAME)
    except socket.gaierror as e:
        raise ResolveError(f"could not resolve {host!r}: {e}") from e

    addresses: list[dict] = []
    seen: set[tuple[int, str]] = set()
    canonical = ""
    for family, _type, _proto, canonname, sockaddr in infos:
        if canonname:
            canonical = canonname
        ip = str(sockaddr[0])
        key = (int(family), ip)
        if key in seen:
            continue
        seen.add(key)
        addresses.append({"ip": ip, "family": "ipv6" if family == socket.AF_INET6 else "ipv4"})

    result: dict = {
        "host": host,
        "is_ip": False,
        "addresses": addresses,
        "canonical_name": canonical or None,
    }
    if want_reverse:
        result["reverse"] = {entry["ip"]: _reverse(entry["ip"]) for entry in addresses}
    return result
