"""runspec-scheduler — a cron-style scheduler service for runspec runnables.

A long-running per-venv daemon (``systemctl --user``) that runs runnables
installed in *its own venv* on a cron/interval cadence, independent of any
desktop console. It is itself a runspec runnable with management verbs
(``run`` / ``add`` / ``remove`` / ``list`` / ``run-now`` / ``install-service``).

Public API (for corporate wrappers / tests): the pure config + timing helpers,
which have no runspec-console dependency.
"""

from __future__ import annotations

from .config import (
    ScheduleEntry,
    config_path,
    load_schedules,
    read_config,
    upsert_schedule,
    write_schedules,
)
from .timing import When, next_fire_time, parse_when

__all__ = [
    "ScheduleEntry",
    "When",
    "config_path",
    "load_schedules",
    "next_fire_time",
    "parse_when",
    "read_config",
    "upsert_schedule",
    "write_schedules",
]

__version__ = "0.1.0"
