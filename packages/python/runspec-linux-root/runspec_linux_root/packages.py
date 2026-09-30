"""Package-manager runnables that need root (apt/dnf/yum/zypper/pacman)."""

import json
import sys

import runspec as rs
from runspec_linux_core import autoremove_packages, install_package, list_upgrades, remove_package, upgrade_packages
from runspec_linux_core.errors import CommandError


def main_list_upgrades() -> None:
    spec = rs.parse("list-upgrades")
    try:
        print(json.dumps(list_upgrades(refresh=not spec.no_refresh.value)))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_upgrade_packages() -> None:
    rs.parse("upgrade-packages")
    try:
        print(json.dumps(upgrade_packages()))
    except CommandError as e:
        print(json.dumps({"upgraded": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ToolNotFoundError (no package manager).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_install_package() -> None:
    spec = rs.parse("install-package")
    package = spec.package.value
    try:
        print(json.dumps(install_package(package)))
    except CommandError as e:
        print(json.dumps({"package": package, "installed": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ValueError (bad name) and ToolNotFoundError (no package manager).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_remove_package() -> None:
    spec = rs.parse("remove-package")
    package = spec.package.value
    try:
        print(json.dumps(remove_package(package, purge=spec.purge.value)))
    except CommandError as e:
        print(json.dumps({"package": package, "removed": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ValueError (bad name) and ToolNotFoundError (no package manager).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_autoremove_packages() -> None:
    spec = rs.parse("autoremove-packages")
    try:
        print(json.dumps(autoremove_packages(purge=spec.purge.value, dry_run=spec.dry_run.value)))
    except CommandError as e:
        print(json.dumps({"removed": False, "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        # Includes ToolNotFoundError (no package manager).
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
