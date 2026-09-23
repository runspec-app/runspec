"""runspec-rundeck-core — pure-Python Rundeck API helpers.

The importable logic behind `runspec-rundeck`: configuration, an httpx request
helper, and the job/project operations (import, list, delete, prune, and the
`sync_target` composition). No `runspec` dependency, no `runspec.toml`, and no
entry points, so installing it exposes the helpers without surfacing any
runnables — a corporate wrapper can depend on this alone, inject credentials,
and bake in an instance.
"""

from runspec_rundeck_core.client import (
    DEFAULT_API_VERSION,
    RundeckConfig,
    RundeckError,
    config_from_env,
    rundeck_request,
)
from runspec_rundeck_core.jobs import (
    ImportResult,
    SyncResult,
    delete_job,
    import_error_details,
    import_jobs,
    list_jobs,
    prune_jobs,
    summarize_import,
    sync_target,
)
from runspec_rundeck_core.projects import create_project, ensure_project, project_exists

__all__ = [
    "DEFAULT_API_VERSION",
    "RundeckConfig",
    "RundeckError",
    "config_from_env",
    "rundeck_request",
    "ImportResult",
    "SyncResult",
    "import_jobs",
    "list_jobs",
    "delete_job",
    "prune_jobs",
    "sync_target",
    "summarize_import",
    "import_error_details",
    "project_exists",
    "create_project",
    "ensure_project",
]
