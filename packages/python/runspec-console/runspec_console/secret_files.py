"""Secret placeholders in a runnable's args — ``{{secret:<credential>}}``.

The agent (or an operator in Forms) writes a placeholder instead of a value::

    filesystem write-file --path /etc/alloy/config.alloy \\
        --content 'password = "{{secret:grafana-cloud}}"'

Before the run, the console finds the placeholders in the call's args, checks
each named credential may be written into files on that host, reads its secret
from the keychain and sends it in the run's environment — on stdin, in the env
frame, like every other credential — as ``RUNSPEC_SECRET_<NAME>`` (and
``RUNSPEC_SECRET_<NAME>__USERNAME`` for ``{{secret:<name>.username}}``). The
runnable (runspec-fs-core's ``write-file`` / ``edit-file``) fills them in on the
host just before it writes. The args, confirm card, run record and History only
ever hold the placeholder.

A credential may be used this way only when the operator ticked **"Agent may
write this into files"** on it (``write_to_files = true``) and the host is in its
``scope_hosts`` (empty = any host). ``<name>`` is the credential's id, or its
label written the way env vars are (``Grafana Cloud`` -> ``grafana-cloud``).

:func:`mask` replaces every saved secret in text going back to the model with
its placeholder, so reading the file back can't hand the value over.

The env var naming here must match ``runspec_fs_core.secrets.env_name``.

Pure (no keyring, no bridge): the bridge passes the credential entries and a
``get_secret`` callable in.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any

ENV_PREFIX = "RUNSPEC_SECRET_"
USERNAME_SUFFIX = "__USERNAME"

#: Shortest secret value used or masked; masking a shorter one would garble output.
MIN_SECRET_LEN = 4

PLACEHOLDER_RE = re.compile(
    r"\{\{secret:([A-Za-z0-9][A-Za-z0-9_.-]*?)(\.username)?\}\}"
)

#: How a credential is told it may be written into files.
FLAG = "write_to_files"


def norm(name: str) -> str:
    """``grafana-cloud`` / ``Grafana Cloud`` -> ``GRAFANA_CLOUD``."""
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def env_name(ref: str, field_: str = "secret") -> str:
    name = ENV_PREFIX + norm(ref)
    return name + USERNAME_SUFFIX if field_ == "username" else name


def placeholder(ref: str, field_: str = "secret") -> str:
    return "{{secret:" + ref + (".username" if field_ == "username" else "") + "}}"


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def has_placeholders(value: Any) -> bool:
    """True when any string in ``value`` (args dict, list, str) holds a placeholder."""
    return any(PLACEHOLDER_RE.search(s) for s in _strings(value))


def find_refs(value: Any) -> list[tuple[str, str]]:
    """The ``(name, field)`` pairs the strings in ``value`` refer to, in order,
    without duplicates; ``field`` is ``"secret"`` or ``"username"``."""
    out: list[tuple[str, str]] = []
    for s in _strings(value):
        for m in PLACEHOLDER_RE.finditer(s):
            ref = (m.group(1), "username" if m.group(2) else "secret")
            if ref not in out:
                out.append(ref)
    return out


def file_credentials(entries: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The credentials the operator allowed into files."""
    return [
        e for e in entries if e.get(FLAG) is True and str(e.get("kind")) != "ssh-key"
    ]


def find_credential(
    ref: str, entries: Iterable[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """The credential a placeholder names: exact id first, then id or label
    compared the env-var way (``Grafana Cloud`` == ``grafana-cloud``)."""
    entries = list(entries)
    for e in entries:
        if str(e.get("id", "")) == ref:
            return e
    key = norm(ref)
    if not key:
        return None
    hits = [
        e
        for e in entries
        if key in (norm(str(e.get("id", ""))), norm(str(e.get("label", ""))))
    ]
    return hits[0] if len(hits) == 1 else None


def _host_allowed(entry: Mapping[str, Any], host: str) -> bool:
    patterns = [str(p) for p in (entry.get("scope_hosts") or []) if str(p).strip()]
    return not patterns or any(fnmatch(host, p) for p in patterns)


@dataclass
class Resolved:
    """What a run's placeholders resolved to."""

    env: dict[str, str] = field(default_factory=dict)
    #: credential ids used (for the confirm card / audit — never values)
    ids: list[str] = field(default_factory=list)
    #: labels of the credentials used, in order
    labels: list[str] = field(default_factory=list)
    #: one plain-English line per refused placeholder; empty = all good
    errors: list[str] = field(default_factory=list)

    @property
    def error(self) -> str:
        return " ".join(self.errors)


def resolve(
    args: Any,
    host: str,
    entries: Iterable[Mapping[str, Any]],
    get_secret: Callable[[str], str | None],
) -> Resolved:
    """Resolve every placeholder in ``args`` for a run on ``host``."""
    entries = list(entries)
    res = Resolved()
    for ref, field_ in find_refs(args):
        entry = find_credential(ref, entries)
        shown = placeholder(ref, field_)
        if entry is None:
            allowed = ", ".join(
                placeholder(str(e.get("id"))) + f" ({e.get('label')})"
                for e in file_credentials(entries)
            )
            res.errors.append(
                f"{shown}: no saved credential named {ref!r}."
                + (
                    f" Credentials you may write into files: {allowed}."
                    if allowed
                    else " No credential is allowed into files yet."
                )
            )
            continue
        label = str(entry.get("label") or entry.get("id"))
        if entry.get(FLAG) is not True:
            res.errors.append(
                f"{shown}: the credential '{label}' isn't allowed into files. The operator can tick "
                "'Agent may write this into files' on it in Settings → Credentials."
            )
            continue
        if not _host_allowed(entry, host):
            res.errors.append(
                f"{shown}: the credential '{label}' isn't scoped to host '{host}'."
            )
            continue
        cid = str(entry.get("id", ""))
        if field_ == "username":
            value = str(entry.get("username") or "")
            if not value:
                res.errors.append(f"{shown}: the credential '{label}' has no username.")
                continue
        else:
            value = get_secret(cid) or ""
            if len(value) < MIN_SECRET_LEN:
                res.errors.append(
                    f"{shown}: the credential '{label}' has no secret saved (or it's too short to keep out of output)."
                )
                continue
        res.env[env_name(ref, field_)] = value
        if cid not in res.ids:
            res.ids.append(cid)
            res.labels.append(label)
    return res


def mask(text: str, secrets: Mapping[str, str]) -> str:
    """Replace every secret value in ``text`` with ``{{secret:<id>}}``.

    ``secrets`` maps credential id -> secret value. Longer values go first, and
    a value is also caught JSON-escaped (inside a printed JSON string)."""
    if not text or not secrets:
        return text
    import json

    for cid, value in sorted(secrets.items(), key=lambda kv: len(kv[1]), reverse=True):
        if len(value) < MIN_SECRET_LEN:
            continue
        ph = placeholder(cid)
        if value in text:
            text = text.replace(value, ph)
        escaped = json.dumps(value)[1:-1]
        if escaped != value and escaped in text:
            text = text.replace(escaped, ph)
    return text
