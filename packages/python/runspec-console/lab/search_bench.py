"""
search_bench.py — how well ``search_runnables`` finds the right runnable.

Loads every ``runspec.toml`` shipped in this repo (the integration packages:
linux, windows, jira, gitlab, …) the way discovery does, puts them on one fake
host, and runs each query through the real ``Bridge._search_runnables``. A
query scores by the rank of the first expected runnable in the results.

Two query sets: ``search_queries.toml`` (written alongside the ranking change)
and ``search_queries_heldout.toml`` (written separately, from the runspec.toml
files only, in everyday ops wording). Judge a ranking change on both — the
held-out set is the one it wasn't tuned on.

No model, no network, same numbers every run — it measures the ranking only.
Whether the agent then picks the right tool is what the Evals tab measures
with a live model.

    # from packages/python/runspec-console, in a venv with the console installed
    python lab/search_bench.py              # both sets: summary + the misses
    python lab/search_bench.py --all        # every query with its top results
    python lab/search_bench.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

HERE = Path(__file__).resolve().parent
PACKAGES = HERE.parent.parent  # packages/python
# Packages that ship runnables a real fleet installs. The console's own
# runspec.toml is its CLI, and the lab fakes are test doubles.
SKIP = {"runspec-console"}


def load_corpus() -> list[dict[str, Any]]:
    """Every runnable from the repo's packages, as discovery records on host
    ``local`` with the package name as the group."""
    from runspec_console import discovery

    out: list[dict[str, Any]] = []
    for path in sorted(PACKAGES.glob("*/runspec_*/runspec.toml")):
        pkg = path.parent.parent.name
        if pkg in SKIP:
            continue
        raw = discovery._normalise_toml_text(path.read_text(encoding="utf-8"))
        if not raw:
            continue
        auto = raw["config"].get("autonomy_default")
        for name, spec in raw["runnables"].items():
            out.extend(discovery._spec_to_runnables(name, spec, "local", pkg, auto))
    return out


def make_bridge(runnables: list[dict[str, Any]]) -> Any:
    """A Bridge with just enough state for ``_search_runnables``."""
    from runspec_console.bridge import Bridge

    b = Bridge.__new__(Bridge)
    b._lock = threading.Lock()
    b._runnables_cache = runnables
    b._credentials = []
    b._reload_credentials = lambda: None  # type: ignore[method-assign]
    b._become_params = lambda rec, host: ("", "sudo", None)  # type: ignore[method-assign]
    b._wiki_notes = lambda names, per_entity=0: {}  # type: ignore[method-assign]
    b.get_config = lambda: {"llm": {"tool_mode": "search"}, "ssh": {}}  # type: ignore[method-assign]
    return b


def run(queries: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    bridge = make_bridge(load_corpus())
    results = []
    for item in queries:
        q, expect = item["q"], set(item["expect"])
        out = bridge._search_runnables(q, limit=limit)
        names = [r["name"] for r in out["results"]]
        rank = next((i + 1 for i, n in enumerate(names) if n in expect), None)
        results.append({"q": q, "expect": sorted(expect), "rank": rank, "top": names})
    return results


def summarise(results: list[dict[str, Any]]) -> dict[str, float]:
    n = len(results)
    ranks = [r["rank"] for r in results]

    def hit(k: int) -> float:
        return sum(1 for x in ranks if x is not None and x <= k) / n

    return {
        "queries": n,
        "hit@1": hit(1),
        "hit@3": hit(3),
        "hit@10": hit(10),
        "mrr": sum(1 / x for x in ranks if x) / n,
        "no_results": sum(1 for r in results if not r["top"]) / n,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--queries",
        action="append",
        help="query file (repeatable; default: both sets in lab/)",
    )
    ap.add_argument("--all", action="store_true", help="print every query")
    ap.add_argument("--json", help="write per-query results to this file")
    ns = ap.parse_args(argv)
    files = ns.queries or [
        str(HERE / "search_queries.toml"),
        str(HERE / "search_queries_heldout.toml"),
    ]

    # The search also reads saved runbooks from the config dir; keep the
    # operator's own out of the numbers.
    report: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["APPDATA"] = tmp
        os.environ["XDG_CONFIG_HOME"] = tmp
        for path in files:
            with open(path, "rb") as f:
                queries = tomllib.load(f)["query"]
            results = run(queries)
            s = summarise(results)
            report[Path(path).name] = {"summary": s, "results": results}
            print(
                f"{Path(path).name}: {s['queries']} queries  hit@1 {s['hit@1']:.0%}  "
                f"hit@3 {s['hit@3']:.0%}  hit@10 {s['hit@10']:.0%}  "
                f"MRR {s['mrr']:.3f}  no results {s['no_results']:.0%}"
            )
            for r in results:
                if ns.all or r["rank"] != 1:
                    rank = r["rank"] or "-"
                    print(
                        f"  [{rank:>2}] {r['q']!r} want {r['expect']} "
                        f"got {r['top'][:3]}"
                    )
    if ns.json:
        Path(ns.json).write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
