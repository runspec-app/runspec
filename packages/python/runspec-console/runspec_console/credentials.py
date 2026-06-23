"""
credentials.py — named credentials: metadata in TOML, secret values in the OS keyring.

A credential is a saved identity the console can apply to a runnable invocation
or a relay connection. Its **secret value never touches disk** in the console's
own files: the metadata (label, kind, which args it binds, where it's allowed)
lives in ``runspec_credentials.toml`` next to the hosts/filters files, while the
secret is stored in the OS keychain via ``keyring`` — Windows Credential Manager,
macOS Keychain, or the Secret Service / KWallet on Linux — keyed by the
credential's stable ``id``.

A credential entry (metadata)::

  [[credential]]
  id              = "svc-prod"
  label           = "Service account (prod)"
  kind            = "userpass"          # userpass | password | token
  username        = "svc_runner"        # non-secret; applied to username_arg
  username_arg    = "service-username"  # runnable arg that gets the username (plain)
  secret_arg      = "user-password"     # runnable arg that gets the secret (env channel)
  scope_hosts     = ["prod-*"]          # fnmatch patterns; empty = any host
  scope_runnables = ["deploy", "db-*"]  # fnmatch patterns; empty = any runnable

The secret for ``id = "svc-prod"`` is stored under keyring service
``runspec-console``. ``secret_arg`` / ``username_arg`` are how a credential binds
to a runnable's flags (resolved in the invocation path); a relay-only credential
leaves them empty and is referenced by ``id``. The flat shape (single
username/secret arg, string-array scopes) keeps the TOML writer trivial — mirrors
``filters_store`` — while still expressing the ``--service-username`` /
``--user-password`` case.

An ``ssh-key`` credential references an SSH **key pair by file path** rather than
binding to a runnable — Config Sync uses it to authenticate an SSH repo with a
dedicated deploy key (so multiple SSH identities can coexist, one per repo)::

  [[credential]]
  id          = "config-repo"
  label       = "Config repo deploy key"
  kind        = "ssh-key"
  private_key = "~/.ssh/runspec_ed25519"      # passed to paramiko key_filename
  public_key  = "~/.ssh/runspec_ed25519.pub"  # optional; informational

The key material stays on disk (the paths are metadata); only an optional key
**passphrase** is stored in the keyring under the credential's ``id``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# Keyring service namespace; every credential's secret is stored under (SERVICE, id).
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
# A credential applied to a run (the form's bottom section, or an agent invoke)
# is injected as **plain environment variables** whose names derive from the
# credential's label and kind by a strict convention, so the operator names a
# credential to match what the runnable reads from the environment:
#
#     label "Jira"      kind userpass  →  JIRA_USERNAME, JIRA_PASSWORD
#     label "Jira Dev"  kind userpass  →  JIRA_DEV_USERNAME, JIRA_DEV_PASSWORD
#     label "Deploy"    kind token     →  DEPLOY_TOKEN
#
# This is distinct from the ``RUNSPEC_*_ARG_*`` channel (executor.secret_env),
# which binds a typed password arg to its value. ``ssh-key`` is not env-injected.

# Which env var suffix(es) each kind emits, keyed by the credential field that
# supplies the value ("username" → metadata, "secret" → keyring).
_KIND_ENV_FIELDS: dict[str, dict[str, str]] = {
    "userpass": {"username": "USERNAME", "secret": "PASSWORD"},
    "password": {"secret": "PASSWORD"},
    "token": {"secret": "TOKEN"},
    "ssh-key": {},
}


def env_prefix(label: str) -> str:
    """Normalise a credential label to an env var prefix.

    Uppercase; every run of non-alphanumeric characters becomes a single ``_``;
    leading/trailing underscores are stripped. Returns ``""`` when nothing usable
    remains (such a credential emits no env vars)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").upper()


def env_field_map(kind: str) -> dict[str, str]:
    """The {field: suffix} map for a credential kind (empty for unknown/ssh-key)."""
    return _KIND_ENV_FIELDS.get(kind, {})


def env_var_names(label: str, kind: str) -> list[str]:
    """The env var names a credential would set — for UI previews (no values)."""
    prefix = env_prefix(label)
    if not prefix:
        return []
    return [f"{prefix}_{suffix}" for suffix in env_field_map(kind).values()]


def env_vars_for(entry: dict[str, Any], secret: str | None) -> dict[str, str]:
    """The plain env vars a credential sets, given its (already-fetched) secret.

    ``username`` comes from the entry metadata, the secret from the keyring (passed
    in so this stays pure). Empty prefix, empty values, and the ``ssh-key`` kind
    yield nothing."""
    prefix = env_prefix(str(entry.get("label", "")))
    if not prefix:
        return {}
    values = {
        "username": str(entry.get("username", "") or ""),
        "secret": str(secret or ""),
    }
    out: dict[str, str] = {}
    for field, suffix in env_field_map(str(entry.get("kind", "userpass"))).items():
        val = values.get(field, "")
        if val:
            out[f"{prefix}_{suffix}"] = val
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
    """Escape a string as a TOML basic string literal (mirrors filters_store)."""
    escaped = v.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ── keyring (secret values) ────────────────────────────────────────────────────


def _keyring() -> Any | None:
    """The ``keyring`` module, or None when the optional extra isn't installed.

    Credentials are an opt-in feature (``pip install runspec-console[credentials]``);
    importing lazily keeps the core install dependency-free and lets the rest of
    the app run when keyring is absent.
    """
    try:
        import keyring

        return keyring
    except Exception:
        return None


def available() -> bool:
    """True if a usable keyring backend is present (the feature can store secrets)."""
    return backend_name() is not None


def backend_name() -> str | None:
    """The active keyring backend's class name (e.g. ``WinVaultKeyring``), or None.

    Returns None when keyring isn't installed *or* resolves to the fail backend
    (no usable OS store) — the UI uses this to warn that secrets can't be saved.
    """
    kr = _keyring()
    if kr is None:
        return None
    try:
        backend = kr.get_keyring()
        name = type(backend).__name__
        # keyring's "fail" backend raises on use — treat it as unavailable.
        if name == "Keyring" and type(backend).__module__.endswith("fail"):
            return None
        return name
    except Exception:
        return None


def set_secret(cred_id: str, value: str) -> None:
    kr = _keyring()
    if kr is None:
        raise RuntimeError(
            "credential storage unavailable — install the 'credentials' extra "
            "(pip install runspec-console[credentials])"
        )
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
        # Already absent (or backend hiccup) — deletion is best-effort.
        pass
