# Changelog

All notable changes to `runspec-confluence-core` are documented here.

## [0.7.0] — 2026-09-04

Enable HTTP retry with backoff by default. `build_client` now constructs the
`atlassian.Confluence` client with `backoff_and_retry=True`, so transient
throttling / outage responses (`429`, `413`, `503`, honouring `Retry-After`) are
retried with exponential backoff instead of failing the call immediately. The
SDK's own retry defaults are unbounded for an interactive/agent context (up to
1000 attempts / 1800s), so this caps them to **5 attempts / 30s**. New
`ConfluenceConfig.retry` field (default `True`) and `CONFLUENCE_RETRY` env var —
set either false-y to restore the previous no-retry behaviour.

The caps are configurable: `ConfluenceConfig.max_backoff_retries` /
`max_backoff_seconds` (env `CONFLUENCE_MAX_BACKOFF_RETRIES` /
`CONFLUENCE_MAX_BACKOFF_SECONDS`), defaulting to the exported
`DEFAULT_MAX_BACKOFF_RETRIES` (5) / `DEFAULT_MAX_BACKOFF_SECONDS` (30) — so an
importing package can tune them relative to the baseline.

## [0.6.0] — 2026-09-02

Server/Data-Center correctness fixes and read-shaping improvements. **Three
items change output for existing callers** (marked ⚠).

**Bugs**

- ⚠ **`cql_from_text` is Server/DC-safe by default.** Plain text now wraps as
  `text ~ "…"` (works on Cloud *and* Server/DC) instead of the **Cloud-only**
  `siteSearch ~ "…"`, which silently returned nothing on Server/DC. New
  `text_field` param (default `"text"`) — pass `"siteSearch"` for the Cloud
  relevance-ranked field.
- **`cql_from_text` parenthesises a passed-through CQL query** before appending
  `type`/`space` clauses, so an `OR` in the query isn't re-scoped by the
  higher-binding `AND` (`a OR b` + type → `(a OR b) AND type = "page"`).
- ⚠ **`get_space_pages` returns `{"results": [...]}`** (a bounded single page)
  instead of the SDK's lazy generator, which serialised as `"<generator object>"`,
  walked the entire space (hundreds of requests), and let `HTTPError` escape
  untranslated. Pre-existing since 0.4.0.
- ⚠ **`simplify_user` reads both Cloud and Server/DC shapes** — Server/DC nests
  email at `details.business.email`, and carries `type`/`userKey`/`profilePicture`
  (avatar). Previously it yielded little more than the username on Server/DC.

**Missing capability**

- **`expand` plumbed through six read ops** (`search_by_label`,
  `get_page_children`, `get_attachments`, `list_spaces`, `get_space`, and
  `get_comments` — whose `body.view` default is now overridable). Without it the
  API omits the sections the `simplify_*` helpers read.
- **`simplify_*` field gaps filled:** `simplify_comment` adds `author`/`created`
  (from `history`, distinct from the last-edit `version.by`); `simplify_space`
  adds `description`; `simplify_attachment` adds `created`/`created_by` and reads
  `fileSize` from `metadata` as well as `extensions`.

**Additions**

- **`detect_verify() -> bool | str`** — best-effort platform trust-store
  detection (inject `truststore` if installed; else a known system CA-bundle
  path; else `True`). **Never returns `False`** — verification stays on unless a
  caller opts out with `verify_ssl=False`.
- ⚠ **`html_to_markdown` uses ATX headings + a stable `-` bullet** (markdownify
  defaults were Setext headings and cycling `*+-` bullets, which read poorly and
  can't express h3+).
- **`simplify_page(..., body_as_markdown=False)`** — opt in to convert the body
  HTML to Markdown instead of embedding raw storage/view HTML.

**Docs**

- Add `CONSUMERS.md` — what a private wrapper package needs from core (construct
  `ConfluenceConfig` directly, proxy / `verify_ssl` injection points, the
  sparse-dict contract the `simplify_*` helpers produce).

## [0.5.0] — 2026-09-02

Proxy + CA-bundle support and new read helpers.

- **Connection:** `ConfluenceConfig` gains `proxies: dict[str, str] | None` and
  widens `verify_ssl` to `bool | str` (a string is a CA-bundle path). New
  `ConfluenceConfig.resolve_verify()` normalises `verify_ssl` into the value
  `requests` expects. `config_from_env` reads `CONFLUENCE_PROXY_URL` (env-prefix
  aware) into `proxies`, and `CONFLUENCE_VERIFY_SSL` now accepts a CA-bundle path
  in addition to a bool token. `build_client` forwards both to
  `atlassian.Confluence` (passed straight through to `requests`).
- **New helpers:** `list_spaces` and `get_current_user`; `search_by_label` and
  `cql_from_text` (turns a plain-text query into a `siteSearch ~ "…"` CQL, or
  passes an already-CQL query through — ported from the mcp-atlassian search
  heuristic). `get_page_children` now takes `child_type`/`start`/`limit` and uses
  `get_page_child_by_type`.
- **Pagination:** new `client._cap_limit(limit, cap=100)` clamps page sizes;
  `get_comments` and `get_attachments` gain a `start` argument.
- **Read-side shaping (new `summarise` module):** `html_to_markdown` (Confluence
  HTML → Markdown, via `markdownify` — now a dependency) plus `simplify_page` /
  `simplify_comment` / `simplify_space` / `simplify_user` / `simplify_attachment`
  that trim a raw API response to a small, stable dict. This is the read
  counterpart to `mdconvert.markdown_to_storage` (writes); the two are **not**
  inverses.
- `get_space` / `get_space_pages` moved from `pages` to the new `spaces` module;
  both remain importable from the package root (back-compat).

## [0.4.0] — 2026-08-07

`config_from_env` gains a keyword-only `personal_token_override` — when set it is
used as the PAT and is authoritative (satisfies the credential requirement on its
own, takes precedence over env Basic credentials); the base URL is still read from
the environment. Lets a caller supply the token out-of-band (e.g. the runnable's
`--pat` secret arg from runspec-console) without env credentials.

## [0.3.0] — 2026-08-07

Add Markdown support: `create_page` / `update_page` accept
`representation="markdown"` and convert the body to Confluence storage XHTML
client-side (new `mdconvert.markdown_to_storage`, over the `markdown` library —
now a dependency). Best-effort: headings, lists, tables, fenced code, links, and
emphasis convert cleanly; Confluence macros are not produced.

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
