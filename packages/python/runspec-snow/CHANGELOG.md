# runspec-snow Changelog

## [0.1.1] — 2026-09-20

### Changed

- **Packaging metadata for the runspec-console Marketplace.** Add a README
  (rendered in the Marketplace card's Details view) and publish `keywords`
  and Documentation / Source / Changelog / Issues project URLs, so the
  package's Marketplace card shows a description, tags and links. No
  runnable, argument, or behaviour changes.

## [0.1.0] — 2026-06-07

Initial release.

8 runnables for ServiceNow ITSM work, targeted at agents working records and
catalog tasks (the JSM-equivalent surface, for teams migrating their ITSM tasks
to ServiceNow). Tuned for `incident`, `sc_task` (catalog task) and `sc_req_item`
(RITM), but `--table` accepts any table.

**Records** — `snow-create-record`, `snow-get-record` (by sys_id or number)

**Comments** — `snow-add-comment` (customer-visible `comments`, or `--internal`
for an agent-only `work_notes` entry — the analog of JSM's public/internal split)

**State** — `snow-list-states`, `snow-update-state` (by state label or value).
ServiceNow has no Jira-style transition endpoint: state is a choice field, so a
"transition" is a validated field update with label→value resolution via
`sys_choice`.

**Fields** — `snow-set-field` (one or more `name=value` columns)

**Service Catalog discovery** — `snow-list-catalog-items`, `snow-list-variables`
(the orderable items and their form variables)

Read-only runnables (`snow-get-record`, `snow-list-states`,
`snow-list-catalog-items`, `snow-list-variables`) are `autonomy = "autonomous"`;
state-changing ones are `autonomy = "confirm"`.

**REST surface** — the Table API (`/api/now/table/{table}`) for record CRUD,
comments, state and fields; the Service Catalog API
(`/api/sn_sc/servicecatalog`) for read-only catalog discovery. Pure HTTP via
`httpx`, so it runs unchanged on Windows and Linux.

**Auth — environment variables only.** Set `SNOW_BASE_URL` (or `SNOW_INSTANCE`,
e.g. `https://acme.service-now.com`) plus either `SNOW_TOKEN` (OAuth/Bearer) or
`SNOW_USER` + `SNOW_PASSWORD` (Basic; token wins when both are present). Optional
`SNOW_TABLE` provides a default table. For multiple instances, use a `SNOW_<ENV>_`
prefix (e.g. `SNOW_UAT_BASE_URL`) and select it with the `--env dev|uat|prod` arg
present on every runnable (or `SNOW_ENV`); prefixed vars fall back to the
unprefixed ones.

**Identity is deploy-determined — no sudo flag.** Because every runnable is pure
HTTP, "run as the service account vs. as yourself" is decided by *who runs the
process* and *whose environment variables are present*: run it as yourself
locally, or as the service account on a server (`sudo -u <svc> -E`, a systemd
unit, or a login shell). No OS calls, so nothing to escalate.

**Public Python API for wrapper packages.** Every operation function takes a
`SnowConfig` first argument, so a private (e.g. Nexus-hosted) package can import
them, inject credentials in code, and bake in a default `default_table` / `env` —
building "one runnable per table" on top of this package. Exported: `SnowConfig`,
`SnowError`, `config_from_env`, `snow_request`, `resolve_table`, `create_record`,
`get_record`, `resolve_sys_id`, `add_comment`, `list_states`,
`resolve_state_value`, `update_state`, `set_fields`, `list_catalog_items`,
`get_catalog_item_variables`. Use `resolve_state_value` to map a state label to
the choice value `update_state` requires.

Catalog item ordering (`order_now`) and attachments are intentionally deferred to
a follow-up release.
