# Changelog

All notable changes to `runspec-gitlab-core` are documented here.

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
