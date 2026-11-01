"""putty_sync — keep PuTTY's saved sessions in step with the console's hosts.

With ``[putty] sync = true`` the console writes one PuTTY saved session per
remote host (``HKCU\\Software\\SimonTatham\\PuTTY\\Sessions``), so a plain
PuTTY opened from the Start menu lists every console host with its host name,
port, user, ``.ppk`` key, keepalive, HTTP proxy or jump host, and terminal
look already filled in. Sessions are named ``group/host`` (just ``host`` when
the host has no group), so PuTTY's flat, sorted list reads by group.

```toml
[putty]
sync = true          # keep one PuTTY saved session per console host
profiles = []        # only hosts in these profiles; [] = every host
keepalive = 30       # seconds between keepalives; 0 = off
prefix = ""          # put in front of every session name, e.g. "rs/"
```

Each session the console writes carries a marker value (:data:`MARKER`, set
to the console's app name). A sync only ever overwrites or deletes sessions
with that marker, so a session you made yourself under the same name is left
alone and reported as skipped.

This module is pure (no winreg): the name escaping, host selection, value
building and the write/delete plan are testable everywhere. The registry I/O
is :class:`RegistrySessions`; the bridge (``bridge_putty``) puts them together.
"""

from __future__ import annotations

import re
from typing import Any

# The registry value marking a session the console wrote. PuTTY ignores values
# it doesn't know, and keeps them when you re-save the session from its GUI.
MARKER = "RunspecConsoleSync"

DEFAULT_KEEPALIVE = 30
SESSIONS_KEY = r"Software\SimonTatham\PuTTY\Sessions"

_HEX = "0123456789ABCDEF"


def read_putty_sync(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise the ``[putty]`` config section (absent ⇒ sync off)."""
    raw = (cfg or {}).get("putty")
    p = raw if isinstance(raw, dict) else {}
    profiles = p.get("profiles")
    names = (
        [str(x).strip() for x in profiles if str(x).strip()]
        if isinstance(profiles, list)
        else []
    )
    try:
        keepalive = int(p.get("keepalive", DEFAULT_KEEPALIVE))
    except (TypeError, ValueError):
        keepalive = DEFAULT_KEEPALIVE
    prefix = str(p.get("prefix") or "").strip()
    return {
        "sync": bool(p.get("sync", False)),
        "profiles": names,
        "keepalive": max(0, min(keepalive, 3600)),
        "prefix": prefix,
    }


def munge(name: str) -> str:
    """Escape a session name the way PuTTY stores it as a registry key.

    Same rule as PuTTY's ``mungestr``: space, ``\\``, ``*``, ``?``, ``%``,
    control and non-ASCII bytes, and a leading ``.`` become ``%XX``."""
    out: list[str] = []
    for i, byte in enumerate(name.encode("utf-8")):
        ch = chr(byte)
        if ch in " \\*?%" or byte < 0x20 or byte > 0x7E or (ch == "." and i == 0):
            out.append("%" + _HEX[byte >> 4] + _HEX[byte & 15])
        else:
            out.append(ch)
    return "".join(out)


def unmunge(key: str) -> str:
    """The session name PuTTY shows for a registry key (inverse of :func:`munge`)."""
    raw = bytearray()
    i = 0
    while i < len(key):
        if key[i] == "%" and re.fullmatch("[0-9A-Fa-f]{2}", key[i + 1 : i + 3]):
            raw.append(int(key[i + 1 : i + 3], 16))
            i += 3
        else:
            raw.extend(key[i].encode("utf-8"))
            i += 1
    return raw.decode("utf-8", errors="replace")


def session_name(entry: dict[str, Any], prefix: str = "") -> str:
    """The PuTTY session name for a host: ``[prefix]group/host`` or ``[prefix]host``."""
    name = str(entry.get("name") or "").strip()
    group = str(entry.get("group") or "").strip()
    return f"{prefix}{group}/{name}" if group else f"{prefix}{name}"


def select_hosts(
    hosts: list[dict[str, Any]] | None, profiles: list[str] | None = None
) -> list[dict[str, Any]]:
    """The remote hosts to sync: every host with an ssh target, narrowed to
    those tagged with any of ``profiles`` when given. Sorted by session name."""
    wanted = {p.lower() for p in (profiles or [])}
    out = []
    for h in hosts or []:
        name = str(h.get("name") or "").strip()
        if not name or name == "local" or not str(h.get("ssh") or "").strip():
            continue
        if wanted:
            tags = {str(t).strip().lower() for t in (h.get("profiles") or [])}
            if not tags & wanted:
                continue
        out.append(h)
    return sorted(out, key=lambda h: session_name(h).lower())


def keepalive_values(seconds: int) -> dict[str, int]:
    """PuTTY keepalive values: an SSH-level ping every ``seconds`` plus TCP
    keepalives, so an idle session isn't dropped by a firewall's idle timer.
    ``PingInterval`` (minutes) is the pre-0.53 spelling, zeroed so an old value
    can't override the seconds one."""
    return {
        "PingInterval": 0,
        "PingIntervalSecs": max(0, int(seconds)),
        "TCPKeepalives": 1 if seconds else 0,
    }


NO_PROXY: dict[str, int | str] = {
    "ProxyMethod": 0,
    "ProxyHost": "",
    "ProxyTelnetCommand": "",
}


def http_proxy_values(proxy: str) -> dict[str, int | str] | None:
    """Saved-session values for an HTTP CONNECT proxy (``host[:port]`` or a
    URL), or None when ``proxy`` has no host."""
    from urllib.parse import urlparse

    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    if not parsed.hostname:
        return None
    return {
        "ProxyMethod": 3,  # 3 = HTTP
        "ProxyHost": parsed.hostname,
        "ProxyPort": parsed.port or 8080,
        "ProxyDNS": 1,  # 1 = Auto
        "ProxyLocalhost": 0,
        "ProxyUsername": "",
        "ProxyPassword": "",
        "ProxyTelnetCommand": "",
    }


def jump_proxy_values(proxy_command: str) -> dict[str, int | str]:
    """Saved-session values for a jump host reached with a local proxy
    command (``plink -nc``) — PuTTY's "Local" proxy type, which every PuTTY
    since 0.58 has (the SSH proxy type only arrived in 0.77)."""
    return {
        "ProxyMethod": 5,  # 5 = local command
        "ProxyTelnetCommand": proxy_command,
        "ProxyHost": "",
        "ProxyDNS": 2,  # 2 = Yes: the jump host resolves the target's name
        "ProxyLocalhost": 0,
        "ProxyUsername": "",
        "ProxyPassword": "",
    }


def host_values(
    *,
    hostname: str,
    port: int | None,
    user: str,
    key_file: str,
    keepalive: int,
    proxy: dict[str, int | str] | None,
    look: dict[str, int | str] | None,
    owner: str,
) -> dict[str, int | str]:
    """Every registry value the console sets on one host's saved session."""
    return {
        **(look or {}),
        **keepalive_values(keepalive),
        **(proxy or NO_PROXY),
        "HostName": hostname,
        "PortNumber": int(port or 22),
        "Protocol": "ssh",
        "UserName": user,
        "PublicKeyFile": key_file,
        MARKER: owner,
    }


def plan(
    desired: dict[str, Any], existing: dict[str, str | None], owner: str
) -> dict[str, list[str]]:
    """What a sync does, by session name.

    ``desired`` is name → values; ``existing`` is every saved session's name →
    its :data:`MARKER` value (None when it has none). Returns ``write`` (ours or
    new), ``delete`` (ours, no longer wanted) and ``conflict`` (wanted, but the
    name belongs to a session somebody else made)."""
    lowered = {k.lower(): v for k, v in existing.items()}
    write, conflict = [], []
    for name in desired:
        marker = lowered.get(name.lower(), "")
        if name.lower() in lowered and marker != owner:
            conflict.append(name)
        else:
            write.append(name)
    wanted = {n.lower() for n in desired}
    delete = [
        name
        for name, marker in existing.items()
        if marker == owner and name.lower() not in wanted
    ]
    return {"write": write, "delete": sorted(delete), "conflict": conflict}


class RegistrySessions:
    """PuTTY's saved sessions in the Windows registry (HKCU). Names here are
    the names PuTTY shows; munging to registry keys happens inside."""

    def __init__(self) -> None:
        import winreg  # Windows-only — raises ImportError elsewhere

        self._w = winreg

    def list(self) -> dict[str, str | None]:
        """Every saved session's name → its :data:`MARKER` value (or None)."""
        w = self._w
        out: dict[str, str | None] = {}
        try:
            root = w.OpenKey(w.HKEY_CURRENT_USER, SESSIONS_KEY)
        except OSError:
            return out  # PuTTY has never saved a session
        with root:
            i = 0
            while True:
                try:
                    key = w.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                marker: str | None = None
                try:
                    with w.OpenKey(root, key) as sk:
                        marker = str(w.QueryValueEx(sk, MARKER)[0])
                except OSError:
                    pass
                out[unmunge(key)] = marker
        return out

    def write(self, name: str, values: dict[str, int | str]) -> None:
        from .bridge_utils import _write_putty_session

        _write_putty_session(munge(name), values)

    def delete(self, name: str) -> None:
        self._w.DeleteKey(self._w.HKEY_CURRENT_USER, rf"{SESSIONS_KEY}\{munge(name)}")
