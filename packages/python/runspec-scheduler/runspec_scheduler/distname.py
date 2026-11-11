"""distname.py — the distribution this copy of runspec-scheduler was installed as.

A company can republish the wheel under its own name for a private index
(``runspec rebrand runspec-scheduler mycorp-scheduler``): the code, the
``runspec-scheduler`` command and the ``runspec_scheduler`` import package stay
the same, only the distribution name changes. Anything that reads the version
or tells someone what to ``pip install`` must use the name this copy really
has, or it reads nothing (the version) or points at a package the private
index may not carry (the hints).
"""

from __future__ import annotations

import functools

#: The upstream distribution name.
DEFAULT = "runspec-scheduler"


@functools.lru_cache(maxsize=1)
def dist_name() -> str:
    """The installed distribution that provides ``runspec_scheduler`` —
    ``runspec-scheduler`` upstream, the new name after a rebrand. When two are
    installed (an upstream copy over a rebranded one) the rebranded name wins,
    since that is the one the private index serves."""
    try:
        from importlib.metadata import packages_distributions

        names = packages_distributions().get("runspec_scheduler") or []
    except Exception:  # noqa: BLE001 — a broken environment shouldn't stop the scheduler
        names = []
    names = list(dict.fromkeys(names))
    others = [n for n in names if _canonical(n) != DEFAULT]
    return others[0] if others else DEFAULT


def _canonical(name: str) -> str:
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


def pip_spec(extra: str = "") -> str:
    """``<dist>[extra]`` quoted for a shell hint."""
    return f"'{dist_name()}[{extra}]'" if extra else dist_name()
