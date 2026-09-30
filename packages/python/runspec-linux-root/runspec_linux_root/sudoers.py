import json
import sys

import runspec as rs
from runspec_linux_core import enable_passwordless_sudo
from runspec_linux_core.errors import CommandError


def main_enable_passwordless_sudo() -> None:
    spec = rs.parse("enable-passwordless-sudo")
    password = spec.password.value
    user = spec.user.value
    scope = spec.scope.value
    try:
        print(json.dumps(enable_passwordless_sudo(password, user, scope)))
    except CommandError as e:
        # Auth failure / sudoers validation failure (rolled back).
        print(json.dumps({"installed": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ValueError (bad scope/user) and ToolNotFoundError (no sudo/visudo).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
