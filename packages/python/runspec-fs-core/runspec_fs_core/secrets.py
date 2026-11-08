"""Secret placeholders — put a credential into a file without it passing through
whoever wrote the text.

The text given to ``write-file`` / ``edit-file`` may carry placeholders::

    password = "{{secret:grafana-cloud}}"
    username = "{{secret:grafana-cloud.username}}"

``grafana-cloud`` names a credential. The *value* never travels in the text: the
caller (runspec-console, or a person in a shell) puts it in the process
environment under a reserved name, and :func:`fill_secrets` swaps it in just
before the file is written::

    {{secret:grafana-cloud}}           ->  $RUNSPEC_SECRET_GRAFANA_CLOUD
    {{secret:grafana-cloud.username}}  ->  $RUNSPEC_SECRET_GRAFANA_CLOUD__USERNAME

Only ``RUNSPEC_SECRET_*`` variables are ever read, so a placeholder can't copy
some other environment variable (a service account's cloud key, say) into a file.
A placeholder with no value is an error and nothing is written.

:func:`mask_secrets` turns every filled secret value back into its placeholder in
anything that is printed (a write result, an ``edit-file`` diff, an error), so the
value only ever lands in the file. Usernames are filled but not masked — they are
not secret and are shown elsewhere anyway.

Pure stdlib, no runspec dependency, so a private wrapper can use it too.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any

from runspec_fs_core.errors import FsCoreError

#: Environment variable prefix the values are read from.
ENV_PREFIX = "RUNSPEC_SECRET_"

#: Field suffix for the username part of a credential (double underscore so it
#: can't collide with a credential id, which normalises to single underscores).
USERNAME_SUFFIX = "__USERNAME"

#: Shortest secret value that is filled. Masking replaces every occurrence of the
#: value in printed output, so a one- or two-character "secret" would garble it.
MIN_SECRET_LEN = 4

PLACEHOLDER_RE = re.compile(r"\{\{secret:([A-Za-z0-9][A-Za-z0-9_.-]*?)(\.username)?\}\}")


class SecretNotAvailableError(FsCoreError):
    """A ``{{secret:…}}`` placeholder has no value in the environment (or one too
    short to keep out of the output). Nothing is written."""


def env_name(credential_id: str, field: str = "secret") -> str:
    """The environment variable a placeholder is filled from.

    ``credential_id`` is upper-cased with every run of non-alphanumerics turned
    into one ``_`` (``grafana-cloud`` -> ``GRAFANA_CLOUD``). runspec-console derives
    the same name when it sends the value."""
    norm = re.sub(r"[^A-Za-z0-9]+", "_", credential_id).strip("_").upper()
    if not norm:
        raise ValueError(f"not a usable credential id: {credential_id!r}")
    name = ENV_PREFIX + norm
    return name + USERNAME_SUFFIX if field == "username" else name


def find_placeholders(text: str) -> list[tuple[str, str]]:
    """The ``(credential_id, field)`` pairs a text refers to, in order, without
    duplicates. ``field`` is ``"secret"`` or ``"username"``."""
    seen: list[tuple[str, str]] = []
    for m in PLACEHOLDER_RE.finditer(text):
        ref = (m.group(1), "username" if m.group(2) else "secret")
        if ref not in seen:
            seen.append(ref)
    return seen


def has_placeholders(text: str) -> bool:
    return PLACEHOLDER_RE.search(text) is not None


def placeholder(credential_id: str, field: str = "secret") -> str:
    return "{{secret:" + credential_id + (".username" if field == "username" else "") + "}}"


def fill_secrets(text: str, environ: Mapping[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """Replace every placeholder in ``text`` from ``environ`` (default
    ``os.environ``).

    Returns ``(filled_text, masks)`` where ``masks`` maps each filled *secret*
    value to the placeholder it came from — hand it to :func:`mask_secrets`.
    Raises :class:`SecretNotAvailableError` naming every placeholder that has no
    value."""
    env = os.environ if environ is None else environ
    refs = find_placeholders(text)
    values: dict[tuple[str, str], str] = {}
    missing: list[str] = []
    for cid, field in refs:
        value = env.get(env_name(cid, field), "")
        if not value or (field == "secret" and len(value) < MIN_SECRET_LEN):
            missing.append(f"{placeholder(cid, field)} (${env_name(cid, field)})")
            continue
        values[(cid, field)] = value
    if missing:
        raise SecretNotAvailableError(
            "no value for " + ", ".join(missing) + ". Secrets come from a saved credential "
            "with that id (runspec-console sends it when the agent's text names it), or from "
            "the RUNSPEC_SECRET_* variable shown, set by whoever runs this command. Nothing was written."
        )

    def sub(m: re.Match[str]) -> str:
        return values[(m.group(1), "username" if m.group(2) else "secret")]

    filled = PLACEHOLDER_RE.sub(sub, text)
    masks = {v: placeholder(cid, field) for (cid, field), v in values.items() if field == "secret"}
    return filled, masks


def mask_secrets(data: Any, masks: Mapping[str, str]) -> Any:
    """Return ``data`` with every secret value in ``masks`` replaced by its
    placeholder — strings, and strings nested in dicts / lists. Longer values are
    replaced first so one secret that contains another is masked whole."""
    if not masks:
        return data
    order = sorted(masks, key=len, reverse=True)

    def one(s: str) -> str:
        for value in order:
            if value in s:
                s = s.replace(value, masks[value])
        return s

    def walk(obj: Any) -> Any:
        if isinstance(obj, str):
            return one(obj)
        if isinstance(obj, dict):
            return {k: walk(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [walk(v) for v in obj]
        return obj

    return walk(data)


def mask_text(text: str, masks: Mapping[str, str]) -> str:
    """Mask a block of printed text, which may be JSON: also catches a value as
    it appears inside a JSON string (quotes / backslashes escaped)."""
    if not masks:
        return text
    for value in sorted(masks, key=len, reverse=True):
        text = text.replace(value, masks[value])
        escaped = json.dumps(value)[1:-1]
        if escaped != value:
            text = text.replace(escaped, masks[value])
    return text


def fill_verb_secrets(verb: str, kwargs: dict[str, Any], environ: Mapping[str, str] | None = None) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Fill placeholders in the text a write verb is about to put in a file.

    ``write-file`` -> ``content``; ``edit-file`` -> each new text (old texts are
    left alone, so an edit can't be used to test whether a file holds a secret).
    Other verbs pass through untouched.

    Returns ``(kwargs, masks, credential_ids)``: a new kwargs dict with the text
    filled (and ``private=True`` when a secret was filled, so a new file starts
    owner-only), the masks for :func:`mask_secrets`, and the credential ids used
    (for an audit record — never the values)."""
    masks: dict[str, str] = {}
    ids: list[str] = []

    def fill(text: str) -> str:
        if not has_placeholders(text):
            return text
        filled, m = fill_secrets(text, environ)
        masks.update(m)
        for cid, _field in find_placeholders(text):
            if cid not in ids:
                ids.append(cid)
        return filled

    out = dict(kwargs)
    if verb == "write-file":
        out["content"] = fill(kwargs["content"])
    elif verb == "edit-file":
        out["edits"] = [[old, fill(new)] for old, new in kwargs["edits"]]
    else:
        return out, masks, ids
    if masks:
        out["private"] = True
    return out, masks, ids
