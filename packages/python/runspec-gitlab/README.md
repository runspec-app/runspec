# runspec-gitlab

A GitLab runnable for [runspec](https://pypi.org/project/runspec/), ported onto
the GitLab REST API (v4) — **not** proxying GitLab's hosted MCP server. One
runnable — **`gitlab`** — with 19 read verbs (`autonomy = "autonomous"`), 15
write verbs (`autonomy = "confirm"`, so an agent asks before each one) and 4
delete verbs (`autonomy = "manual"`, so only a person can run them). `pip install` it into a venv and it's discoverable by `runspec
local`, `runspec serve` (MCP), and runspec-console.

`runspec serve` flattens each verb into a leaf MCP tool (`gitlab_get-issue`, …).
Verb names are reconciled against GitLab's canonical MCP tool registry; the
extras (`whoami`, `get-project`, `list-projects`, `search-projects`, `search-code`, `list-issues`, `get-file`,
`list-tree`) are useful REST reads GitLab has no MCP tool for.

## Install

```
pip install runspec-gitlab
```

## Verbs

| Area | Verbs |
|---|---|
| Account / project | `whoami`, `get-project`, `list-projects`, `search-projects`, `get-file`, `list-tree` |
| Code search | `search-code` (a string across every project in a group) |
| Issues | `list-issues`, `get-issue` |
| Merge requests | `list-merge-requests`, `get-merge-request`, `get-merge-request-diffs`, `get-merge-request-commits`, `get-merge-request-notes`, `get-merge-request-pipelines`, `get-merge-request-conflicts` |
| Pipelines | `list-pipelines`, `get-pipeline-jobs`, `get-job-log` |

Writes (all `confirm`):

| Area | Verbs |
|---|---|
| Issues | `create-issue`, `update-issue` (edit, labels, assignees, close/reopen), `add-issue-note` |
| Merge requests | `create-merge-request`, `update-merge-request` (edit, labels, assignees, reviewers, close/reopen), `add-merge-request-note`, `merge-merge-request` |
| Repository | `create-branch`, `write-file` (create or replace a text file in one commit) |
| Pipelines / jobs | `create-pipeline`, `retry-pipeline`, `cancel-pipeline`, `retry-job`, `cancel-job`, `play-job` |

Assignees and reviewers are given as usernames. Labels, assignees, reviewers and
pipeline variables (`KEY=VALUE`) are repeatable args. Writes need a token with
the `api` scope; reads work with `read_api`.

Deletes (all `manual` — refused to an agent, run from the command line or the
console's Forms tab): `delete-issue`, `delete-branch`, `delete-file`,
`delete-pipeline`. GitLab itself limits issue and pipeline deletes to project
owners/admins and refuses protected or default branches.

### Searching a group's code

GitLab's group code search needs Advanced Search (Elasticsearch/OpenSearch) or
exact code search. Without it the web UI finds nothing across a group, even when
the text is there. `search-code` tries the group search first and, if GitLab
refuses it or it finds nothing, searches each project of the group and its
subgroups in turn (8 at a time by default):

```
gitlab search-code --group platform --search OLD_API_URL
gitlab search-code --group platform --search acme-auth --filename pyproject.toml
gitlab search-code --group platform --search acme-auth --file-path pyproject.toml
```

`--file-path` reads that one file in every project instead of searching, so it
works on any GitLab and is the surest way to find a dependency. Each match is a
project, a file and its matching lines; `strategy` and `note` say how the search
ran. `--limit` caps the matching files (`truncated` says there were more),
`--max-projects` caps the projects searched, and archived projects are skipped
unless `--include-archived`.

`get-merge-request-conflicts` is best-effort — GitLab has no public `/api/v4`
conflicts endpoint, so it relays the web-UI endpoint and returns its JSON
verbatim.

## Auth

```
export GITLAB_URL=https://gitlab.example.com        # default https://gitlab.com
export GITLAB_TOKEN=<personal-or-project-access-token>   # or GITLAB_PAT
export GITLAB_PROJECT=group/subgroup/project        # optional default
```

Every verb takes `--env`, selecting `GITLAB_<ENV>_*` prefixed vars. `--project`
accepts a numeric id or a `group/project` path and falls back to
`GITLAB_PROJECT`.

In runspec-console, a **token** credential labelled `GitLab` sets `GITLAB_TOKEN`
and `GITLAB_PAT`. The runnable declares both (on `--gitlab-token`), so the
credential applies to the agent without a scope, not only when picked in Forms.
`--gitlab-token` (password type) takes the token through the console's secret
channel and is refused on the command line; `--gitlab-url` overrides
`GITLAB_URL`.

```
gitlab whoami
gitlab list-merge-requests --project group/app --state opened
gitlab get-job-log --project group/app --job-id 998877
gitlab create-issue --project group/app --title "Login fails" --label bug --assignee alice
gitlab write-file --project group/app --branch fix --start-branch main \
  --file-path docs/notes.md --content-file notes.md --commit-message "Add notes"
```

## Public Python API

The pure logic is in **[`runspec-gitlab-core`](../runspec-gitlab-core)** —
`httpx`-only, no `runspec` dependency, no entry points. Every op takes a
`GitlabConfig` first arg and raises `GitlabError`, so a private wrapper can
inject credentials and bake in a default `project` / `env`:

```python
from runspec_gitlab_core import config_from_env, get_issue, list_merge_requests

cfg = config_from_env(env="prod")
mrs = list_merge_requests(cfg, state="opened")
```

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```
