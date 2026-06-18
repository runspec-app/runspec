import json
import sys

import runspec as rs
from runspec_logops_core import build_bundle, map_trace_to_sources, summarize_log
from runspec_logops_core.errors import SourceNotFoundError


def main_bundle_digest() -> None:
    spec = rs.parse("bundle-digest")
    file_path = str(spec.file)
    # Unset optionals resolve to an Arg wrapping value None; read .value so the
    # absent case is a real None rather than the string "None".
    repo = str(spec.repo) if spec.repo.value is not None else None
    trace = str(spec.trace) if spec.trace.value is not None else None
    destination = str(spec.destination)
    top = int(spec.top)
    try:
        digest = summarize_log(file_path, top=top)
        code_map = None
        if trace and repo:
            code_map = map_trace_to_sources(repo, trace)
        result = build_bundle(digest, code_map, dest_dir=destination, metadata={"trace": trace})
        print(json.dumps(result))
    except SourceNotFoundError as e:
        print(json.dumps({"error": str(e), "file": file_path}))
        sys.exit(1)
    except OSError as e:
        print(json.dumps({"error": str(e), "file": file_path}))
        sys.exit(1)
