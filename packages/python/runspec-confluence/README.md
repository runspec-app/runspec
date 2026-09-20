# runspec-confluence

A Confluence runnable for [runspec](https://pypi.org/project/runspec/), ported
from the [Atlassian MCP server](https://github.com/sooperset/mcp-atlassian) onto
`atlassian-python-api` — pages, spaces, comments, labels, restrictions and
attachments, on **Cloud and Server/Data Center**. One runnable — **`confluence`**
— with 20-plus verbs. `pip install` it into a venv and it's discoverable by
`runspec local`, `runspec serve` (MCP), and runspec-console.

`runspec serve` flattens each verb into a leaf MCP tool (`confluence_get-page`,
…). Read verbs are `autonomy = "autonomous"`; writes are `autonomy = "confirm"`.

## Install

```
pip install runspec-confluence
```

## Verbs

| Area | Verbs |
|---|---|
| Search | `search` (CQL), `search-text`, `search-by-label` |
| Spaces | `list-spaces`, `get-space`, `get-space-pages` |
| Pages (read) | `get-page`, `get-page-children`, `get-page-ancestors`, `get-page-history`, `get-page-restrictions`, `whoami` |
| Pages (write) | `create-page`, `update-page`, `delete-page`, `move-page` |
| Labels & comments | `get-labels`, `add-label`, `get-comments`, `add-comment` |
| Attachments | `get-attachments`, `download-attachments`, `upload-attachment`, `delete-attachment` |

Page bodies pass in a Confluence `representation` via `--content-format`
(`storage` / `wiki` / `editor` / `markdown`); `markdown` is converted to storage
XHTML client-side (best-effort).

## Auth — env vars only

```
export CONFLUENCE_URL=https://your-org.atlassian.net/wiki
# Cloud (API token):
export CONFLUENCE_USERNAME=you@example.com
export CONFLUENCE_API_TOKEN=<api-token>
# …or Server/DC (Personal Access Token):
export CONFLUENCE_PERSONAL_TOKEN=<pat>
```

Cloud vs Server/DC is detected from a `.atlassian.net` host or set with
`CONFLUENCE_CLOUD`. Every verb takes `--env`, selecting `CONFLUENCE_<ENV>_*`
prefixed vars. The PAT can instead ride the runspec-console secret channel via
the optional `--confluence-pat` (a `password`-typed arg, refused on the CLI,
kept out of logs).

```
confluence search --cql "space = OPS AND type = page"
confluence get-page --page-id 123456
confluence create-page --space OPS --title "Runbook" --content-format markdown --body "# Steps"
```

## Public Python API

The pure logic is in **[`runspec-confluence-core`](../runspec-confluence-core)**
— over `atlassian-python-api` + `markdown`, no `runspec` dependency, no entry
points. Every op takes a `ConfluenceConfig` first arg and raises
`ConfluenceError`, so a private wrapper can inject credentials and bake in a
default `space` / `env`:

```python
from runspec_confluence_core import config_from_env, build_client, get_page

cfg = config_from_env(env="prod")
page = get_page(cfg, page_id="123456")
```

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```
