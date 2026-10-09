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
| Macros & diagrams | `list-macros`, `get-diagram`, `download-diagram`, `add-diagram` |

Page bodies pass in a Confluence `representation` via `--content-format`
(`storage` / `wiki` / `editor` / `markdown`); `markdown` is converted to storage
XHTML client-side (best-effort).

## Macros and diagrams

`get-page --markdown` renders macros readably: code and PlantUML/Mermaid
macros become fenced blocks tagged with their language, info/note/tip/warning
panels become block quotes, expand becomes a bold title over its body, and a
draw.io or Gliffy diagram becomes a placeholder such as
`[draw.io diagram "arch" rev 3]`.

draw.io and Gliffy keep the diagram itself as a page attachment, so:

- `list-macros --page-id 123` lists every macro on the page with its parameters.
- `get-diagram --page-id 123 [--name arch]` reads the diagram as text: its
  shapes (labels, containing group) and connectors (`source -> target [label]`).
  Works with draw.io's compressed and uncompressed formats; Gliffy is
  best-effort. A diagram embedded from another page (`inc-drawio`) is read from
  that page.
- `download-diagram --page-id 123 --dest ./out` saves the `.drawio` / `.gliffy`
  source and the `.png` preview.

draw.io diagrams are read-only: creating or editing one would also need a fresh
PNG preview, which only draw.io can render.

### Creating diagrams

`add-diagram` puts a diagram written as text on a page as an image, with the
source in a collapsed "Diagram source" block under it so it can be edited and
re-rendered later. Re-adding the same `--name` replaces the image.

Render the PNG first and pass it with `--image`. In runspec-console the agent
does this with the built-in `render_diagram` tool, which needs nothing installed:

```
confluence add-diagram --page-id 123 --kind mermaid --name "Order flow" \
  --image order-flow.png \
  --source $'graph TD\n  Cart-->Checkout\n  Checkout-->Payment'
```

The image path is on the host the runnable runs on, so a remote host needs the
PNG uploaded there first.

Without `--image`, it uses a renderer the operator turned on for the host. Both
are off by default; with neither set, the call refuses before touching the page.

- **Kroki**: a [Kroki](https://kroki.io) server renders the text to PNG. Works
  on any Confluence, for mermaid, plantuml, c4plantuml, graphviz, d2, ditaa,
  erd, nomnoml, bpmn and structurizr.
- **macro**: inserts your Mermaid or PlantUML app's macro with the text as its
  body, so Confluence draws it live and people can edit it. Needs the app.

| Env var | Meaning |
|---|---|
| `CONFLUENCE_KROKI_URL` | Kroki server, e.g. a self-hosted `https://kroki.example.com`. The public kroki.io sees your diagram text, so prefer your own. |
| `CONFLUENCE_DIAGRAM_MODE` | `macro` or `image`. Unset: Kroki when a Kroki URL is set, else nothing. |
| `CONFLUENCE_MERMAID_MACRO` | Your Mermaid app's macro name (vendors differ; `list-macros` on a page with one shows it). Needed for Mermaid in macro mode. |
| `CONFLUENCE_PLANTUML_MACRO` | PlantUML app's macro name, default `plantuml`. |

All take the usual `CONFLUENCE_<ENV>_` prefix. With a Kroki URL set, macro mode
checks the text renders before touching the page.

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
