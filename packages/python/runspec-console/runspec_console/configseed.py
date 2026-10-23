"""
configseed.py — discover baseline config bundled inside the deployment package.

A white-label / corporate distribution that wraps runspec-console can **ship a
baseline config as package data** so a fresh install is configured with no git
host and no PyPI index reachable at run time — the floor for a disaster-recovery
or air-gapped scenario. This module only *discovers* that config; the merge onto
local config reuses the exact pipeline a git pull uses
(``Bridge._apply_config_layers``). See ``docs/design/config-seed.md``.

A contributor declares an entry point in group ``runspec_console.config_seed``
pointing at a package that holds a ``config_seed/`` data directory of config
TOMLs (filenames must be in ``gitsync.KNOWN_FILES``)::

    [project.entry-points."runspec_console.config_seed"]
    mycorp = "mycorp_console"          # a package with a config_seed/ dir

``discover()`` returns ``(fetched_layers, stamp)`` shaped for the apply pipeline:
``fetched_layers`` maps each file name to its bytes from every contributor (low→
high by sorted distribution name — the documented, bounded fold order), and
``stamp`` maps each contributing distribution → its version (the idempotency key,
read straight from installed metadata so it tracks the package with nothing to
hand-maintain).

Best-effort throughout: a malformed or unreadable contributor is skipped and
logged, never raised — a broken plugin must not stop the console from starting.
"""

from __future__ import annotations

import importlib.metadata as meta

from .bridge_const import logger

ENTRY_POINT_GROUP = "runspec_console.config_seed"


def branding_dist() -> meta.Distribution | None:
    """The single external distribution contributing a config seed, or ``None``.

    This is the white-label identity for a package that ships a config seed
    rather than an LLM adapter: it names **both** the window title (see
    ``branding.resolve_branding``) **and** the ``%APPDATA%`` config folder (see
    ``config.app_dir``), so the two always agree — install ``acme-console`` and
    the title reads "Acme Console" while its config lives in
    ``%APPDATA%/acme-console``.

    Returns the one config-seed contributor whose distribution isn't
    ``runspec-console`` itself; ``None`` when there are none, or more than one
    (ambiguous — callers fall back to the default). Metadata-only (never reads
    config.toml), so it is safe to call *before* config is located and cheap
    enough to cache. Best-effort: any lookup failure degrades to ``None``.
    """
    try:
        eps = list(meta.entry_points(group=ENTRY_POINT_GROUP))
    except Exception:
        return None
    out: list[meta.Distribution] = []
    seen: set[str] = set()
    for ep in eps:
        dist = getattr(ep, "dist", None)
        name = (getattr(dist, "name", None) or "").strip().lower()
        if not name or name == "runspec-console" or name in seen:
            continue
        seen.add(name)
        out.append(dist)  # type: ignore[arg-type]
    return out[0] if len(out) == 1 else None


def branding_dist_name() -> str | None:
    """The distribution name of :func:`branding_dist` (e.g. ``acme-console``),
    stripped, or ``None``."""
    dist = branding_dist()
    name = getattr(dist, "name", None)
    return name.strip() if isinstance(name, str) and name.strip() else None


def discover() -> tuple[dict[str, list[bytes]], dict[str, str]]:
    """Discover bundled seed config from installed distributions.

    Returns ``({name: [bytes, ...]}, {dist: version})``. The byte lists are
    ordered low→high by sorted distribution name (ready for the fold in
    ``_apply_config_layers``); the stamp carries only distributions that
    actually contributed at least one known file. Both empty when nothing is
    installed.
    """
    import importlib.metadata as meta
    import importlib.resources as resources

    from .gitsync import KNOWN_FILES

    try:
        eps = list(meta.entry_points(group=ENTRY_POINT_GROUP))
    except Exception:
        return {}, {}

    # Resolve each entry point to (distribution name, version, module). Fall back
    # to the module name when a distribution can't be traced, so a dev/editable
    # install with odd metadata still seeds.
    contributors: list[tuple[str, str, str]] = []
    for ep in eps:
        module = ep.module  # the package that holds the config_seed/ data dir
        dist = getattr(ep, "dist", None)
        dist_name = getattr(dist, "name", None) or module
        version = getattr(dist, "version", None) or ""
        contributors.append((dist_name, version, module))

    # Stable, documented fold order: low→high by distribution name.
    contributors.sort(key=lambda c: c[0])

    fetched: dict[str, list[bytes]] = {}
    stamp: dict[str, str] = {}
    for dist_name, version, module in contributors:
        try:
            seed_dir = resources.files(module) / "config_seed"
            if not seed_dir.is_dir():
                continue
            contributed = False
            for child in sorted(seed_dir.iterdir(), key=lambda p: p.name):
                if child.name in KNOWN_FILES and child.is_file():
                    fetched.setdefault(child.name, []).append(child.read_bytes())
                    contributed = True
            if contributed:
                stamp[dist_name] = version
        except Exception:
            logger.warning(
                "config seed from %r could not be read — skipping",
                module,
                exc_info=True,
            )
            continue

    return fetched, stamp
