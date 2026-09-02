# Changelog

All notable changes to `runspec-confluence` are documented here.

## [0.6.0] — 2026-09-02

Track `runspec-confluence-core` 0.6.0 (dep bumped to `>=0.6.0`).

- **`search-text` is now correct on Server/Data Center.** The verb passes
  `text_field="siteSearch"` only when the instance is Cloud, and `"text"`
  otherwise — Server/DC has no `siteSearch` field, so plain-text search there
  previously returned nothing.
- Inherits the core 0.6.0 fixes visible through the runnable: `get-space-pages`
  now returns a materialised `{"results": [...]}` page (no leaked generator);
  `whoami` returns useful fields on Server/DC; `get-page --markdown` renders
  ATX headings and `-` bullets.

## [0.5.0] — 2026-09-02

Surface the `runspec-confluence-core` 0.5.0 additions on the runnable (requires
`runspec-confluence-core >= 0.5.0`).

- **New verbs:** `whoami` (current user), `list-spaces`, `search-by-label`, and
  `search-text` (plain-text search wrapped as a `siteSearch` CQL via
  `cql_from_text`, or a full CQL expression used verbatim). All autonomous reads.
- **`get-page-children`** now takes `--child-type` (page/comment/attachment),
  `--start`, and `--limit` (was immediate child pages only).
- **`get-comments`** and **`get-attachments`** gain a `--start` pagination offset.
- **`get-page --markdown`** renders the fetched body to Markdown as a `markdown`
  field (forces `body.storage` expand when `--expand` is unset), via the core
  `html_to_markdown`.
- **Proxy + private-CA support** (no new flags): `CONFLUENCE_PROXY_URL` routes
  requests through a proxy and `CONFLUENCE_VERIFY_SSL` may be a CA-bundle path —
  both read by `config_from_env` and forwarded to the client.

## [0.4.0] — 2026-08-07

Add an optional `--confluence-pat` secret arg (a `password`-typed inherited global)
for the Confluence Personal Access Token, so a credential can be supplied through
runspec-console (via the `RUNSPEC_CONFLUENCE_ARG_CONFLUENCE_PAT` secret channel)
instead of the `CONFLUENCE_PERSONAL_TOKEN` env var. The arg is named
`confluence-pat` (not `pat`) so runspec-console derives a distinct env var from it. When set it overrides env credentials. Being a
`password` arg it is refused on the command line, kept out of logs/audit, and
omitted from the agent's MCP tool schemas. Requires `runspec-confluence-core >= 0.4.0`.

## [0.3.0] — 2026-08-07

`create-page` / `update-page` gain a `markdown` option on `--content-format`,
converting a Markdown body to Confluence storage client-side (best-effort, over
the `markdown` library). Requires `runspec-confluence-core >= 0.3.0`.

## [0.2.0] — 2026-08-07

Add four attachment verbs to the `confluence` runnable: `get-attachments`
(autonomous), and `download-attachments` / `upload-attachment` / `delete-attachment`
(confirm). `download-attachments --dest <dir>` writes a page's attachments to a
local directory; `upload-attachment --file <path>` uploads a local file. Requires
`runspec-confluence-core >= 0.2.0`.

## [0.1.0] — 2026-08-07

Initial release. A `confluence` runnable ported from the Atlassian MCP server
(sooperset/mcp-atlassian) onto `atlassian-python-api`, on Cloud and Server/Data
Center. Thin wrapper over `runspec-confluence-core`.

- One runnable, `confluence`, with a verb per tool; `runspec serve` re-expands
  each into a leaf MCP tool (`confluence_get-page`, …). Reads are `autonomous`,
  writes `confirm`.
- Verbs: `search` (CQL), pages (`get-page`, `get-page-children`,
  `get-page-ancestors`, `create-page`, `update-page`, `delete-page`, `move-page`,
  `get-space-pages`, `get-space`), and page content (`get-page-history`,
  `get-labels`, `add-label`, `get-comments`, `add-comment`,
  `get-page-restrictions`).
- Auth is env-var driven (`CONFLUENCE_URL` + `CONFLUENCE_PERSONAL_TOKEN` or
  `CONFLUENCE_USERNAME`/`CONFLUENCE_API_TOKEN`), with `CONFLUENCE_<ENV>_*` prefixes
  selected by the global `--env`. Cloud vs Server/DC via `CONFLUENCE_CLOUD` or a
  `.atlassian.net` host.
- Page bodies are passed in a Confluence `representation` via `--content-format`
  (`storage` default, or `wiki`/`editor`).

Deferred: markdown↔storage conversion, attachments/images, page diffs, space
page-tree, setting restrictions, inline comments, templates, analytics, OAuth 2.0.
