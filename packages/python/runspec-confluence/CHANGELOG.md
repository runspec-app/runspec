# Changelog

All notable changes to `runspec-confluence` are documented here.

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
