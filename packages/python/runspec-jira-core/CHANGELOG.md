# Changelog

All notable changes to `runspec-jira-core` are documented here.

## [0.2.0] — 2026-08-07

Add issue attachment helpers over the SDK's attachment methods:
`list_attachments`, `download_attachments`, `add_attachment`, `remove_attachment`.
Binary content is path-in / path-out — downloads write to a destination
directory, uploads read a local file path.

## [0.1.0] — 2026-08-07

Initial release. Pure-Python Jira Software/Core + Service Desk helpers over
`atlassian-python-api` — the importable core behind `runspec-jira`. No `runspec`
dependency, no `runspec.toml`, no entry points (surfaces zero runnables).

- `JiraConfig` + `config_from_env(env=None)` — env-var auth (`JIRA_URL`,
  `JIRA_USERNAME`/`JIRA_API_TOKEN` for Cloud/DC basic, `JIRA_PERSONAL_TOKEN` for
  DC PAT) with `JIRA_<ENV>_*` prefixes selected by `--env dev|uat|prod`. Cloud vs
  Server/Data Center resolved via `JIRA_CLOUD` or a `.atlassian.net` host.
- `build_client` / `build_servicedesk` construct `atlassian.Jira` /
  `atlassian.ServiceDesk`, picking exactly one auth mode.
- Operation helpers grouped by area: issues (get/create/update/assign/delete),
  search (`jql_search`, `get_project_issues`), transitions, comments, links
  (issue links, epic link, remote link), worklog, projects (discovery +
  create-field metadata), users, field discovery, watchers, and service desk (request
  types, request-type fields, create customer request, get request).
- `translate_errors` surfaces the SDK's `requests` exceptions as `JiraError`
  carrying Jira's own error message; `parse_json_arg` parses JSON-string args.

Deferred: attachments/media, images, OAuth 2.0, transition-by-id with screen
fields, cross-project issue move.
