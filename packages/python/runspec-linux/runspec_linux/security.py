import json
import sys

import runspec as rs
from runspec_linux_core import last_logins, who


def main_last_logins() -> None:
    spec = rs.parse("last-logins")
    limit = spec.limit.value
    try:
        print(json.dumps(last_logins(limit)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_who() -> None:
    rs.parse("who")
    try:
        print(json.dumps(who()))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
