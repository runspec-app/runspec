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
| Request types by name | `create-request`, `update-request`, `describe-request-types` — one call, friendly field names |
| Project admin | `list-project-roles`, `get-role-members`, `add-to-role`, `remove-from-role`, `check-user-access` |
| Customers | `list-customers`, `add-customers`, `remove-customers`, `list-organizations`, `add-to-organization` |
| Attachments | `list-attachments`, `download-attachments`, `add-attachment`, `remove-attachment` |

### Compact output

`get-issue`, `search` and `get-project-issues` return a compact view by default,
so an agent gets what it needs without a 20 KB API payload:

```bash
jira get-issue --key PROJ-1                          # essentials + counts of comments, links, …
jira get-issue --key PROJ-1 --include comments,links # add parts
jira get-issue --key PROJ-1 --raw                    # the raw Jira REST response
jira search --jql 'project = PROJ' --start 25        # compact rows; truncated / next_start for paging
```

In an interactive terminal these print readable text. Agents, pipes, the
console's Forms tab and schedulers get JSON.

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
In a venv, set these in its `.runspec_env` (the console's Env files window
lists them). `--jira-url`, `--jira-user`, `--jira-password` (password or
API token) and `--jira-pat` override the environment; the password and PAT
args are `password` type, so they come through the runspec-console secret
channel and never on a command line.

Each auth arg declares its variables as `env`, so a runspec-console credential
applies with no extra configuration: one labelled `Jira` of the username +
password kind sets `JIRA_USERNAME`/`JIRA_USER` and `JIRA_PASSWORD`, and one of the
token kind sets `JIRA_PAT`/`JIRA_TOKEN` (read as the Personal Access Token).

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

### Raising and editing requests in one call

`raise-request` needs a request type id and a JSON object keyed by
`customfield_*` ids in JSM's wire shapes, so an agent first calls
`list-request-types` and `list-request-type-fields`. `create-request` /
`update-request` skip that — name the project and request type, key fields by
name or portal label, and pass plain values:

```
jira create-request --project HR --request-type "New starter" \
  --values '{"Summary": "Welcome Sam", "Office": "Leeds", "Equipment": ["Laptop"], "Location": "UK > Leeds"}'
jira update-request --key HR-42 --values '{"Office": "London"}'
```

A wrong field or option comes back with the closest match and the request type's
full field list, so a second attempt is right without a discovery call.

### A department package with typed verbs

For the handful of projects a department uses every day, generate one typed verb
per request type into your own wrapper package, so the agent sees each form as
the tool's input schema (options as `choice`, required fields marked) and raises
in one call with no discovery at all. Other projects stay reachable through the
generic verbs above.

```
python -m runspec_jira_core.codegen --runnable mydept \
  --project HR --project PAY --project FAC --project IT --project SEC --project PROC \
  --toml mydept/runspec.toml --manifest mydept/jsm_request_types.json
```

This splices `[mydept.commands.raise-hr-new-starter]`-style tables between
markers in your `runspec.toml` (hand-written verbs outside the markers are kept)
and writes a manifest to ship as package data. `--request-type '*laptop*'`
narrows the set; `--check` in CI fails when a form changed in Jira. Dispatch:

```python
from importlib.resources import files
from runspec_jira_core import load_manifest, manifest_arg_names, run_generated_command, update_request, manifest_signatures

MANIFEST = load_manifest(files("mydept") / "jsm_request_types.json")

def dispatch(spec, cfg):
    cmd = spec.runspec_command
    if cmd in MANIFEST["commands"]:
        values = {name: spec.get(name).value for name in manifest_arg_names(MANIFEST, cmd)}
        return run_generated_command(cfg, MANIFEST, cmd, values, on_behalf_of=spec.get("on-behalf-of").value)
    if cmd == "update-request":  # edits resolve offline for the generated types
        return update_request(cfg, spec.key.value, spec.values.value, signatures=manifest_signatures(MANIFEST))
    ...
```

Request type and option ids differ between instances: when `--env` points at an
instance other than the one the manifest was generated from, the raise is
re-resolved by project + request type name.

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```
