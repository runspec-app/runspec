# JSM project admin + request-type signatures in `runspec-jira-core`

Status: **implemented** in `runspec-jira-core` 0.4.0 / `runspec-jira` 0.4.0.
The target is **Jira Service Management Data Center**; the Cloud branches
(accountIds, Cloud user params) are kept where they cost nothing.

## 1. Goals and decisions

1. **Project admin helpers**: project roles, JSM customers and organizations,
   and a one-call user access check.
2. **Collapse agent round trips** for raising and editing requests. The old flow
   took three calls: `list-request-types` → `list-request-type-fields` →
   `raise-request` with a JSON object keyed by `customfield_*` in wire shapes.
   The new flow is one call keyed by names.
3. **One department package, not one package per team.** A single internal
   wrapper package (already depending on `runspec-jira-core`) generates typed
   raise verbs for the ~6 projects the department uses daily. Every other
   project stays reachable ad hoc through the generic verbs.

**Automation rules were dropped.** On Data Center the only API is Automation for
Jira's internal `/rest/cb-automation/…`: unsupported and version-fragile.
**Configuring a request type's field layout was also dropped**, since neither
platform has a public API for it. Creating a request type *is* public
(`POST …/requesttype`), but it isn't built: without the field layout it is
rarely useful from an agent.

## 2. Request-type signatures (`requesttypes.py`)

`describe_request_types(cfg, projects, request_types=None)` resolves each
project (key or name) to its service desk, lists its request types, and fetches
fields **only for the types that match** (ids or name globs). It returns
`RequestTypeSig`s, and each `FieldSig` carries:

- `name`: a slug of the portal label, unique per type, suffixed `-field` when it
  would clash with runnable args such as `env`
- `label`
- `field_id`
- `required`
- `kind`: `str`/`text`/`number`/`date`/`datetime`/`select`/`multiselect`/`cascade`/`user`/`multiuser`/`labels`/`attachment`/`unknown`, derived from `jiraSchema`
- `options`: validValues with ids and cascade children

`RequestTypeSig.json_schema()` gives an MCP-ready input schema. Request types and
signatures are cached in-process for an hour.

`build_field_values(sig, values, cloud=, require_all=)` is pure. It resolves keys
by name, label or field id, and coerces plain values to wire shapes:

| kind | pass | sent as |
|---|---|---|
| select | `"Leeds"` (label or id) | `{"id": "2"}` |
| multiselect | `["Laptop","Monitor"]` or `"Laptop, Monitor"` | `[{"id":…}, …]` |
| cascade | `"UK > Leeds"` | `{"id": p, "child": {"id": c}}` |
| user / multiuser | username | `{"name": …}` (DC) or `{"accountId": …}` (Cloud) |
| date / datetime | ISO | `yyyy-MM-dd` / `yyyy-MM-ddTHH:mm:ss.000±zzzz` |
| labels | list or comma string | list of strings |

The following cases raise `JiraError`, and every error embeds the type's field
list:

- an unknown field
- a bad option, which gets a "did you mean"
- a missing required field
- an attachment field (raise the request first, then call `add-attachment`)

A key shaped `customfield_NNN` that isn't on the form passes through unchanged.
That is the escape hatch for fields hidden from the portal.

`create_request(cfg, project, request_type, values)` performs a lookup of 3 GETs
(desks, request types, that type's fields), then **one POST**.
`update_request(cfg, key, values)` reads the request's type, resolves the values
the same way (partial: required fields are not enforced), then makes **one issue
PUT**. Both accept pre-built `signatures`, which skips the lookups.

## 3. Generated department verbs (`codegen.py`)

A runspec.toml is static, so the department package **generates** its raise
verbs:

```
python -m runspec_jira_core.codegen --runnable mydept \
  --project HR --project PAY … --toml mydept/runspec.toml --manifest mydept/jsm_request_types.json
```

- **TOML output:** one `raise-<project>-<request type>` verb per request type is
  spliced between marker comments, so hand-written verbs outside them survive.
  Each field becomes a typed arg:
  - a select with ≤ `--max-options` (default 50) unique labels becomes `choice`
  - multiselect, multiuser and labels become `multiple`
  - dates get `ui = "date"`/`"datetime"`
  - `required` is copied across
  - attachments are skipped
  - `on-behalf-of` is added when the type allows it
  - `meta.jsm` carries the project, service desk id and request type id
  - `autonomy` is `confirm` by default
- **Manifest output:** the signatures as JSON, shipped as package data.
  `run_generated_command` raises from it with **one POST, no lookups**. When the
  runtime config targets a different instance than the manifest's `url` (e.g.
  `--env uat`), it re-resolves by project + request type name, because request
  type and option ids differ between instances.
- **`--check`:** writes nothing and exits 1 when either artifact is stale. Run it
  in CI or nightly to catch forms edited in Jira.
- **Edits:** the department package's `update-request` verb passes
  `manifest_signatures(MANIFEST)`, so edits to the generated types resolve
  offline. Other types resolve live.
- **Tool count:** the generator emits one verb per request type, so six projects
  with many types can produce dozens of tools. `--request-type` globs trim the
  set. The console's `tool_mode = "search"` and `[tool_visibility]` handle the
  rest.

The generated TOML is parsed and validated by the real runspec parser in
`runspec-jira`'s tests: choice options, `multiple` choices, required fields.

## 4. Admin helpers

| Module | Functions | Notes |
|---|---|---|
| `roles.py` | `list_project_roles`, `get_role_members`, `add_to_role`, `remove_from_role` | Role by name (case-insensitive) or id; users + groups; idempotent results (`added_*` / `already_*`, `removed_*` / `not_member_*`). |
| `customers.py` | `list_customers`, `add_customers`, `remove_customers`, `list_organizations`, `add_to_organization` | Project key or name → service desk. `add_to_organization(create=True, project=…)` creates and links the org. DC sends `usernames`, Cloud sends `accountIds`. |
| `users.py` | `check_user_access(cfg, user, project=None)` | See below. |

`check_user_access` returns:

- `active`
- `groups`
- `application_roles`
- `jsm_licensed` (`jira-servicedesk`)
- with a project: `project_roles` (via `direct` or `group:<name>`), `is_agent`
  (licensed + in *Service Desk Team* + active) and `is_customer` (in *Service
  Desk Customers*)
- plain-language `findings`

Customer access granted only through a JSM organization is not detected, and
the findings say so.

The new helpers call REST paths directly through the SDK's generic
`get`/`post`/`put`/`delete`. This keeps them independent of which
`atlassian-python-api` version wraps which endpoint.

## 5. `runspec-jira` verbs

The generic runnable exposes the same functionality for ad hoc use:

| Group | Verbs | Autonomy |
|---|---|---|
| Request types by name | `describe-request-types` | `autonomous` |
| | `create-request`, `update-request` | `confirm` |
| Roles | `list-project-roles`, `get-role-members` | `autonomous` |
| | `add-to-role`, `remove-from-role` | `confirm` |
| Customers | `list-customers`, `list-organizations` | `autonomous` |
| | `add-customers`, `remove-customers`, `add-to-organization` | `confirm` |
| Access | `check-user-access` | `autonomous` |

## 6. Relationship to `runspec-jsm`

`runspec-jsm`'s `resolve_request_fields` (friendly name → fieldId) is a subset of
§2. It is left as is; whether to point it at the core or retire it is a later
call.
