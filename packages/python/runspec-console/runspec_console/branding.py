"""
branding.py — resolve the app's white-label identity from the active LLM plugin.

When someone wraps runspec-console in their own distribution (one that declares
``runspec-console`` as a dependency), the app can present *their* identity instead
of runspec's: the window title, the docs link, and the package the update check
targets. The white-label distribution is identified either by the **active** LLM
provider it supplies (``[llm] provider`` in config.toml) **or**, for an AI-off /
adapter-less package, by the **config seed** it contributes (entry-point group
``runspec_console.config_seed``) — so a fleet-only console with no model adapter
still brands itself from the package it was installed as.

Resolution is per-field, first hit wins:

    title            : [console.theme] brand_name     → prettified dist name
                       → "runspec console"
    docs_url         : [console] docs_url             → dist Project-URL
                       (Documentation → Homepage → Home-page) → DEFAULT_DOCS_URL
    version_package  : dist name                      → "runspec-console"

The ``dist`` is the external distribution supplying the active provider, else the
single external distribution contributing a config seed. A built-in provider
(anthropic/openai/bedrock/langserve) with no seed package falls straight through
to the runspec defaults. ``brand_name`` is an explicit display override (it drives
the sidebar too), so it wins the title even when a dist is present, but it never
feeds ``version_package`` (which needs a real dist name). Every field is wrapped
so a malformed / missing dist degrades to its default rather than raising into the
UI (the same best-effort contract as ``version.latest_version``).
"""

from __future__ import annotations

import importlib.metadata as meta
from dataclasses import dataclass

from . import configseed
from .adapters.base import ENTRY_POINT_GROUP, builtin_provider_names
from .config import DEFAULT_DOCS_URL, read_config

_DEFAULT_TITLE = "runspec console"
_DEFAULT_VERSION_PACKAGE = "runspec-console"


@dataclass(frozen=True)
class Branding:
    title: str
    docs_url: str
    version_package: str


def _prettify_dist_name(name: str) -> str:
    """``mycorp-console`` → ``Mycorp Console``; ``acme_ops`` → ``Acme Ops``."""
    tokens = [t for t in name.replace("_", "-").split("-") if t]
    return " ".join(t[:1].upper() + t[1:] for t in tokens)


def _normalize_label(label: str) -> str:
    """PEP 753-style label match: lowercase, drop non-alphanumerics.

    ``"Home-page"`` / ``"Home Page"`` → ``"homepage"``; ``"Documentation"`` →
    ``"documentation"``.
    """
    return "".join(ch for ch in label.lower() if ch.isalnum())


def _docs_from_dist(dist: meta.Distribution) -> str | None:
    """A docs URL from a distribution's metadata, or ``None``.

    Prefers a ``Project-URL`` labelled Documentation, then Homepage, then the
    legacy top-level ``Home-page`` field.
    """
    urls: dict[str, str] = {}
    try:
        for entry in dist.metadata.get_all("Project-URL") or []:
            label, _, url = str(entry).partition(",")
            url = url.strip()
            if url:
                urls.setdefault(_normalize_label(label), url)
    except Exception:
        urls = {}
    if "documentation" in urls:
        return urls["documentation"]
    if "homepage" in urls:
        return urls["homepage"]
    # Legacy single-value field. PackageMetadata is email.message-like; index
    # access raises KeyError when absent.
    try:
        home = dist.metadata["Home-page"]
    except KeyError:
        home = None
    if isinstance(home, str) and home.strip():
        return home.strip()
    return None


def _active_dist(config: dict) -> meta.Distribution | None:
    """The distribution providing the active LLM provider, or ``None``.

    ``None`` when no provider is set, the provider is a runspec built-in, or the
    provider can't be traced to an installed distribution.
    """
    provider = config.get("llm", {}).get("provider")
    if not isinstance(provider, str) or not provider.strip():
        return None
    provider = provider.strip()
    if provider in builtin_provider_names():
        return None

    # Dotted-path provider ("package.module:Class") — map the top-level module to
    # its distribution.
    if ":" in provider:
        top = provider.partition(":")[0].split(".", 1)[0]
        try:
            dist_names = meta.packages_distributions().get(top)
        except Exception:
            dist_names = None
        if not dist_names:
            return None
        try:
            return meta.distribution(dist_names[0])
        except Exception:
            return None

    # Entry-point provider — find the advertised name in the adapters group.
    try:
        for ep in meta.entry_points(group=ENTRY_POINT_GROUP):
            if ep.name == provider:
                return ep.dist
    except Exception:
        return None
    return None


def _seed_dist() -> meta.Distribution | None:
    """The external distribution that contributes a config seed, or ``None``.

    An AI-off / adapter-less white-label package brands the console by shipping a
    config seed (entry-point group ``runspec_console.config_seed``) rather than an
    LLM adapter, so there is no active provider to trace. The single such
    distribution is the branding source — the same one that names the
    ``%APPDATA%`` config folder (:func:`config.app_dir`), so the title and folder
    always agree. Delegates to :func:`configseed.branding_dist`; best-effort (any
    lookup failure ⇒ ``None``).
    """
    try:
        return configseed.branding_dist()
    except Exception:
        return None


def _brand_name(config: dict) -> str | None:
    """``[console.theme] brand_name`` if set — the explicit display override that
    also titles the sidebar, so the window title matches it."""
    console = config.get("console")
    if isinstance(console, dict):
        theme = console.get("theme")
        if isinstance(theme, dict):
            name = theme.get("brand_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _config_docs_url(config: dict) -> str | None:
    console = config.get("console")
    if isinstance(console, dict):
        url = console.get("docs_url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def resolve_branding(config: dict | None = None) -> Branding:
    """Resolve the app identity (title, docs URL, version-check package).

    ``config`` lets a caller pass an already-loaded config.toml dict to avoid a
    redundant file read; defaults to reading it.
    """
    if config is None:
        try:
            config = read_config()
        except Exception:
            config = {}

    # The branding distribution: the active LLM provider's package, else the
    # config-seed package (AI-off / adapter-less white-label). Best-effort.
    try:
        dist = _active_dist(config)
    except Exception:
        dist = None
    if dist is None:
        dist = _seed_dist()

    # title — explicit [console.theme] brand_name wins (matches the sidebar), then
    # the branding dist's prettified name, then the default.
    title = _DEFAULT_TITLE
    brand = _brand_name(config)
    if brand:
        title = brand
    elif dist is not None:
        try:
            pretty = _prettify_dist_name(dist.name)
            if pretty:
                title = pretty
        except Exception:
            pass

    # docs_url — [console] docs_url wins, then the dist's metadata, then default.
    docs_url = _config_docs_url(config)
    if docs_url is None and dist is not None:
        try:
            docs_url = _docs_from_dist(dist)
        except Exception:
            docs_url = None
    if docs_url is None:
        docs_url = DEFAULT_DOCS_URL

    # version_package — dist name, else runspec-console.
    version_package = _DEFAULT_VERSION_PACKAGE
    if dist is not None:
        try:
            if dist.name:
                version_package = dist.name
        except Exception:
            pass

    return Branding(title=title, docs_url=docs_url, version_package=version_package)
