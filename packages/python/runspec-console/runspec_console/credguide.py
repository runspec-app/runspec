"""
credguide.py — work out which credentials the installed runnables (and console
features) need, and what each saved credential is actually used by.

A new operator's first hurdle is the credential ↔ env-var convention: a
credential labelled ``Jira`` sets ``JIRA_USERNAME``/``JIRA_USER``/``JIRA_PASSWORD``
(or ``JIRA_TOKEN``/``JIRA_PAT``), and an unscoped credential reaches a runnable
only when that runnable *declares* one of those names as an arg ``env`` alias (or
an arg the credential binds). This module reads the runnables the console has
discovered and turns that rule around:

* :func:`needs` — one suggestion per env-var prefix a runnable reads a credential
  under (``JIRA`` → "add a Jira token, or a Jira username + password"), with the
  kinds that would match and the exact names each sets; plus ``password`` args
  that declare no env var at all and so can only be reached by an arg binding.
* :func:`applies_to` — which runnables (on which hosts) a credential, saved or
  being typed, would auto-apply to. Same rule as ``Bridge._scoped_credentials``,
  which delegates to :func:`declared_use` / :func:`has_scope` /
  :func:`scope_matches` so the two can't drift.
* :func:`label_hint` — for a credential that matches nothing, the label/kind that
  probably was meant ("Jira PAT" sets ``JIRA_PAT_TOKEN``; "Jira" would match).

Pure: no I/O, no keyring — the bridge passes in the runnable cache, the saved
credentials and which ids have a stored secret.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

from . import credentials as cred

# Env var suffixes a credential sets (canonical + aliases, every kind). A var
# ending in one of these is something a credential can supply by name.
_EMITTED: dict[str, str] = {}
for _kind, _fields in cred._KIND_ENV_FIELDS.items():
    for _field, _suffixes in _fields.items():
        for _s in _suffixes:
            _EMITTED.setdefault(_s, _field)

# Longest first, so ``…_USERNAME`` is tried before ``…_USER``.
_SUFFIXES = sorted(_EMITTED, key=len, reverse=True)

# Var names that look like a secret but no credential kind sets by name — shown
# so the operator knows the runnable reads them (set them in Env files).
_SECRETISH = ("API_KEY", "SECRET", "KEY", "PASSPHRASE")

# Kinds offered for a need, in the order the UI lists them.
_KIND_ORDER = ("token", "userpass", "password")


# ── the rule shared with Bridge._scoped_credentials ─────────────────────────────


def scope_matches(patterns: Any, value: str) -> bool:
    """True if ``value`` matches any fnmatch pattern (empty list = matches all)."""
    pats = [str(p).strip() for p in (patterns or []) if str(p).strip()]
    if not pats:
        return True
    return any(fnmatch.fnmatchcase(value, p) for p in pats)


def has_scope(entry: dict[str, Any]) -> bool:
    """True when a credential names at least one scope pattern (host or
    runnable). ``["*"]`` counts — it's how an operator deliberately says
    "everywhere"."""
    return any(
        str(p).strip()
        for key in ("scope_hosts", "scope_runnables")
        for p in (entry.get(key) or [])
    )


def declared_use(entry: dict[str, Any], envs: set[str], names: set[str]) -> bool:
    """True when a runnable's own interface asks for this credential: an arg
    ``env`` equal to one of the credential's name-derived env vars, or an arg the
    credential binds via ``username_arg``/``secret_arg``."""
    wanted = {
        n.upper()
        for n in cred.env_var_names(
            str(entry.get("label", "")), str(entry.get("kind", ""))
        )
    }
    if wanted & envs:
        return True
    return any(
        str(entry.get(k) or "").strip() in names
        for k in ("username_arg", "secret_arg")
        if str(entry.get(k) or "").strip()
    )


# ── what the runnables declare ──────────────────────────────────────────────────


@dataclass
class Interface:
    """One runnable's declared interface, merged across the hosts it's on."""

    name: str
    hosts: list[str] = field(default_factory=list)
    envs: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    # env var → description of the arg that declares it (first one wins).
    env_desc: dict[str, str] = field(default_factory=dict)
    # env var → True when the declaring arg is ``password``-typed.
    env_secret: dict[str, bool] = field(default_factory=dict)
    # ``password`` args that declare no env var: (arg name, description, a
    # sibling username-looking arg or "").
    bare_secrets: list[tuple[str, str, str]] = field(default_factory=list)


def _env_list(arg: dict[str, Any]) -> list[str]:
    env = arg.get("env")
    raw = [env] if isinstance(env, str) else list(env or [])
    return [str(e).strip().upper() for e in raw if str(e).strip()]


def _first_sentence(text: str, limit: int = 140) -> str:
    text = " ".join(str(text or "").split())
    cut = text.find(". ")
    if 0 < cut < limit:
        return text[: cut + 1]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _userish(name: str) -> bool:
    n = name.lower().replace("_", "-")
    return n in ("user", "username", "login") or n.endswith(
        ("-user", "-username", "-login")
    )


def interfaces(runnables: list[dict[str, Any]]) -> list[Interface]:
    """Merge the discovered runnable records (one per host × venv) into one
    :class:`Interface` per runnable name, walking every subcommand."""
    by_name: dict[str, Interface] = {}
    for r in runnables or []:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        iface = by_name.setdefault(name, Interface(name=name))
        host = str(r.get("host") or "local")
        if host not in iface.hosts:
            iface.hosts.append(host)

        def walk(node: dict[str, Any], iface: Interface = iface) -> None:
            args = [a for a in (node.get("args") or []) if isinstance(a, dict)]
            arg_names = [str(a.get("name") or "") for a in args]
            for a in args:
                aname = str(a.get("name") or "")
                if aname:
                    iface.names.add(aname)
                envs = _env_list(a)
                is_secret = str(a.get("type") or "") == "password"
                desc = _first_sentence(str(a.get("description") or ""))
                for e in envs:
                    if e.startswith("RUNSPEC_"):
                        continue
                    iface.envs.add(e)
                    iface.env_desc.setdefault(e, desc)
                    iface.env_secret[e] = iface.env_secret.get(e, False) or is_secret
                if is_secret and not envs and aname:
                    if all(b[0] != aname for b in iface.bare_secrets):
                        sib = next((n for n in arg_names if n and _userish(n)), "")
                        iface.bare_secrets.append((aname, desc, sib))
            for sub in (node.get("commands") or {}).values():
                if isinstance(sub, dict):
                    walk(sub)

        walk(r)
    for iface in by_name.values():
        iface.hosts.sort(key=lambda h: (h != "local", h))
    return sorted(by_name.values(), key=lambda i: i.name)


def split_env(name: str) -> tuple[str, str] | None:
    """``JIRA_API_TOKEN`` → ``("JIRA_API", "TOKEN")``: the prefix a credential
    label would have to normalise to, and the suffix it sets. ``None`` when no
    credential kind sets a var ending like this."""
    for s in _SUFFIXES:
        if name.endswith("_" + s) and len(name) > len(s) + 1:
            return name[: -(len(s) + 1)], s
    return None


def suggested_label(prefix: str) -> str:
    """A friendly label that :func:`credentials.env_prefix` maps back to
    ``prefix``: ``JIRA`` → ``Jira``, ``JSM`` → ``JSM``, ``GRAFANA_PROD`` →
    ``Grafana Prod``. Short words stay upper-case (they're usually acronyms)."""
    words = [w for w in prefix.split("_") if w]
    return " ".join(w if len(w) <= 3 else w.capitalize() for w in words)


def _prefixes(iface: Interface) -> list[str]:
    """The env prefixes this runnable reads credentials under, most general
    first. ``GRAFANA_SERVICE_ACCOUNT`` (from ``GRAFANA_SERVICE_ACCOUNT_TOKEN``) is
    dropped when ``GRAFANA`` is also read — one suggestion per service."""
    found = {p for p in (split_env(e) for e in iface.envs) if p}
    prefixes = sorted({p for p, _ in found}, key=lambda p: (len(p), p))
    kept: list[str] = []
    for p in prefixes:
        if not any(p.startswith(k + "_") for k in kept):
            kept.append(p)
    return kept


def _kind_matches(prefix: str, kind: str, envs: set[str]) -> list[str]:
    names = cred.env_var_names(prefix, kind)
    return [n for n in names if n in envs]


def needs(
    runnables: list[dict[str, Any]],
    credentials: list[dict[str, Any]],
    secrets: set[str] | None = None,
) -> dict[str, Any]:
    """What the discovered runnables need, and which saved credentials cover it.

    Returns ``{"needs": [...], "bindings": [...]}``:

    * a **need** per env prefix — ``prefix``, a suggested ``label``, the
      ``runnables`` (``{name, hosts}``) that read it, ``options`` (one per
      credential kind that would match: ``{kind, matches, hint}``), ``reads``
      (every credential-looking var those runnables declare under the prefix),
      ``other_env`` (the prefix's non-secret vars — URL, org id — which a
      credential doesn't set), and ``covered_by`` (saved credentials that
      already auto-apply to every runnable in the need: ``{id, label,
      has_secret}``);
    * a **binding** per ``password`` arg with no env var — reachable only by a
      credential whose ``secret_arg`` names it: ``runnable``, ``hosts``,
      ``arg``, ``username_arg`` (a sibling user arg, or ``""``), ``hint``,
      ``covered_by``.

    Uncovered entries sort first."""
    secrets = secrets or set()
    ifaces = interfaces(runnables)
    grouped: dict[str, list[Interface]] = {}
    for iface in ifaces:
        for p in _prefixes(iface):
            grouped.setdefault(p, []).append(iface)

    def covering(group: list[Interface]) -> list[dict[str, Any]]:
        out = []
        for c in credentials:
            if str(c.get("kind") or "") == "ssh-key":
                continue
            if all(_applies(c, i) for i in group):
                cid = str(c.get("id") or "")
                out.append(
                    {
                        "id": cid,
                        "label": c.get("label", ""),
                        "has_secret": cid in secrets,
                    }
                )
        return out

    need_rows: list[dict[str, Any]] = []
    for prefix, group in grouped.items():
        envs: set[str] = set().union(*(i.envs for i in group))
        under = sorted(e for e in envs if e.startswith(prefix + "_"))
        options = []
        for kind in _KIND_ORDER:
            matches = _kind_matches(prefix, kind, envs)
            if not matches:
                continue
            # A password-only kind is worth offering only when nothing reads a
            # username under this prefix (userpass covers it otherwise).
            if kind == "password" and _kind_matches(prefix, "userpass", envs):
                continue
            secret_var = next(
                (m for m in matches if _EMITTED.get(m[len(prefix) + 1 :]) == "secret"),
                matches[0],
            )
            hint = next(
                (i.env_desc.get(secret_var, "") for i in group if secret_var in i.envs),
                "",
            )
            options.append({"kind": kind, "matches": matches, "hint": hint})
        if not options:
            continue
        reads = [
            e
            for e in under
            if split_env(e)
            or any(i.env_secret.get(e) for i in group)
            or e.endswith(_SECRETISH)
        ]
        other = [e for e in under if e not in reads]
        need_rows.append(
            {
                "prefix": prefix,
                "label": suggested_label(prefix),
                "runnables": [{"name": i.name, "hosts": list(i.hosts)} for i in group],
                "options": options,
                "reads": reads,
                "other_env": other,
                "covered_by": covering(group),
            }
        )

    bindings: list[dict[str, Any]] = []
    for iface in ifaces:
        for arg, hint, sib in iface.bare_secrets:
            cov = []
            for c in credentials:
                if str(c.get("secret_arg") or "").strip() == arg and _applies(c, iface):
                    cid = str(c.get("id") or "")
                    cov.append(
                        {
                            "id": cid,
                            "label": c.get("label", ""),
                            "has_secret": cid in secrets,
                        }
                    )
            bindings.append(
                {
                    "runnable": iface.name,
                    "hosts": list(iface.hosts),
                    "arg": arg,
                    "username_arg": sib,
                    "hint": hint,
                    "label": suggested_label(cred.env_prefix(iface.name)),
                    "covered_by": cov,
                }
            )

    need_rows.sort(key=lambda n: (bool(n["covered_by"]), n["prefix"]))
    bindings.sort(key=lambda b: (bool(b["covered_by"]), b["runnable"], b["arg"]))
    return {"needs": need_rows, "bindings": bindings}


# ── what a credential is used by ────────────────────────────────────────────────


def _applies(entry: dict[str, Any], iface: Interface) -> bool:
    return bool(_hosts_applied(entry, iface))


def _hosts_applied(entry: dict[str, Any], iface: Interface) -> list[str]:
    if has_scope(entry):
        if not scope_matches(entry.get("scope_runnables"), iface.name):
            return []
        return [h for h in iface.hosts if scope_matches(entry.get("scope_hosts"), h)]
    return list(iface.hosts) if declared_use(entry, iface.envs, iface.names) else []


def applies_to(
    entry: dict[str, Any], runnables: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The runnables (``{name, hosts, via}``) a credential auto-applies to.
    ``via`` says why: ``"env"`` (an arg declares one of its env vars, listed in
    ``envs``), ``"arg"`` (its username_arg/secret_arg is an arg there) or
    ``"scope"`` (its host/runnable scope matches)."""
    if str(entry.get("kind") or "") == "ssh-key":
        return []
    out: list[dict[str, Any]] = []
    wanted = {
        n.upper()
        for n in cred.env_var_names(
            str(entry.get("label", "")), str(entry.get("kind", ""))
        )
    }
    for iface in interfaces(runnables):
        hosts = _hosts_applied(entry, iface)
        if not hosts:
            continue
        hit = sorted(wanted & iface.envs)
        via = "scope" if has_scope(entry) else ("env" if hit else "arg")
        out.append({"name": iface.name, "hosts": hosts, "via": via, "envs": hit})
    return out


def label_hint(
    entry: dict[str, Any], need_rows: list[dict[str, Any]]
) -> dict[str, str] | None:
    """For a credential that applies to nothing, the label/kind it was probably
    meant to have: ``{"label", "kind", "why"}`` or ``None``.

    * label close to a need's prefix — ``Jira PAT`` (``JIRA_PAT_*``) or
      ``Jira Token`` → ``Jira``;
    * right label, wrong kind — a ``Jira`` password where only a token is read."""
    kind = str(entry.get("kind") or "userpass")
    if kind == "ssh-key":
        return None
    mine = cred.env_prefix(str(entry.get("label", "")))
    if not mine:
        return None
    for n in need_rows:
        p = n["prefix"]
        kinds = [o["kind"] for o in n["options"]]
        if mine == p:
            if kind not in kinds and kinds:
                return {
                    "label": n["label"],
                    "kind": kinds[0],
                    "why": f"nothing reads {p} as a {kind}; it is read as a {kinds[0]}",
                }
            continue
        if mine.startswith(p + "_") or p.startswith(mine + "_"):
            pick = kind if kind in kinds else kinds[0]
            names = cred.env_var_names(p, pick)
            return {
                "label": n["label"],
                "kind": pick,
                "why": f"the label {entry.get('label')!r} sets {mine}_…; the runnables read {', '.join(names)}",
            }
    return None
