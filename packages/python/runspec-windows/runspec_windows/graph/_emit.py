"""_emit.py — shared JSON output + error handling for Graph runnables."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from runspec_windows import _platform
from runspec_windows.graph import GraphError


def run_graph(produce: Callable[[], Any]) -> None:
    """Print ``produce()`` as JSON, turning Graph errors into a JSON error payload."""
    try:
        print(json.dumps(produce()))
    except GraphError as e:
        _platform.fail(str(e), kind=type(e).__name__)
    except Exception as e:  # network/JSON/etc.
        _platform.fail(str(e))
