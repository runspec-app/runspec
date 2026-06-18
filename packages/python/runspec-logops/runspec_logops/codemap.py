import json
import sys

import runspec as rs
from runspec_logops_core import map_trace_to_sources
from runspec_logops_core.errors import SourceNotFoundError


def main_map_trace() -> None:
    spec = rs.parse("map-trace")
    repo = str(spec.repo)
    trace = str(spec.trace)
    context = int(spec.context)
    max_files = int(spec.max_files)
    try:
        result = map_trace_to_sources(repo, trace, context=context, max_files=max_files)
        print(json.dumps(result))
    except SourceNotFoundError as e:
        print(json.dumps({"error": str(e), "repo": repo}))
        sys.exit(1)
    except OSError as e:
        print(json.dumps({"error": str(e), "repo": repo}))
        sys.exit(1)
