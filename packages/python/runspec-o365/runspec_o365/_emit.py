"""_emit.py — shared JSON output + error handling for Graph runnables."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from runspec_o365 import GraphError


def fail(message: str, **extra: object) -> None:
    """Print ``{"error": message, **extra}`` as JSON and exit non-zero.

    A tiny local copy of ``runspec_windows._platform.fail`` — the Graph runnables
    were split out of runspec-windows into this package, and this was their only
    dependency on that package's Windows-specific ``_platform`` helper.
    """
    print(json.dumps({"error": message, **extra}))
    sys.exit(1)


def run_graph(produce: Callable[[], Any]) -> None:
    """Print ``produce()`` as JSON, turning Graph errors into a JSON error payload."""
    try:
        print(json.dumps(produce()))
    except GraphError as e:
        fail(str(e), kind=type(e).__name__)
    except Exception as e:  # network/JSON/etc.
        fail(str(e))
