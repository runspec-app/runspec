# Changelog

All notable changes to `runspec-confluence-core` are documented here.

## [0.10.0] — 2026-10-18

- `config_from_env` takes `url` / `username` / `api_token` keyword overrides
  (explicit basic credentials win over a PAT from the environment).
- Reads the spellings a runspec-console credential sets: `CONFLUENCE_USER` for
  `USERNAME`, `CONFLUENCE_PASSWORD` for `API_TOKEN`, and `CONFLUENCE_PAT` / `CONFLUENCE_TOKEN` for
  `PERSONAL_TOKEN` (each also under the `CONFLUENCE_<ENV>_` prefix).

## [0.9.0] — 2026-10-09

**Create diagrams** from text.

- **New `diagram_write` module:** `add_diagram(config, page_id, kind, source,
  name, *, image=None, mode=None, position="append", caption=None)` adds a
  diagram written as Mermaid / PlantUML / C4-PlantUML / Graphviz / D2 / ditaa /
  erd / nomnoml / BPMN / Structurizr text to a page:
  - **`image=`** (the default path): a PNG the caller already rendered (e.g.
    by runspec-console's `render_diagram` tool) is uploaded as a page
    attachment (`Order flow` → `order-flow.png`) and shown with `ac:image`,
    followed by the source in a collapsed "Diagram source" expand. Needs
    nothing set up on the host. Re-adding the same name uploads a new
    attachment version and refreshes the source block in place.
  - **Kroki** (operator opt-in, `kroki_url`): without `image`, the text is
    rendered to PNG by a [Kroki](https://kroki.io) server and attached the
    same way.
  - **macro** (operator opt-in, `diagram_mode = "macro"`): a Mermaid /
    PlantUML app macro with the text as its body. Macro names come from the
    config (`mermaid_macro` has no default, `plantuml_macro` defaults to
    `plantuml`). With a Kroki server also set, the text is rendered once first
    so a syntax error comes back before the page is edited.
  - With no `image` and neither renderer set up, it refuses before touching the
    page and says to render the PNG first (`NO_RENDERER`). `resolve_mode`
    picks: explicit mode, else `diagram_mode`, else image when `kroki_url` is
    set, else that refusal.
- Also exported: `render_kroki`, `macro_storage`, `image_storage`,
  `diagram_filename`, `resolve_mode`, `IMAGE_KINDS`, `MACRO_KINDS`.
- **Config:** `ConfluenceConfig` gains `kroki_url`, `diagram_mode`,
  `mermaid_macro` and `plantuml_macro`, read from `CONFLUENCE_[ENV_]KROKI_URL`,
  `DIAGRAM_MODE`, `MERMAID_MACRO` and `PLANTUML_MACRO`. Kroki requests use the
  instance's proxy and CA settings.
- draw.io is still read-only: its preview PNG can only be rendered by draw.io.

## [0.8.0] — 2026-10-09

Read **macros and draw.io / Gliffy diagrams** on a page.

- **Macro-aware Markdown reads.** `html_to_markdown` (and so
  `simplify_page(body_as_markdown=True)`) used to strip `ac:` macro tags and
  glue their parameter values into the text, so a page with a draw.io diagram,
  a code block and an expand read back as `arch3pythonprint(1)More`. Storage
  macros are now rewritten first: code / noformat and PlantUML / Mermaid
  macros become fenced blocks tagged with their language, info / note / tip /
  warning / panel become block quotes led by their type, expand becomes a bold
  title over its body, draw.io / Gliffy become a `[draw.io diagram "arch" rev 3]`
  placeholder, `status` / `jira` keep their text, `ac:link` / `ac:image` become
  their link text / `[image: name]`, and other macros keep their body or leave a
  `[name macro]` placeholder. HTML without `ac:` markup is unchanged.
- **New `macros` module:** `list_macros(storage)` inventories a storage body
  (name, kind, parameters, plain-text body, nesting depth, and for draw.io /
  Gliffy the attachment filenames that hold the diagram); `storage_to_html`;
  `macro_kind`. PlantUML / Mermaid are matched by substring since vendors name
  their macros differently.
- **New `diagrams` module:** `page_macros(config, page_id)`,
  `get_diagram(config, page_id, name=…, include_source=…)` (fetches the source
  attachment and returns a text outline: shapes with labels, shape and
  containing group, plus connectors `source -> target [label]`),
  `download_diagram(config, page_id, dest, name=…)` (saves the `.drawio` /
  `.gliffy` source and `.png` preview), and the pure decoders
  `decode_drawio` (compressed and plain mxfile, `UserObject` wrappers, HTML
  labels, edge-label cells) and `decode_gliffy` (best-effort). `inc-drawio`
  diagrams are read from the page they're embedded from.
- `beautifulsoup4` is now a declared dependency (already installed via
  `markdownify`).

Read-only: writing draw.io diagrams would also need a fresh PNG preview, which
needs draw.io to render.

## [0.7.1] — 2026-09-05

Fix **415 Unsupported Media Type when re-uploading an attachment that already
exists** on a page. `upload_attachment` is reimplemented to call Confluence
directly instead of `atlassian-python-api`'s `attach_file`, whose update path
PUTs a multipart body to the JSON content endpoint (`PUT /rest/api/content/{id}`)
and so 415s when the filename matches an existing attachment. Now: an existing
attachment's data is updated via `POST .../child/attachment/{id}/data` (a new
version), and a new one is created via `POST .../child/attachment`. Content type
is guessed with stdlib `mimetypes` when not given; a lookup failure falls back to
the create endpoint (which Confluence versions by filename anyway).

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
