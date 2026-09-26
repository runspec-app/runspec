"""runspec-linux-core — pure-Python Linux system-admin helpers.

This package has **no dependency on runspec** and ships **no runspec.toml and no
entry points**, so installing it exposes the helper functions for import without
surfacing any runnables (it is invisible to ``runspec local`` / ``runspec serve``
discovery). ``runspec-linux`` depends on it and wraps each helper in a runnable.

Each function does the work and returns plain data; failures raise (see
``runspec_linux_core.errors``).
"""

from runspec_linux_core.become import (
    BecomeError,
    build_become,
    check_become_error,
    current_user,
    escalation_supported,
    validate_user,
)
from runspec_linux_core.commands import which
from runspec_linux_core.containers import (
    container_logs,
    list_containers,
    restart_container,
)
from runspec_linux_core.cron import (
    add_cron,
    clear_cron,
    list_cron,
    remove_cron,
    validate_schedule,
)
from runspec_linux_core.errors import CommandError, LinuxCoreError, ToolNotFoundError
from runspec_linux_core.files import backup_files, find_large_files, find_large_files_report
from runspec_linux_core.investigate import (
    failed_units,
    open_ports,
    owning_package,
    service_detail,
)
from runspec_linux_core.logs import journalctl, search_log, tail_log
from runspec_linux_core.nc import nc_send
from runspec_linux_core.network import (
    check_port,
    dns_config,
    network_interfaces,
    ping_host,
    route_table,
    show_connections,
    socket_stats,
    trace_route,
)
from runspec_linux_core.packages import (
    autoremove_packages,
    detect_manager,
    install_package,
    list_packages,
    list_upgrades,
    remove_package,
    upgrade_packages,
)
from runspec_linux_core.perf import cpu_info, get_sysctl, pressure, set_sysctl
from runspec_linux_core.power import reboot_host
from runspec_linux_core.security import last_logins, who
from runspec_linux_core.services import check_service, list_services, restart_service
from runspec_linux_core.sudoers import enable_passwordless_sudo
from runspec_linux_core.system import block_devices, check_memory, disk_usage, list_processes, system_info
from runspec_linux_core.venvs import configure_pip, create_venv, install_into_venv, share_venv_logs

__all__ = [
    # errors
    "LinuxCoreError",
    "ToolNotFoundError",
    "CommandError",
    # become (privilege escalation)
    "BecomeError",
    "build_become",
    "check_become_error",
    "current_user",
    "escalation_supported",
    "validate_user",
    # system
    "system_info",
    "disk_usage",
    "block_devices",
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
    "trace_route",
    "route_table",
    "socket_stats",
    "dns_config",
    "network_interfaces",
    # perf tuning & analysis
    "get_sysctl",
    "set_sysctl",
    "cpu_info",
    "pressure",
    # app discovery & investigation
    "service_detail",
    "open_ports",
    "owning_package",
    "failed_units",
    # files
    "find_large_files",
    "find_large_files_report",
    "backup_files",
    # security
    "last_logins",
    "who",
    # containers
    "list_containers",
    "container_logs",
    "restart_container",
    # cron
    "list_cron",
    "add_cron",
    "remove_cron",
    "clear_cron",
    "validate_schedule",
    # packages
    "detect_manager",
    "list_packages",
    "list_upgrades",
    "upgrade_packages",
    "install_package",
    "remove_package",
    "autoremove_packages",
    # power
    "reboot_host",
    # sudoers
    "enable_passwordless_sudo",
    # venvs
    "create_venv",
    "install_into_venv",
    "configure_pip",
    "share_venv_logs",
    # tcp
    "nc_send",
    # commands
    "which",
]
