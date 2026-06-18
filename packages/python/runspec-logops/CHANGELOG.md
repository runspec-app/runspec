# runspec-logops Changelog

## [0.1.0] — 2026-06-18

Initial release.

Three runnables that let an agent triage application issues from a log file
cheaply — the runnables do the heavy lifting on the host where the log lives and
return small structured JSON, so the agent reads condensed conclusions, not raw
data:

- **`summarize-log`** (autonomous) — streams a log file and clusters repeated
  errors by signature, returning the top-N distinct issues with counts,
  first/last timestamp and one bounded sample each. Output size is bounded
  regardless of input size. Supports a severity floor (`--level`) and a time
  window (`--since`/`--until`).
- **`map-trace`** (autonomous) — resolves a stack trace, or a `summarize-log`
  signature, to the few relevant source snippets in a checkout (`--repo`).
- **`bundle-digest`** (confirm) — summarises a log, optionally maps a trace to
  code, and packs digest + snippets + manifest into one small zip for transfer.

Thin wrappers over `runspec-logops-core`. To wrap the logic privately (bake in
corporate paths/defaults, surface only your own runnables), depend on
`runspec-logops-core` directly rather than installing this package.
