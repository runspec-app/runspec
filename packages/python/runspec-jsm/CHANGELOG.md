# runspec-jsm Changelog

## [0.1.1] — 2026-09-20

### Changed

- **Packaging metadata for the runspec-console Marketplace.** Add a README
  (rendered in the Marketplace card's Details view) and publish `keywords`
  and Documentation / Source / Changelog / Issues project URLs, so the
  package's Marketplace card shows a description, tags and links. No
  runnable, argument, or behaviour changes.

## [0.1.0] — 2026-06-07

Initial release.

8 runnables for Jira Service Management **Data Center** service-desk work,
targeted at agents and customers (not Jira product admins):

**Requests** — `jsm-raise-request`, `jsm-get-request`

**Comments** — `jsm-add-comment` (public, or `--internal` for an agent-only note)

**Transitions** — `jsm-list-transitions`, `jsm-transition-issue` (by transition
name or id)

**Issue properties** — `jsm-set-property`

**Portal-form discovery** — `jsm-list-request-types`, `jsm-list-fields` (the real
`fieldId`/name/required metadata you need before raising a request)

Read-only runnables (`jsm-get-request`, `jsm-list-transitions`,
`jsm-list-request-types`, `jsm-list-fields`) are `autonomy = "autonomous"`;
state-changing ones are `autonomy = "confirm"`.

**Mixed REST surface** — the JSM Service Desk API (`/rest/servicedeskapi`) for
raise/comment/discovery; the core Jira API (`/rest/api/2`) for transitions and
properties. Pure HTTP via `httpx`, so it runs unchanged on Windows and Linux.

**Auth — environment variables only.** Set `JSM_BASE_URL` plus either `JSM_PAT`
(Bearer) or `JSM_USER` + `JSM_PASSWORD` (Basic; PAT wins when both are present).
Optional `JSM_SERVICE_DESK_ID` / `JSM_PROJECT_KEY` provide defaults. For multiple
instances, use a `JSM_<ENV>_` prefix (e.g. `JSM_UAT_BASE_URL`) and select it with
the `--env dev|uat|prod` arg present on every runnable (or `JSM_ENV`); prefixed
vars fall back to the unprefixed ones.

**Identity is deploy-determined — no sudo flag.** Because every runnable is pure
HTTP, "run as the service account vs. as yourself" is decided by *who runs the
process* and *whose environment variables are present*: run it as yourself
locally, or as the service account on a server (`sudo -u <svc> -E`, a systemd
unit, or a login shell). No OS calls, so nothing to escalate.

**Public Python API for wrapper packages.** Every operation function takes a
`JsmConfig` first argument, so a private (e.g. Nexus-hosted) package can import
them, inject credentials in code, and bake in a default `service_desk_id` /
`project_key` / `env` — building "one runnable per project" on top of this
package. Exported: `JsmConfig`, `config_from_env`, `jsm_request`, `JsmError`,
`create_request`, `get_request`, `add_comment`, `list_transitions`,
`transition_issue`, `resolve_transition_id`, `set_issue_property`,
`list_request_types`, `get_request_type_fields`, `resolve_request_fields`. Use
`resolve_request_fields` to map friendly field names to the `fieldId` keys
`create_request` requires — it raises early on unknown or missing-required fields
instead of failing with an opaque server 400.

Attachments are intentionally deferred to a follow-up release.
