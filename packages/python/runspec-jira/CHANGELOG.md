# Changelog

All notable changes to `runspec-jira` are documented here.

## [0.5.1] — 2026-10-18

### Added

- **Mock responses** (`runspec_jira/mocks/jira.json`) for `get-issue`,
  `search`, `list-transitions`, `transition-issue`, `add-comment` and
  `get-all-projects`, so `RUNSPEC_MOCK=1 jira …` (runspec 0.74.0+) answers with
  realistic output and no Jira. The data is made up: it is recorded from the
  real code against a fake Jira by `scripts/record_mocks.py`.

## [0.5.0] — 2026-10-16

### Changed

- **`get-issue`, `search` and `get-project-issues` return compact output by
  default**, so an agent reading them uses a fraction of the context it did.
  `get-issue` gives the essentials plus counts of comments, links, subtasks and
  attachments. `--include comments,links,…` adds parts, and `--raw` (or
  `--fields` / `--expand`, as before) returns the raw API response. `search` and
  `get-project-issues` return compact rows with `total`, `truncated` and
  `next_start`. Their default `limit` drops from 50 to 25, and
  `get-project-issues` gains `--start`. Use `--raw` for the old output.
- **Readable text in a terminal.** Run by a person in an interactive terminal,
  `get-issue` prints `Label  value` lines and the list verbs print a table.
  Agents, pipes, the console's Forms tab and schedulers still get JSON.

## [0.4.0] — 2026-09-26

### Added

- **Request types by name** — `create-request --project HR --request-type "New
  starter" --values '{"Summary": …, "Office": "Leeds"}'` raises a request in one
  call (fields keyed by name or portal label, plain values; ids and wire shapes
  resolved by the core), `update-request --key HR-9 --values …` edits one the
  same way, and `describe-request-types --project …` lists the forms when an
  agent needs them. A wrong field or option returns the request type's field list.
- **Project admin verbs** — `list-project-roles`, `get-role-members`,
  `add-to-role`, `remove-from-role`, `list-customers`, `add-customers`,
  `remove-customers`, `list-organizations`, `add-to-organization`, and
  `check-user-access` (licence + roles + findings in one call). Reads are
  `autonomous`, changes `confirm`.

### Changed

- Requires `runspec-jira-core >= 0.4.0`.

## [0.3.1] — 2026-09-20

### Changed

- **Packaging metadata for the runspec-console Marketplace.** Add a README
  (rendered in the Marketplace card's Details view) and publish `keywords`
  and Documentation / Source / Changelog / Issues project URLs, so the
  package's Marketplace card shows a description, tags and links. No
  runnable, argument, or behaviour changes.

## [0.3.0] — 2026-08-07

Add an optional `--jira-pat` secret arg (a `password`-typed inherited global) for
the Jira Personal Access Token, so a credential can be supplied through
runspec-console (via the `RUNSPEC_JIRA_ARG_JIRA_PAT` secret channel) instead of the
`JIRA_PERSONAL_TOKEN` env var. The arg is named `jira-pat` (not `pat`) so
runspec-console derives a distinct env var from it. When set it overrides env credentials. Being a `password` arg it is
refused on the command line, kept out of logs/audit, and omitted from the agent's
MCP tool schemas. Requires `runspec-jira-core >= 0.3.0`.

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
