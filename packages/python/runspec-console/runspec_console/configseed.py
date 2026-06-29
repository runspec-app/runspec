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

from .bridge_const import logger

ENTRY_POINT_GROUP = "runspec_console.config_seed"


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
