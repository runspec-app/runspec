"""runspec-rundeck — sync a venv's runspec runnables to Rundeck as jobs.

A thin runnable over `runspec-rundeck-core`: `sync` emits a Rundeck job per
runnable (via `runspec emit --rundeck`) and imports them over the REST API,
`push` imports pre-emitted job files, and `list` reads a project's jobs. The
`rundeck` entry point is wired in `cli.main`.
"""

from runspec_rundeck.cli import main
from runspec_rundeck.emit import (
    EmitError,
    discover_visibility,
    emit_jobs,
    filter_visible,
    load_job_files,
    owning_runnable,
)

__all__ = [
    "main",
    "EmitError",
    "emit_jobs",
    "discover_visibility",
    "filter_visible",
    "owning_runnable",
    "load_job_files",
]
