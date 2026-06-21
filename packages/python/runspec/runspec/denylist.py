"""denylist.py — venv-root kill switch for runnables.

Every runnable funnels through ``runspec.parse()``, so reading a single
``{sys.prefix}/runspec_denylist.toml`` there gives one uniform place to refuse
a runnable — by name, by effective username, by hostname, or any combination
(an omitted dimension means "all"). This mirrors the ``run_as`` identity gate
(``become.py``), which already runs on every ``parse()`` from the same
venv-root convention used by ``.runspec_env`` (``{sys.prefix}``).

The file is a *separate* format from ``runspec.toml`` — it is not part of the
spec a runnable author writes, and there is deliberately no per-runnable
opt-out: the denylist is authoritative so an operator's kill switch cannot be
disarmed by the runnable itself.

The reserved name ``runspec`` (the core CLI dogfooding its own spec) is never
denied, so ``runspec deny clear`` / ``runspec serve`` always work — the
guaranteed break-glass / re-enable path on any host.

File format::

    [denylist]
    mode = "enforce"          # "enforce" (default) | "warn" | "off"
    # disabled = true         # fast path: deny everything

    [[denylist.rule]]
    runnables = ["*"]         # omitted => ["*"] (all runnables)
    users = ["svc-batch"]     # omitted => ["*"] (all users)
    hosts = ["prod-*"]        # omitted => ["*"] (all hosts)
    reason = "change freeze"  # surfaced in the refusal message

Set the file path for tests via the ``RUNSPEC_DENYLIST_FILE`` env var (mirrors
``RUNSPEC_ENV_FILE``).
"""

from __future__ import annotations

import fnmatch
import os
import socket
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

DENYLIST_FILENAME = "runspec_denylist.toml"

# The core CLI parses its own runspec.toml (script_name "runspec") for --help and
# subcommands; it is the control plane (deny / serve / local) and must stay
# reachable even under a deny-everything rule, so it is never gated.
_SELF_NAME = "runspec"


def resolve_denylist_path() -> Path:
    """Return the denylist path: ``RUNSPEC_DENYLIST_FILE`` override, else
    ``{sys.prefix}/runspec_denylist.toml`` (one per virtual environment)."""
    if override := os.environ.get("RUNSPEC_DENYLIST_FILE"):
        return Path(override)
    return Path(sys.prefix) / DENYLIST_FILENAME


def load_denylist(path: Path | None = None) -> dict[str, Any] | None:
    """Return the ``[denylist]`` table, or ``None`` when there is nothing to
    enforce (file absent, unreadable, malformed, or no ``[denylist]`` table)."""
    if path is None:
        path = resolve_denylist_path()
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return None
    section = data.get("denylist")
    return section if isinstance(section, dict) else None


def current_identity() -> tuple[str, str]:
    """Return ``(effective_username, hostname)``.

    The *effective* user is the post-sudo identity the runnable actually runs
    as — escalation (``runspec serve`` / SSH ``sudo -u``) happens before the
    process starts, so this is the security-relevant account. Hostname comes
    from ``socket.gethostname()`` — the same source the ``run_as`` gate uses.
    """
    return _effective_username(), socket.gethostname()


def _effective_username() -> str:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None:
        try:
            import pwd

            return pwd.getpwuid(geteuid()).pw_name
        except (KeyError, ImportError):
            pass
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # pragma: no cover — getuser falls back to env vars
        return ""


def _matches_any(value: str, patterns: Any) -> bool:
    """True when ``value`` matches at least one glob in ``patterns``. A missing
    or empty list means "all" and matches anything (``fnmatch`` globs)."""
    if not patterns:
        return True
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(fnmatch.fnmatch(value, str(p)) for p in patterns)


def check_denylist(
    name: str,
    *,
    username: str | None = None,
    hostname: str | None = None,
    path: Path | None = None,
) -> tuple[str, str] | None:
    """Return ``(mode, reason)`` when ``name`` is denied for the current
    identity, else ``None``.

    ``username`` / ``hostname`` are resolved from the live process when omitted
    (tests pass them explicitly for determinism). A run is denied when any rule
    matches: the runnable name AND username AND hostname each match at least one
    glob in their list. ``disabled = true`` is a fast path that denies
    everything. The reserved name ``runspec`` is always allowed.
    """
    if name == _SELF_NAME:
        return None

    section = load_denylist(path)
    if section is None:
        return None

    mode = str(section.get("mode", "enforce"))

    if username is None or hostname is None:
        live_user, live_host = current_identity()
        username = live_user if username is None else username
        hostname = live_host if hostname is None else hostname

    if section.get("disabled") is True:
        return (mode, str(section.get("reason", "")))

    rules = section.get("rule", [])
    if isinstance(rules, dict):  # tolerate a single inline table
        rules = [rules]
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if _matches_any(name, rule.get("runnables")) and _matches_any(username, rule.get("users")) and _matches_any(hostname, rule.get("hosts")):
            return (mode, str(rule.get("reason", "")))
    return None


def status(path: Path | None = None) -> dict[str, Any]:
    """Return the denylist as a structured, normalised dict for display/JSON.

    Shape: ``{path, present, mode, disabled, reason, rules: [{runnables, users,
    hosts, reason}]}`` with every rule dimension defaulted to ``["*"]`` so a
    consumer (the ``--json`` CLI output, the console status table) never has to
    re-apply the "omitted ⇒ all" rule."""
    if path is None:
        path = resolve_denylist_path()
    section = load_denylist(path)
    present = section is not None
    section = section or {}
    rules = section.get("rule", [])
    if isinstance(rules, dict):
        rules = [rules]
    norm_rules = [
        {
            "runnables": list(r["runnables"]) if r.get("runnables") else ["*"],
            "users": list(r["users"]) if r.get("users") else ["*"],
            "hosts": list(r["hosts"]) if r.get("hosts") else ["*"],
            "reason": str(r.get("reason", "")),
        }
        for r in rules
        if isinstance(r, dict)
    ]
    return {
        "path": str(path),
        "present": present,
        "mode": str(section.get("mode", "enforce")),
        "disabled": bool(section.get("disabled")),
        "reason": str(section.get("reason", "")),
        "rules": norm_rules,
    }


# ── Writing (used by `runspec deny`) ────────────────────────────────────────


def _toml_str(value: Any) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_str_array(items: Any) -> str:
    return "[" + ", ".join(_toml_str(item) for item in items) + "]"


def serialize(section: dict[str, Any]) -> str:
    """Render a ``[denylist]`` section back to TOML text. The library has no
    runtime deps and stdlib ``tomllib`` is read-only, so this is a small,
    purpose-built writer for the denylist's flat shape."""
    lines = ["[denylist]"]
    lines.append(f"mode = {_toml_str(section.get('mode', 'enforce'))}")
    if section.get("disabled"):
        lines.append("disabled = true")
    if section.get("reason"):
        lines.append(f"reason = {_toml_str(section['reason'])}")

    rules = section.get("rule", [])
    if isinstance(rules, dict):
        rules = [rules]
    for rule in rules:
        lines.append("")
        lines.append("[[denylist.rule]]")
        for key in ("runnables", "users", "hosts"):
            if rule.get(key):
                lines.append(f"{key} = {_toml_str_array(rule[key])}")
        if rule.get("reason"):
            lines.append(f"reason = {_toml_str(rule['reason'])}")
    return "\n".join(lines) + "\n"


def write_denylist(section: dict[str, Any], path: Path | None = None) -> Path:
    """Atomically write ``section`` to the denylist file after validating that
    it re-parses. Returns the path written."""
    if path is None:
        path = resolve_denylist_path()
    content = serialize(section)
    tomllib.loads(content)  # validate before touching the real file
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path
