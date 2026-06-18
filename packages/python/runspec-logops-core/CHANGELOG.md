# runspec-logops-core Changelog

## [0.1.0] — 2026-06-18

Initial release.

The pure-Python logic core behind `runspec-logops`. Provides log-condensing and
code-mapping helpers as plain importable functions — **no dependency on
`runspec`, no `runspec.toml`, no console-script entry points** — so a package can
`from runspec_logops_core import summarize_log` (and the rest) without surfacing
any runnables in the venv or in `runspec local` / `runspec serve` discovery. This
is the corporate-facing deliverable: wrap it in a private package, bake in
corporate defaults as plain params, and ship your own runnables.

Each function returns plain data and *raises* on failure (`SourceNotFoundError`
when an input path is missing); wrappers catch these and render the JSON/exit
behaviour.

Exports:

- **Signatures** — `normalize_line`, `error_signature`, `detect_level` (pure
  line/event shaping for cheap clustering)
- **Digest** — `summarize_log` (streams a log, clusters by signature, returns a
  bounded top-N digest whose size is independent of input size)
- **Code map** — `map_trace_to_sources` (resolves a stack trace / signature to the
  few relevant source snippets in a checkout)
- **Bundle** — `build_bundle` (zips digest + snippets + manifest for transfer)
- **Errors** — `LogopsCoreError`, `SourceNotFoundError`
