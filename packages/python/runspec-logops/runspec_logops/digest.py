import json
import sys

import runspec as rs
from runspec_logops_core import summarize_log
from runspec_logops_core.errors import SourceNotFoundError


def main_summarize_log() -> None:
    spec = rs.parse("summarize-log")
    file_path = str(spec.file.value)
    level = str(spec.level.value)
    # Unset optionals resolve to an Arg wrapping value None; read .value so the
    # absent case is a real None rather than the string "None".
    since = spec.since.value
    until = spec.until.value
    top = spec.top.value
    max_sample_lines = spec.max_sample_lines.value
    try:
        digest = summarize_log(
            file_path,
            level=level,
            since=since,
            until=until,
            top=top,
            max_sample_lines=max_sample_lines,
        )
        print(json.dumps(digest))
    except SourceNotFoundError as e:
        print(json.dumps({"error": str(e), "file": file_path}))
        sys.exit(1)
    except OSError as e:
        print(json.dumps({"error": str(e), "file": file_path}))
        sys.exit(1)
