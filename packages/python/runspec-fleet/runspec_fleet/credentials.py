"""
credentials.py — named credentials: metadata in TOML, secret values in the OS keyring.

Vendored from runspec-console (kept behaviour-identical by ``tests/test_parity.py``)
plus a small **reverse lookup** (``credential_prefix_for_env`` / ``match_credentials``)
that runspec-console has no need for: the gateway matches a runnable's declared
arg ``env`` names back to the credential that supplies them, so a runnable reading
``WINDOWS_USERNAME`` auto-pulls the credential labelled ``Windows``.

A credential's **secret value never touches disk** in the fleet's own files: the
metadata (label, kind, scope) lives in ``runspec_credentials.toml``; the secret is
stored in the OS keychain via ``keyring`` — keyed by the credential's stable ``id``
under the ``runspec-console`` keyring service, so a secret saved by the desktop
console is readable by the plugin and vice versa (the two share one keychain).

A credential entry (metadata)::

  [[credential]]
  id              = "svc-prod"
  label           = "Service account (prod)"   # → SVC_PROD_* env var prefix
  kind            = "userpass"                  # userpass | password | token | ssh-key
  username        = "svc_runner"                # non-secret
  scope_hosts     = ["prod-*"]                  # fnmatch patterns; empty = any host
  scope_runnables = ["deploy", "db-*"]          # fnmatch patterns; empty = any runnable
"""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# Keyring service namespace — deliberately the SAME as runspec-console so the two
# tools share one keychain (a secret saved in the console is usable by the plugin).
KEYRING_SERVICE = "runspec-console"

# Metadata fields persisted to runspec_credentials.toml, in write order. The
# secret value is deliberately absent — it only ever lives in the keyring.
_STR_FIELDS = (
    "id",
    "label",
    "kind",
    "username",
    "username_arg",
    "secret_arg",
    "private_key",
    "public_key",
)
_LIST_FIELDS = ("scope_hosts", "scope_runnables")

VALID_KINDS = ("userpass", "password", "token", "ssh-key")


# ── name-derived environment variables ─────────────────────────────────────────
#
# A credential applied to a run is injected as **plain environment variables**
# whose names derive from the credential's label and kind by a strict convention,
# so the operator names a credential to match what the runnable reads from the
# environment:
#
#     label "Jira"      kind userpass  →  JIRA_USERNAME/JIRA_USER, JIRA_PASSWORD
#     label "Deploy"    kind token     →  DEPLOY_TOKEN, DEPLOY_PAT
#
# This is distinct from the ``RUNSPEC_*_ARG_*`` channel (executor.secret_env),
# which binds a typed password arg to its value. ``ssh-key`` is not env-injected.

# Which env var suffix(es) each kind emits, keyed by the credential field that
# supplies the value ("username" → metadata, "secret" → keyring). The first suffix
# is canonical; the rest are additive aliases (so one credential covers the common
# spellings a runnable might read).
_KIND_ENV_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "userpass": {"username": ("USERNAME", "USER"), "secret": ("PASSWORD",)},
    "password": {"secret": ("PASSWORD",)},
    "token": {"secret": ("TOKEN", "PAT")},
    "ssh-key": {},
}

# Every env var suffix any kind can emit — the set the reverse lookup strips to
# recover a prefix. Longest-first so ``_USERNAME`` is tried before ``_USER``.
_ALL_ENV_SUFFIXES = tuple(
    sorted(
        {suffix for fields in _KIND_ENV_FIELDS.values() for suffixes in fields.values() for suffix in suffixes},
        key=len,
        reverse=True,
    )
)


def env_prefix(label: str) -> str:
    """Normalise a credential label to an env var prefix.

    Uppercase; every run of non-alphanumeric characters becomes a single ``_``;
    leading/trailing underscores are stripped. Returns ``""`` when nothing usable
    remains (such a credential emits no env vars)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").upper()


def env_field_map(kind: str) -> dict[str, tuple[str, ...]]:
    """The {field: (suffix, …)} map for a credential kind (empty for unknown/
    ssh-key). Each field maps to one or more env var suffixes (canonical first)."""
    return _KIND_ENV_FIELDS.get(kind, {})


def env_var_names(label: str, kind: str) -> list[str]:
    """The env var names a credential would set — for previews (no values).
    Includes the alias names (e.g. both ``_USERNAME`` and ``_USER``)."""
    prefix = env_prefix(label)
    if not prefix:
        return []
    return [f"{prefix}_{suffix}" for suffixes in env_field_map(kind).values() for suffix in suffixes]


def env_vars_for(entry: dict[str, Any], secret: str | None) -> dict[str, str]:
    """The plain env vars a credential sets, given its (already-fetched) secret.

    ``username`` comes from the entry metadata, the secret from the keyring (passed
    in so this stays pure). Each field's value is set under every suffix the kind
    declares (canonical + aliases). Empty prefix, empty values, and the ``ssh-key``
    kind yield nothing."""
    prefix = env_prefix(str(entry.get("label", "")))
    if not prefix:
        return {}
    values = {
        "username": str(entry.get("username", "") or ""),
        "secret": str(secret or ""),
    }
    out: dict[str, str] = {}
    for field, suffixes in env_field_map(str(entry.get("kind", "userpass"))).items():
        val = values.get(field, "")
        if val:
            for suffix in suffixes:
                out[f"{prefix}_{suffix}"] = val
    return out


# ── reverse lookup: env var name → the credential that supplies it ─────────────
#
# runspec-console never needs this (it derives forward, label → env vars, and the
# operator picks credentials by scope). The gateway matches the *other* way: a
# runnable declares it reads ``WINDOWS_USERNAME`` (an arg ``env``), and we find the
# credential labelled ``Windows`` to inject. This is the round-trip inverse of
# ``env_prefix`` + the suffix table.


def credential_prefix_for_env(env_name: str) -> str | None:
    """Recover a credential env prefix from a declared env var name.

    ``WINDOWS_USERNAME`` → ``WINDOWS``; ``DEPLOY_PAT`` → ``DEPLOY``. Returns None
    when the name carries no known credential suffix (so it can't have come from a
    credential by this convention). Matching is suffix-longest-first so
    ``*_USERNAME`` wins over ``*_USER``."""
    upper = env_name.strip().upper()
    for suffix in _ALL_ENV_SUFFIXES:
        tail = f"_{suffix}"
        if upper.endswith(tail) and len(upper) > len(tail):
            return upper[: -len(tail)]
    return None


def _scope_matches(entry: dict[str, Any], host: str, runnable: str) -> bool:
    """True when the credential's fnmatch scope globs admit this host+runnable.

    An empty (or missing) scope list matches anything — same semantics as
    runspec-console's ``_scoped_credentials``."""

    def ok(patterns: Any, value: str) -> bool:
        pats = [str(p) for p in (patterns or []) if str(p).strip()]
        return not pats or any(fnmatch.fnmatch(value, p) for p in pats)

    return ok(entry.get("scope_hosts"), host) and ok(entry.get("scope_runnables"), runnable)


def match_credentials(
    arg_env_names: list[str],
    host: str,
    runnable: str,
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The credentials to inject for a runnable invocation, de-duplicated by id.

    Union of two matchers (either alone is enough):

    1. **Auto by arg env** — for each env var the runnable declares it reads
       (``arg_env_names``), recover the prefix and pick the credential whose
       ``env_prefix(label)`` equals it. Zero-config: the runnable's own interface
       says which credential it wants.
    2. **Scope globs** — any credential whose ``scope_hosts``/``scope_runnables``
       admit this host+runnable. Covers runnables that read bare env the schema
       can't advertise (e.g. runspec-jsm reading ``JSM_USER``).

    Order is stable (first match wins on id); ``ssh-key`` credentials and
    empty-prefix labels are naturally excluded because they emit no env vars."""
    wanted_prefixes = {p for name in arg_env_names if (p := credential_prefix_for_env(name)) is not None}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        cid = str(entry.get("id", ""))
        if not cid or cid in seen:
            continue
        prefix = env_prefix(str(entry.get("label", "")))
        auto = bool(prefix) and prefix in wanted_prefixes
        scoped = _scope_matches(entry, host, runnable)
        # Scope alone matches only when the operator declared a scope; a
        # credential with no scope and no arg-env hit is not force-injected
        # everywhere (that would leak every credential into every run).
        has_scope = bool(entry.get("scope_hosts") or entry.get("scope_runnables"))
        if auto or (scoped and has_scope):
            out.append(entry)
            seen.add(cid)
    return out


# ── metadata store (TOML) ──────────────────────────────────────────────────────


def load_credentials(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return data.get("credential", [])


def save_credentials(path: Path, entries: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    for c in entries:
        lines.append("[[credential]]")
        for k in _STR_FIELDS:
            v = c.get(k)
            if v is None or (v == "" and k not in ("id", "label")):
                continue  # id + label always written; other strings only if set
            lines.append(f"{k} = {_toml_str(str(v))}")
        for k in _LIST_FIELDS:
            vals = [str(x) for x in (c.get(k) or []) if str(x).strip()]
            if vals:
                inner = ", ".join(_toml_str(x) for x in vals)
                lines.append(f"{k} = [{inner}]")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _toml_str(v: str) -> str:
    """Escape a string as a TOML basic string literal."""
    escaped = v.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ── keyring (secret values) ────────────────────────────────────────────────────


def _keyring() -> Any | None:
    """The ``keyring`` module, or None when the optional extra isn't installed."""
    try:
        import keyring

        return keyring
    except Exception:
        return None


def available() -> bool:
    """True if a usable keyring backend is present (the feature can store secrets)."""
    return backend_name() is not None


def backend_name() -> str | None:
    """The active keyring backend's class name (e.g. ``WinVaultKeyring``), or None."""
    kr = _keyring()
    if kr is None:
        return None
    try:
        backend = kr.get_keyring()
        name = type(backend).__name__
        if name == "Keyring" and type(backend).__module__.endswith("fail"):
            return None
        return name
    except Exception:
        return None


def set_secret(cred_id: str, value: str) -> None:
    kr = _keyring()
    if kr is None:
        raise RuntimeError("credential storage unavailable — install the 'credentials' extra (pip install runspec-fleet[credentials])")
    kr.set_password(KEYRING_SERVICE, cred_id, value)


def get_secret(cred_id: str) -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(KEYRING_SERVICE, cred_id)
    except Exception:
        return None


def has_secret(cred_id: str) -> bool:
    return bool(get_secret(cred_id))


def delete_secret(cred_id: str) -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(KEYRING_SERVICE, cred_id)
    except Exception:
        pass
