"""Bridge glue for ``{{secret:<credential>}}`` placeholders (see ``secret_files``).

Module functions taking the bridge rather than mixin methods, so the run paths
(agent, Forms/MCP/rules, the turn loop) can call them on the lightweight bridge
stubs the tests build, which borrow only the methods they exercise.
"""

from __future__ import annotations

import time
from typing import Any

from . import credentials as cred
from . import secret_files
from .bridge_const import logger

_MASK_TTL = 60.0


def secret_file_env(bridge: Any, host: str, args: Any) -> secret_files.Resolved | None:
    """Resolve the placeholders in a run's args. ``None`` when there are none.
    Otherwise a :class:`secret_files.Resolved` whose ``env`` (``RUNSPEC_SECRET_*``)
    joins the run's credential env — it reaches the host on stdin, never argv —
    or whose ``errors`` say why a placeholder was refused (credential not allowed
    into files, not scoped to this host, unknown). The runnable fills the values
    in on the host; args and History keep the placeholder."""
    if not secret_files.has_placeholders(args):
        return None
    reload = getattr(bridge, "_reload_credentials", None)
    if callable(reload):
        reload()
    entries = getattr(bridge, "_credentials", None) or []
    return secret_files.resolve(args, host, entries, cred.get_secret)


def mask_known_secrets(bridge: Any, text: str) -> str:
    """Replace every saved credential's secret in ``text`` with
    ``{{secret:<id>}}`` — applied to tool output before the model (or an MCP
    client) sees it, so a secret written into a file can't come back by reading
    the file. Secrets are read from the keychain at most once a minute, or when
    the credential list changes. Never raises."""
    if not text:
        return text
    try:
        secrets = _mask_values(bridge)
    except Exception:  # noqa: BLE001 — masking must never break a tool result
        logger.debug("secret masking unavailable", exc_info=True)
        return text
    return secret_files.mask(text, secrets)


def _mask_values(bridge: Any) -> dict[str, str]:
    ids = tuple(
        sorted(
            str(c.get("id"))
            for c in (getattr(bridge, "_credentials", None) or [])
            if c.get("id") and str(c.get("kind")) != "ssh-key"
        )
    )
    if not ids:
        return {}
    cache = getattr(bridge, "_secret_mask_cache", None)
    now = time.monotonic()
    if cache and cache[0] == ids and now - cache[1] < _MASK_TTL:
        return dict(cache[2])
    values: dict[str, str] = {}
    for cid in ids:
        try:
            v = cred.get_secret(cid)
        except Exception:  # noqa: BLE001
            v = None
        if v:
            values[cid] = str(v)
    try:
        bridge._secret_mask_cache = (ids, now, values)
    except AttributeError:
        pass
    return values
