# Atlassian (Jira + Confluence) runnables — parked exploration

**Status: UNDECIDED / PARKED (2026-08-01).** No implementation started, no
packages scaffolded. This doc preserves the exploration so we can resume without
re-deriving it. Nothing here is a commitment.

## Context

We wanted to exercise the `mcp-to-runnable` skill against a large, feature-packed
MCP server: [sooperset/mcp-atlassian](https://github.com/sooperset/mcp-atlassian),
which bridges AI assistants to **Jira and Confluence**. The goal is to decide
*whether* and *how* to turn it into runspec runnable(s) so the runspec-console
agent gets Jira/Confluence tools on every SSH fleet host via `runspec serve`,
under per-tool confirm gates and audit, with no second server process per host.

## What mcp-atlassian is

- **Products:** Jira **and** Confluence; **Cloud and Server/Data Center** (some
  tools Cloud-only: `jira_batch_get_changelogs`, `jira_move_issue`, the Confluence
  template tools).
- **Implementation:** Python (>=3.10), FastMCP over **`atlassian-python-api`**,
  plus `httpx`, `beautifulsoup4`, `markdownify`/`markdown-to-confluence` for
  content conversion. Transports: stdio (default) + streamable-HTTP.
- **Auth (env-var):** Cloud Basic = `JIRA_URL`/`JIRA_USERNAME`/`JIRA_API_TOKEN`
  (+ `CONFLUENCE_*`); Server/DC = `JIRA_PERSONAL_TOKEN`/`CONFLUENCE_PERSONAL_TOKEN`
  (PAT bearer); optional OAuth 2.0 (`ATLASSIAN_OAUTH_*`, keyring token storage).
  Jira and Confluence configured independently. HTTP mode supports per-request
  multi-tenant tokens.
- **Scale:** **66 tools total — 34 Jira + 32 Confluence** (verified by reading
  `src/mcp_atlassian/servers/jira.py` + `confluence.py`). The README's "98" is an
  inaccurate auto-summary.

### Full real tool inventory (source of truth for any future build)

**Jira (34):** get_user_profile, search_assignable_users, get_issue_watchers,
add_watcher, remove_watcher, get_issue, search, search_fields, get_field_options,
get_project_issues, get_transitions, get_worklog, download_attachments,
get_issue_images, get_agile_boards, get_board_issues, get_sprints_from_board,
get_sprint_issues, get_link_types, create_issue, batch_create_issues,
batch_get_changelogs, update_issue, assign_issue, delete_issue, move_issue,
add_comment, edit_comment, add_worklog, link_to_epic, create_issue_link,
create_remote_issue_link, remove_issue_link, transition_issue.

**Confluence (32):** search, get_page, get_page_children, get_space_page_tree,
get_comments, get_labels, add_label, create_page, update_page,
update_page_section, delete_page, move_page, add_comment, reply_to_comment,
get_inline_comments, add_inline_comment, search_user, get_page_history,
get_page_diff, get_page_views, upload_attachment, upload_attachments,
get_attachments, download_attachment, download_content_attachments,
delete_attachment, get_page_images, list_page_templates, get_page_template,
create_page_from_template, get_page_restrictions, set_page_restrictions.

## Reconciliation of a curated toolset proposal

A curated toolset grouping was proposed. Reconciled against the source:

- **Real & keepable:** `jira_issues`, `jira_fields`, `jira_comments`,
  `jira_transitions`, `jira_links`, `jira_worklog`, `jira_users`, `jira_watchers`
  (27 Jira tools), and essentially the **complete** Confluence surface
  (`confluence_pages`, `_comments`, `_labels`, `_users`, `_analytics`,
  `_templates`, `_attachments`).
- **Not present in mcp-atlassian (15 tools)** — but note "not in the server" ≠
  "cannot build," because a port reimplements over `atlassian-python-api`:
  - `jira_projects` group (6): get_all_projects, search_projects,
    get_project_components, get_project_issue_types, get_create_fields,
    get_project_fields — **buildable from the SDK; genuinely useful** for
    create-issue/field-discovery (mirrors what `runspec-jsm` got from its
    list-request-types/list-fields).
  - `jira_service_desk` group (6): service-desk/queues/request-types/
    customer-request — **overlaps `runspec-jsm`** (JSM territory); building here
    would duplicate that package.
  - `confluence` extras (3): `check_content_permissions`, `get_space_permissions`
    — superseded by `get/set_page_restrictions` (already kept); `copy_page` —
    SDK-buildable nice-to-have.
- **Naming note:** the phantom names resemble Atlassian's *official* Remote MCP
  server, not sooperset's — likely a blended tool table.

**Framing:** the proposal, corrected, is ~60+ tools — **near-full parity**, not a
narrow subset. Any build should be **phased** (toolset groups = the roadmap).

## Design options (OPEN — no decision)

### Option A — one package `runspec-atlassian`, two runnables (`jira` + `confluence`)

Skill-idiomatic (one server → one package). Two runnables because Jira/Confluence
auth is independent and 66 verbs on a single runnable is unwieldy. Core over
`atlassian-python-api`; wrapper carries prompts/types/autonomy; `runspec serve`
re-expands verbs to `jira_*` / `confluence_*` leaf MCP tools.

### Option B — product-aligned packages (current leaning)

Separate by product rather than by source server:

- **`runspec-confluence`** (new package + `confluence` runnable) — no existing
  runspec coverage of Confluence; clean, self-contained.
- **Jira Software/Core** tools (issues, search, transitions, links, worklog,
  agile) — these do **not** belong in `runspec-jsm`, because **`runspec-jsm` is
  Jira *Service Management*** (the `/rest/servicedeskapi` surface), a different
  API from Jira Software/Core. So Option B most coherently becomes:
  - a **new `runspec-jira`** for Jira Software/Core, **and**
  - **`runspec-jsm` enhanced only where it has genuine JSM gaps** (e.g. JSM Cloud
    support, or the extra service-desk verbs from the proposal) — *if needed*.

### A vs B trade-offs

- **A** is closest to the skill's worked pattern and keeps one CI/release unit;
  but "Atlassian" as a package spans two products with independent auth.
- **B** matches the repo's existing "one product surface = one package" grain
  (`runspec-jsm`, `runspec-snow`), gives Confluence a clean home, and avoids
  overloading jsm; but it's 2–3 packages instead of 1, and Jira/Confluence share
  no code so there's no core-reuse argument for co-locating them.
- **Recommendation to revisit:** B likely fits this repo better *provided* we
  keep the Jira-Software vs JSM distinction crisp (new `runspec-jira`, not "bolt
  Jira Software onto jsm").

## Cross-cutting decisions (independent of packaging; all OPEN)

1. **Auth modes:** Basic + PAT (env-var, the `runspec-jsm`/`snow` model) is the
   easy 80%; OAuth 2.0 is materially more work — likely defer.
2. **Core implementation:** wrap `atlassian-python-api` (fast, faithful, inherits
   content conversion; the skill permits a needed SDK) vs raw `httpx` like
   jsm/snow (leaner, but reimplements markdown/storage conversion).
3. **Binary/media tools** (`jira_download_attachments`, `jira_get_issue_images`,
   the 7 `confluence` attachment tools): don't map onto a JSON-stdout runnable —
   defer, or handle as path-in/path-out; keep out of any first tranche.
4. **SDK-only additions:** whether to add the `jira_projects` discovery tools
   (not in mcp-atlassian but buildable and useful).
5. **Phasing:** first tranche = text/JSON-shaped read + core-write verbs, then
   grow by toolset group.

## Reference material (for whoever resumes)

- Skill: `.claude/skills/mcp-to-runnable/SKILL.md`; design intent in
  `docs/design/mcp-to-runnable-skill.md`.
- Reference core/wrapper split: `packages/python/runspec-fs-core/` +
  `packages/python/runspec-fs/` (esp. `runspec_fs/cli.py`, `_dispatch.py`,
  `runspec_fs/runspec.toml`).
- Service-backed shape + env-var/`--env` auth to copy:
  `packages/python/runspec-jsm/` (`JsmConfig`, `config_from_env`) and
  `packages/python/runspec-snow/`.
- Type mapping / autonomy conventions: inherited from the `add-runnable` skill.

## Next step when resumed

1. Decide packaging: **Option A** (one `runspec-atlassian`, two runnables) vs
   **Option B** (`runspec-confluence` + `runspec-jira`, jsm enhanced if needed).
2. Settle the cross-cutting decisions above (auth, core impl, binary handling,
   phasing).
3. Run the `mcp-to-runnable` skill for the first tranche.
