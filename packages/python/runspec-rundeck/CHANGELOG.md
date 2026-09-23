# Changelog

All notable changes to `runspec-rundeck` are documented here.

## [0.1.0] — 2026-09-23

Initial release. Sync a venv's runspec runnables to Rundeck as jobs.

- `rundeck sync` — discover every runnable in the venv, emit a Rundeck job per
  leaf command (via `runspec emit --rundeck --stdout`), and import them into a
  project over the REST API. Idempotent by the emitted UUID
  (create-when-missing, update-when-present, rename-safe). `--prune` deletes
  jobs a runnable no longer emits (scoped to the managed group). `--all` includes
  hidden (`discoverable = false`) runnables; `--create-project` creates the
  project when missing. `--group` / `--node-filter` / `--venv` pass through to
  the emitter.
- `rundeck push` — import already-emitted `jobs/*.json` files (or a directory),
  the JVM-free equivalent of `rd jobs load -F json`.
- `rundeck list` — list a project's jobs (autonomous).
- Auth is env-var first — `RUNDECK_URL` / `RUNDECK_TOKEN` / `RUNDECK_PROJECT`,
  with `RUNDECK_<ENV>_*` prefixes selected by `--env` (dev/uat/prod or regional
  labels like `prod-eu`), so a CI matrix pushes to one instance per job. A
  `--token` password arg supplies the token through the runspec-console secret
  channel; `--url` / `--project` / `--api-version` / `--ca-bundle` / `--proxy` /
  `--insecure` override the environment.
- Thin wrapper over `runspec-rundeck-core`.
