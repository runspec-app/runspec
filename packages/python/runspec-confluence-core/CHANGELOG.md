# Changelog

All notable changes to `runspec-confluence-core` are documented here.

## [0.2.0] — 2026-08-07

Add page attachment helpers over the SDK's attachment methods: `get_attachments`,
`download_attachments`, `upload_attachment`, `delete_attachment`. Binary content
is path-in / path-out.

## [0.1.0] — 2026-08-07

Initial release. Pure-Python Confluence helpers over `atlassian-python-api` — the
importable core behind `runspec-confluence`. No `runspec` dependency, no
`runspec.toml`, no entry points (surfaces zero runnables).

- `ConfluenceConfig` + `config_from_env(env=None)` — env-var auth
  (`CONFLUENCE_URL`, `CONFLUENCE_USERNAME`/`CONFLUENCE_API_TOKEN` for Cloud/DC
  basic, `CONFLUENCE_PERSONAL_TOKEN` for DC PAT) with `CONFLUENCE_<ENV>_*`
  prefixes selected by `--env`. Cloud vs Server/DC via `CONFLUENCE_CLOUD` or a
  `.atlassian.net` host.
- `build_client` constructs `atlassian.Confluence`, picking exactly one auth mode.
- Operation helpers: pages (get/children/ancestors/create/update/delete/move,
  space listing + space metadata), CQL `search`, and page content
  (history/labels/comments/restrictions).
- `translate_errors` surfaces the SDK's `requests` exceptions as `ConfluenceError`.

Content is passed in a Confluence `representation` (`storage` XHTML default, or
`wiki`/`editor`). Deferred: markdown↔storage conversion, attachments/images,
page diffs, space page-tree, setting restrictions, inline comments, templates,
analytics, OAuth 2.0.
