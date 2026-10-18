# Changelog

All notable changes to `runspec-jira-core` are documented here.

## [0.6.0] — 2026-10-18

- `config_from_env` takes `url` / `username` / `api_token` keyword overrides
  (explicit basic credentials win over a PAT from the environment).
- Reads the spellings a runspec-console credential sets: `JIRA_USER` for
  `USERNAME`, `JIRA_PASSWORD` for `API_TOKEN`, and `JIRA_PAT` / `JIRA_TOKEN` for
  `PERSONAL_TOKEN` (each also under the `JIRA_<ENV>_` prefix).

## [0.5.0] — 2026-10-16

### Added

- **Compact views for agents** (`runspec_jira_core.compact`). `compact_issue`
  turns a raw issue into a small view: key, URL, summary, status, type,
  priority, people, dates, labels, components, fix versions, parent, a
  600-char description preview, and counts of comments, links, subtasks and
  attachments. `include` adds `description` (full), `comments` (newest 20),
  `links`, `subtasks`, `attachments`, `changelog` or `custom` (non-empty custom
  fields by name). `compact_search` gives one row per issue plus `total`,
  `truncated` and `next_start`. `get_issue_compact`, `search_compact` and
  `project_issues_compact` fetch only the fields those views need. A raw issue
  is often 15–30 KB; the compact view is usually under 1 KB.
- `get_project_issues` takes `start` for paging.

## [0.4.0] — 2026-09-26

JSM project admin helpers and **request-type signatures** — raise or edit a
service desk request in one call by project + request type *name*, with friendly
field keys, instead of three discovery round trips.

### Added

- **Request-type signatures** (`requesttypes.py`): `describe_request_types(cfg,
  projects, request_types=None)` turns each request type of the given projects
  (keys or names) into a `RequestTypeSig` — fields with a slug `name`, portal
  `label`, `field_id`, `required`, a normalised `kind` and the allowed `options`
  (with ids), plus `json_schema()`. Fields are only fetched for the request types
  that match; results are cached in-process for an hour
  (`clear_signature_cache()`).
- **One-call create / update**: `create_request(cfg, project, request_type,
  values)` and `update_request(cfg, issue_key, values)` accept values keyed by
  field name, portal label or field id and coerce plain values into JSM's wire
  shapes (select → `{"id"}`, multiselect, cascade `"Parent > Child"`, user →
  `{"name"}` on DC / `{"accountId"}` on Cloud, dates, labels). An unknown field
  or option, or a missing required field, raises `JiraError` with close matches
  **and the request type's field list**, so the next attempt is right.
  `build_field_values` is the pure resolver; `create_request_from_signature`
  raises from a pre-built signature with a single POST.
- **Generated department verbs** (`codegen.py`): `python -m
  runspec_jira_core.codegen --runnable <name> --project … --toml … --manifest …`
  splices one typed `raise-<project>-<request type>` verb per request type into a
  wrapper package's `runspec.toml` (select → `choice` with options, `required`
  copied, date pickers) between markers, and writes a JSON manifest.
  `--check` exits 1 when either is stale. At runtime (`manifest.py`) `run_generated_command`
  raises from the manifest with one POST, re-resolving by name when the config
  targets a different instance (ids differ between dev/uat/prod).
  `load_manifest` / `manifest_signatures` / `manifest_arg_names` support the
  wrapper's dispatch.
- **Project roles** (`roles.py`): `list_project_roles`, `get_role_members`,
  `add_to_role`, `remove_from_role` — roles by name or id, users and groups,
  idempotent (reports who was already a member / not a member).
- **Customers and organizations** (`customers.py`): `list_customers`,
  `add_customers`, `remove_customers`, `list_organizations`,
  `add_to_organization` (optionally `create=True` and link to a `project`) — all
  addressed by project key or name.
- **`check_user_access(cfg, user, project=None)`**: active flag, groups,
  application roles (JSM licence), every project role held directly or via a
  group, `is_agent` / `is_customer`, and plain-language `findings`.
- `list_service_desks` / `resolve_service_desk` — project key or name → service desk.

## [0.3.0] — 2026-08-07

`config_from_env` gains a keyword-only `personal_token_override` — when set it is
used as the PAT and is authoritative (satisfies the credential requirement on its
own, takes precedence over env Basic credentials); the base URL is still read from
the environment. Lets a caller supply the token out-of-band (e.g. the runnable's
`--pat` secret arg from runspec-console) without env credentials.

## [0.2.0] — 2026-08-07

Add issue attachment helpers over the SDK's attachment methods:
`list_attachments`, `download_attachments`, `add_attachment`, `remove_attachment`.
Binary content is path-in / path-out — downloads write to a destination
directory, uploads read a local file path.

## [0.1.0] — 2026-08-07

Initial release. Pure-Python Jira Software/Core + Service Desk helpers over
`atlassian-python-api` — the importable core behind `runspec-jira`. No `runspec`
dependency, no `runspec.toml`, no entry points (surfaces zero runnables).

- `JiraConfig` + `config_from_env(env=None)` — env-var auth (`JIRA_URL`,
  `JIRA_USERNAME`/`JIRA_API_TOKEN` for Cloud/DC basic, `JIRA_PERSONAL_TOKEN` for
  DC PAT) with `JIRA_<ENV>_*` prefixes selected by `--env dev|uat|prod`. Cloud vs
  Server/Data Center resolved via `JIRA_CLOUD` or a `.atlassian.net` host.
- `build_client` / `build_servicedesk` construct `atlassian.Jira` /
  `atlassian.ServiceDesk`, picking exactly one auth mode.
- Operation helpers grouped by area: issues (get/create/update/assign/delete),
  search (`jql_search`, `get_project_issues`), transitions, comments, links
  (issue links, epic link, remote link), worklog, projects (discovery +
  create-field metadata), users, field discovery, watchers, and service desk (request
  types, request-type fields, create customer request, get request).
- `translate_errors` surfaces the SDK's `requests` exceptions as `JiraError`
  carrying Jira's own error message; `parse_json_arg` parses JSON-string args.

Deferred: attachments/media, images, OAuth 2.0, transition-by-id with screen
fields, cross-project issue move.
