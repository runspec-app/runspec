# Changelog

All notable changes to `runspec-gitlab-core` are documented here.

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
