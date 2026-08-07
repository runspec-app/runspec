# Changelog

All notable changes to `runspec-gitlab` are documented here.

## [0.1.0]

Initial release. One runnable, `gitlab`, with a read verb per tool of the
[GitLab MCP server](https://docs.gitlab.com/user/model_context_protocol/mcp_server/),
**ported onto the GitLab REST API (v4)** rather than proxying that server — so it
runs over the runspec-console SSH fleet with no extra process. `runspec serve`
re-expands each verb into its own leaf MCP tool (`gitlab_get-issue`,
`gitlab_list-pipelines`, …).

- Verbs (all `autonomy = "autonomous"` — read-only): `whoami`, `get-project`,
  `search-projects`, `list-issues`, `get-issue`, `list-merge-requests`,
  `get-merge-request`, `get-merge-request-changes`, `list-pipelines`,
  `get-pipeline-jobs`, `get-job-log`, `get-file`, `list-tree`.
- Global `--env` (dev/uat/prod) selects `GITLAB_<ENV>_*` env vars; auth is
  `GITLAB_URL` (default `https://gitlab.com`) + `GITLAB_TOKEN`. `project` accepts
  a numeric id or `group/project` path and falls back to `GITLAB_PROJECT`.
- Thin wrapper over `runspec-gitlab-core` (the dependency-free REST logic core).
