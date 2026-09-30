import json
import sys

import runspec as rs
from runspec_linux_core import configure_pip, create_venv, install_into_venv, share_venv_logs
from runspec_linux_core.errors import CommandError

from runspec_linux._escalation import refuse_root


def main_create_venv() -> None:
    spec = rs.parse("create-venv")
    refuse_root(spec.run_as.value or None)
    try:
        result = create_venv(
            str(spec.python.value),
            str(spec.dest.value),
            run_as=spec.run_as.value or None,
            become_method=spec.become_method.value,
            group=spec.group.value or None,
            system_site_packages=spec.system_site_packages.value,
            upgrade_deps=spec.upgrade_deps.value,
            clear=spec.clear.value,
            prompt=spec.prompt.value or None,
            inbox=not spec.no_inbox.value,
        )
        print(json.dumps(result))
    except CommandError as e:
        print(json.dumps({"dest": str(spec.dest.value), "created": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ValueError (non-executable interpreter / bad name).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_install_into_venv() -> None:
    spec = rs.parse("install-into-venv")
    refuse_root(spec.run_as.value or None)
    try:
        result = install_into_venv(
            str(spec.venv.value),
            spec.package.value or "",
            project=str(spec.project.value) if spec.project.value else None,
            editable=spec.editable.value,
            upgrade=spec.upgrade.value,
            no_cache_dir=spec.no_cache_dir.value,
            force_reinstall=spec.force_reinstall.value,
            run_as=spec.run_as.value or None,
            become_method=spec.become_method.value,
        )
        print(json.dumps(result))
    except CommandError as e:
        print(json.dumps({"venv": str(spec.venv.value), "installed": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ValueError (bad package name / missing venv / nothing to install).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_share_venv_logs() -> None:
    spec = rs.parse("share-venv-logs")
    refuse_root(spec.run_as.value or None)
    try:
        result = share_venv_logs(
            str(spec.venv.value),
            group=spec.group.value or None,
            run_as=spec.run_as.value or None,
            become_method=spec.become_method.value,
            inbox=not spec.no_inbox.value,
        )
        print(json.dumps(result))
    except CommandError as e:
        print(json.dumps({"venv": str(spec.venv.value), "shared": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ValueError (bad venv path / group name).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_configure_pip() -> None:
    spec = rs.parse("configure-pip")
    refuse_root(spec.run_as.value or None)
    try:
        result = configure_pip(
            spec.index_url.value,
            extra_index_url=spec.extra_index_url.value or None,
            trusted_host=spec.trusted_host.value or None,
            cert=spec.cert.value or None,
            timeout=spec.timeout.value,
            retries=spec.retries.value,
            proxy=spec.proxy.value or None,
            require_virtualenv=spec.require_virtualenv.value,
            scope=spec.scope.value,
            venv=str(spec.venv.value) if spec.venv.value else None,
            run_as=spec.run_as.value or None,
            become_method=spec.become_method.value,
        )
        print(json.dumps(result))
    except CommandError as e:
        print(json.dumps({"configured": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ValueError (bad scope / missing venv path).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
