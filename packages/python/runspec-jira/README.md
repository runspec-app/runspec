# runspec-jira

A full-featured Jira runnable for [runspec](https://pypi.org/project/runspec/),
ported from the [Atlassian MCP server](https://github.com/sooperset/mcp-atlassian)
onto `atlassian-python-api` — covering Jira Software/Core **and** Service Desk
(JSM), on **Cloud and Server/Data Center**. One runnable — **`jira`** — with ~37
verbs. `pip install` it into a venv and it's discoverable by `runspec local`,
`runspec serve` (MCP), and runspec-console.

`runspec serve` flattens each verb into a leaf MCP tool (`jira_get-issue`, …).
Read verbs are `autonomy = "autonomous"`; writes are `autonomy = "confirm"`.

> The narrower one-runnable-per-project JSM option, [`runspec-jsm`](../runspec-jsm),
> is unchanged — use this package when you want the full Jira surface.

## Install

```
pip install runspec-jira
```

## Verbs

| Area | Verbs |
|---|---|
| Issues | `get-issue`, `create-issue`, `update-issue`, `assign-issue`, `delete-issue` |
| Search | `search` (JQL), `get-project-issues` |
| Transitions | `list-transitions`, `transition-issue` |
| Comments | `add-comment`, `edit-comment` |
| Links | `get-link-types`, `create-issue-link`, `link-to-epic`, `create-remote-issue-link`, `remove-issue-link` |
| Worklog | `get-worklog`, `add-worklog` |
| Projects | `get-all-projects`, `search-projects`, `get-project-components`, `get-project-issue-types`, `get-create-fields` |
| Fields / users | `search-fields`, `get-user-profile`, `search-assignable-users` |
| Watchers | `get-watchers`, `add-watcher`, `remove-watcher` |
| Service desk | `list-request-types`, `list-request-type-fields`, `raise-request`, `get-request` |
| Attachments | `list-attachments`, `download-attachments`, `add-attachment`, `remove-attachment` |

## Auth — env vars only

```
export JIRA_URL=https://your-org.atlassian.net
# Cloud (API token):
export JIRA_USERNAME=you@example.com
export JIRA_API_TOKEN=<api-token>
# …or Server/DC (Personal Access Token):
export JIRA_PERSONAL_TOKEN=<pat>
```

Cloud vs Server/DC is detected from a `.atlassian.net` host or set with
`JIRA_CLOUD`. Every verb takes `--env`, selecting `JIRA_<ENV>_*` prefixed vars.
The PAT can instead ride the runspec-console secret channel via the optional
`--jira-pat` (a `password`-typed arg, refused on the CLI, kept out of logs).
JSON-object args (`--additional-fields`, `--fields`, `--visibility`,
`--worklog-json`, `--field-values`) are passed as strings and parsed in the core.

```
jira search --jql "project = OPS AND status = 'In Progress'"
jira create-issue --project OPS --summary "Disk full on db-01" --issue-type Incident
jira transition-issue --issue OPS-42 --transition Done
```

## Public Python API

The pure logic is in **[`runspec-jira-core`](../runspec-jira-core)** — over
`atlassian-python-api`, no `runspec` dependency, no entry points. Every op takes
a `JiraConfig` first arg and raises `JiraError`, so a private wrapper can inject
credentials and bake in a default `project_key` / `service_desk_id` / `env`:

```python
from runspec_jira_core import config_from_env, build_client, get_issue

cfg = config_from_env(env="prod")
issue = get_issue(cfg, "OPS-42")
```

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```
