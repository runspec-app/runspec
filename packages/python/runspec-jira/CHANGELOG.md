# Changelog

All notable changes to `runspec-jira` are documented here.

## [0.2.0] — 2026-08-07

Add four attachment verbs to the `jira` runnable: `list-attachments`
(autonomous), and `download-attachments` / `add-attachment` / `remove-attachment`
(confirm). `download-attachments --dest <dir>` writes an issue's attachments to a
local directory; `add-attachment --file <path>` uploads a local file. Requires
`runspec-jira-core >= 0.2.0`.

## [0.1.0] — 2026-08-07

Initial release. A full-featured `jira` runnable ported from the Atlassian MCP
server (sooperset/mcp-atlassian) onto `atlassian-python-api`, covering Jira
Software/Core **and** Service Desk (JSM) on Cloud and Server/Data Center. Thin
wrapper over `runspec-jira-core`.

- One runnable, `jira`, with a verb per tool; `runspec serve` re-expands each into
  a leaf MCP tool (`jira_get-issue`, …). Reads are `autonomous`, writes `confirm`.
- Verbs: issues (get/create/update/assign/delete), search (`search` JQL,
  `get-project-issues`), transitions (list/transition), comments (add/edit), links
  (link types, issue link, epic link, remote link, remove), worklog (get/add),
  projects (get-all/search/components/issue-types/create-fields), field discovery
  (`search-fields`), users (profile/assignable), watchers (get/add/remove), and
  service desk (`list-request-types`, `list-request-type-fields`, `raise-request`,
  `get-request`).
- Auth is env-var driven (`JIRA_URL` + `JIRA_PERSONAL_TOKEN` or
  `JIRA_USERNAME`/`JIRA_API_TOKEN`), with `JIRA_<ENV>_*` prefixes selected by the
  global `--env`. Cloud vs Server/DC via `JIRA_CLOUD` or a `.atlassian.net` host.
- JSON-object arguments (`--additional-fields`, `--fields`, `--visibility`,
  `--worklog-json`, `--field-values`) are passed as strings and parsed in the core.

The existing `runspec-jsm` package is unchanged; it remains the narrow
"one runnable per project" JSM service-desk option. Deferred here: attachments/
media, images, OAuth 2.0, transition-by-id with screen fields, cross-project move.
