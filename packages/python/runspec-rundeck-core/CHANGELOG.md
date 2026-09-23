# Changelog

All notable changes to `runspec-rundeck-core` are documented here.

## [0.1.0] — 2026-09-23

Initial release. Pure-Python Rundeck API helpers behind `runspec-rundeck`.

- `RundeckConfig` / `config_from_env` — resolve an instance URL, API token, and
  default project from `RUNDECK_URL` / `RUNDECK_TOKEN` / `RUNDECK_PROJECT`, with
  free-form `RUNDECK_<ENV>_*` prefixes (dev/uat/prod or regional labels like
  `prod-eu`) selected by an env label. Private-CA (`ca_bundle`), TLS-verify
  toggle, and explicit `proxy` supported.
- `rundeck_request` — the shared httpx helper (token header, `RundeckError` on
  ≥400, JSON or raw body).
- Job ops: `import_jobs` (create/update by UUID via `dupeOption`/`uuidOption`),
  `list_jobs`, `delete_job`, `prune_jobs` (delete stale jobs in a managed group),
  and `sync_target` (ensure-project → import → prune) returning a structured
  `SyncResult`. `project_exists` / `ensure_project` / `create_project` for the
  create-when-missing project flow.
- No `runspec` dependency, no `runspec.toml`, no entry points — importable
  helpers only.
