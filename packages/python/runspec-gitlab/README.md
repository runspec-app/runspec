# runspec-gitlab

A read-only GitLab runnable for [runspec](https://pypi.org/project/runspec/),
ported onto the GitLab REST API (v4) — **not** proxying GitLab's hosted MCP
server. One runnable — **`gitlab`** — with 17 read verbs, all `autonomy =
"autonomous"`. `pip install` it into a venv and it's discoverable by `runspec
local`, `runspec serve` (MCP), and runspec-console.

`runspec serve` flattens each verb into a leaf MCP tool (`gitlab_get-issue`, …).
Verb names are reconciled against GitLab's canonical MCP tool registry; the
extras (`whoami`, `get-project`, `search-projects`, `list-issues`, `get-file`,
`list-tree`) are useful REST reads GitLab has no MCP tool for.

## Install

```
pip install runspec-gitlab
```

## Verbs

| Area | Verbs |
|---|---|
| Account / project | `whoami`, `get-project`, `search-projects`, `get-file`, `list-tree` |
| Issues | `list-issues`, `get-issue` |
| Merge requests | `list-merge-requests`, `get-merge-request`, `get-merge-request-diffs`, `get-merge-request-commits`, `get-merge-request-notes`, `get-merge-request-pipelines`, `get-merge-request-conflicts` |
| Pipelines | `list-pipelines`, `get-pipeline-jobs`, `get-job-log` |

`get-merge-request-conflicts` is best-effort — GitLab has no public `/api/v4`
conflicts endpoint, so it relays the web-UI endpoint and returns its JSON
verbatim.

## Auth — env vars only

```
export GITLAB_URL=https://gitlab.example.com        # default https://gitlab.com
export GITLAB_TOKEN=<personal-or-project-access-token>
export GITLAB_PROJECT=group/subgroup/project        # optional default
```

Every verb takes `--env`, selecting `GITLAB_<ENV>_*` prefixed vars. `--project`
accepts a numeric id or a `group/project` path and falls back to
`GITLAB_PROJECT`.

```
gitlab whoami
gitlab list-merge-requests --project group/app --state opened
gitlab get-job-log --project group/app --job-id 998877
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
