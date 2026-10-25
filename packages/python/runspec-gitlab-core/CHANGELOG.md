# Changelog

All notable changes to `runspec-gitlab-core` are documented here.

## [0.5.0] — 2026-10-25

- `search_projects` can list the token user's projects: `search` is now
  optional, and with no term the call asks for member projects only (listing
  every visible project on gitlab.com is no use). A term also matches the
  group/namespace path (`search_namespaces`), so a username or group name finds
  the projects under it. New `owned` parameter limits results to owned projects.

## [0.4.0] — 2026-10-25

- `config_from_env` reads the token from `GITLAB_TOKEN` or `GITLAB_PAT` (each
  also under the `GITLAB_<ENV>_` prefix) — the two names a runspec-console token
  credential labelled `GitLab` sets — and takes `url` / `token` keyword
  overrides that win over the environment.
- The "No token configured" error also names the console credential route.

## [0.3.0]

Write helpers, each taking a `GitlabConfig` first like the reads.

- **Issues:** `create_issue`, `update_issue`, `add_issue_note`.
- **Merge requests:** `create_merge_request`, `update_merge_request`,
  `add_merge_request_note`, `merge_merge_request`.
- **Repository:** `create_branch`, `write_file` (checks whether the file exists
  on the starting branch, then creates or updates it; returns `action`
  `created`/`updated`).
- **Pipelines/jobs:** `create_pipeline`, `retry_pipeline`, `cancel_pipeline`,
  `retry_job`, `cancel_job`, `play_job` (variables as `KEY=VALUE` strings).
- **Deletes:** `delete_issue`, `delete_branch`, `delete_file`,
  `delete_pipeline` (each returns a small `{"deleted": ...}` summary).
- `resolve_user_ids` maps usernames to the numeric ids the write APIs take.
- `gitlab_request` gains a `body=` JSON request body (its `None` values are
  dropped), and `GitlabError` now carries the HTTP `status_code` of an API
  error (`None` for a configuration error).
- `update_issue`/`update_merge_request` raise `ValueError` when no field is
  given, rather than sending an empty edit.

## [0.2.0]

Reconciled against GitLab's canonical MCP tool set.

- **Renamed** `get_merge_request_changes` → `get_merge_request_diffs` — the
  canonical name; targets the paginated `/diffs` endpoint instead of the
  deprecated `/changes`. **Breaking** API change vs 0.1.0.
- **Added** read helpers: `get_merge_request_commits`, `get_merge_request_notes`,
  `get_merge_request_pipelines`, `get_merge_request_conflicts`, and
  `resolve_project_path`.
- `gitlab_request` gains an `api=False` mode to target `{base_url}{path}` (no
  `/api/v4` prefix) — used only for the non-public MR conflicts web endpoint.
- `get_merge_request_conflicts` is **best-effort**: it relays GitLab's
  non-public web-UI conflicts endpoint (there is no public `/api/v4` one) and
  returns its JSON verbatim, resolving a numeric project id to its
  `namespace/path` via `resolve_project_path`.

## [0.1.0]

Initial release. Pure-Python GitLab REST (v4) read helpers — the importable core
behind `runspec-gitlab`, with **no runspec dependency, no runspec.toml, and no
entry points** (so it surfaces no runnables and stays importable on its own).

- `GitlabConfig` + `config_from_env` — env-var driven connection/identity
  (`GITLAB_URL` defaulting to `https://gitlab.com`, `GITLAB_TOKEN`, optional
  `GITLAB_PROJECT`), with `GITLAB_<ENV>_*` prefixes selected by `env`.
- `gitlab_request` / `encode_project` — authenticated `PRIVATE-TOKEN` request
  helper against `{base_url}/api/v4`, raising `GitlabError` on non-2xx.
- Read helpers ported from the read tools of GitLab's hosted MCP server:
  `whoami`, `get_project`, `search_projects`, `list_issues`, `get_issue`,
  `list_merge_requests`, `get_merge_request`, `get_merge_request_changes`,
  `list_pipelines`, `get_pipeline_jobs`, `get_job_log`, `get_file`, `list_tree`.
