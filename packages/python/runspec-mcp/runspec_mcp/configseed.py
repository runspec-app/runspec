"""
configseed.py — discover baseline config bundled inside a deployment package.

Ported from runspec-console. A white-label / corporate distribution can **ship a
baseline config as package data** so a fresh (or air-gapped) install is configured
with no config file to hand-write — the disaster-recovery floor. This module only
*discovers* that config; ``config.apply_seed`` folds it onto the local files via
the pure ``configmerge`` primitives.

A contributor declares an entry point pointing at a package that holds a
``config_seed/`` data directory of config TOMLs (filenames must be in
``KNOWN_FILES``)::

    [project.entry-points."runspec_mcp.config_seed"]
    mycorp = "mycorp_fleet"            # a package with a config_seed/ dir

``runspec-mcp`` reads **both** ``runspec_mcp.config_seed`` and the console's
``runspec_console.config_seed`` group, so one corporate seed package configures the
desktop console and this plugin alike.

``discover()`` returns ``(fetched_layers, stamp)`` shaped for the apply pipeline:
``fetched_layers`` maps each file name to its bytes from every contributor (low→
high by sorted distribution name), and ``stamp`` maps each contributing
distribution → its version (the idempotency key). Best-effort throughout: a
malformed contributor is skipped and logged, never raised.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("runspec_mcp")

# Entry-point groups scanned for bundled seed config. The console's group is read
# too so a single seed package configures both tools.
ENTRY_POINT_GROUPS = ("runspec_mcp.config_seed", "runspec_console.config_seed")

# Config files a seed is allowed to bundle. Only the subset the gateway reads
# matters here, but the full console set is accepted so a shared seed's other
# files are simply ignored rather than rejected.
KNOWN_FILES: tuple[str, ...] = (
    "config.toml",
    "runspec_hosts.toml",
    "runspec_groups.toml",
    "runspec_profiles.toml",
    "runspec_credentials.toml",
)


def discover() -> tuple[dict[str, list[bytes]], dict[str, str]]:
    """Discover bundled seed config from installed distributions.

    Returns ``({name: [bytes, ...]}, {dist: version})``. The byte lists are
    ordered low→high by sorted distribution name (ready for the fold in
    ``configmerge.fold_layers``); the stamp carries only distributions that
    actually contributed at least one known file. Both empty when nothing is
    installed.
    """
    import importlib.metadata as meta
    import importlib.resources as resources

    eps: list[Any] = []
    for group in ENTRY_POINT_GROUPS:
        try:
            eps.extend(meta.entry_points(group=group))
        except Exception:
            continue

    # Resolve each entry point to (distribution name, version, module). De-dup by
    # module so a package advertised under both groups contributes once.
    contributors: dict[str, tuple[str, str, str]] = {}
    for ep in eps:
        module = ep.module  # the package that holds the config_seed/ data dir
        dist = getattr(ep, "dist", None)
        dist_name = getattr(dist, "name", None) or module
        version = getattr(dist, "version", None) or ""
        contributors.setdefault(module, (dist_name, version, module))

    # Stable, documented fold order: low→high by distribution name.
    ordered = sorted(contributors.values(), key=lambda c: c[0])

    fetched: dict[str, list[bytes]] = {}
    stamp: dict[str, str] = {}
    for dist_name, version, module in ordered:
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
            logger.warning("config seed from %r could not be read — skipping", module, exc_info=True)
            continue

    return fetched, stamp
