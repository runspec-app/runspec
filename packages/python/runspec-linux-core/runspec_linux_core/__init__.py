"""runspec-linux-core — pure-Python Linux system-admin helpers.

This package has **no dependency on runspec** and ships **no runspec.toml and no
entry points**, so installing it exposes the helper functions for import without
surfacing any runnables (it is invisible to ``runspec local`` / ``runspec serve``
discovery). ``runspec-linux`` depends on it and wraps each helper in a runnable.

Each function does the work and returns plain data; failures raise (see
``runspec_linux_core.errors``).
"""

from runspec_linux_core.commands import which
from runspec_linux_core.containers import (
    container_logs,
    list_containers,
    restart_container,
)
from runspec_linux_core.errors import CommandError, LinuxCoreError, ToolNotFoundError
from runspec_linux_core.files import backup_files, find_large_files
from runspec_linux_core.logs import journalctl, search_log, tail_log
from runspec_linux_core.nc import nc_send
from runspec_linux_core.network import check_port, ping_host, show_connections
from runspec_linux_core.packages import (
    detect_manager,
    install_package,
    list_packages,
    list_upgrades,
    remove_package,
    upgrade_packages,
)
from runspec_linux_core.power import reboot_host
from runspec_linux_core.security import last_logins, who
from runspec_linux_core.services import check_service, list_services, restart_service
from runspec_linux_core.sudoers import enable_passwordless_sudo
from runspec_linux_core.system import check_memory, disk_usage, list_processes, system_info
from runspec_linux_core.venvs import configure_pip, create_venv, install_into_venv

__all__ = [
    # errors
    "LinuxCoreError",
    "ToolNotFoundError",
    "CommandError",
    # system
    "system_info",
    "disk_usage",
    "check_memory",
    "list_processes",
    # services
    "check_service",
    "list_services",
    "restart_service",
    # logs
    "tail_log",
    "search_log",
    "journalctl",
    # network
    "ping_host",
    "check_port",
    "show_connections",
    # files
    "find_large_files",
    "backup_files",
    # security
    "last_logins",
    "who",
    # containers
    "list_containers",
    "container_logs",
    "restart_container",
    # packages
    "detect_manager",
    "list_packages",
    "list_upgrades",
    "upgrade_packages",
    "install_package",
    "remove_package",
    # power
    "reboot_host",
    # sudoers
    "enable_passwordless_sudo",
    # venvs
    "create_venv",
    "install_into_venv",
    "configure_pip",
    # tcp
    "nc_send",
    # commands
    "which",
]
