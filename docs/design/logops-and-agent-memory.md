# Logops + agent memory — cheap log-driven app support

## Problem

Support staff want the runspec-console agent to help triage application issues
**from the app's log file**, frequently. A naive flow — the agent reads the whole
log over MCP, then greps the whole codebase — burns tokens on every query, which
is the thing corporate cares about when this runs all day.

The fix is a principle, not a model upgrade: **runnables do the heavy lifting on
the host where the data lives and return small structured JSON; the agent reads
condensed conclusions, not raw data.** Logs and the git checkout sit on remote
hosts reached over SSH; the console invokes remote runnables via `runspec serve`
(MCP over SSH) and already has cheap on-source filtering (`download_file` /
`relay_file` with filter pipelines) to colocate a filtered log next to the code,
remote or local.

This is an assistant, not a replacement — best-effort heuristics are acceptable,
and every output says how much it is *not* showing so the agent can narrow
deliberately.

## Pillar A — `runspec-logops` (shipped)

Three runnables, thin wrappers over the dependency-free `runspec-logops-core`:

- **`summarize-log`** (autonomous) — streams a log file, normalises each line to
  a stable *shape* (masking timestamps, ids, hex, numbers, quoted literals) and
  clusters events by an **error signature** (exception class + top normalised
  stack frames). Returns the top-N distinct signatures with `count`, first/last
  timestamp and one truncated sample each. The digest size is bounded by `top ×
  max_sample_lines × max_bytes` — **independent of how big the log is**. A
  `--level` floor and `--since`/`--until` window narrow it further. `truncated`
  and `distinct_signatures` tell the agent what it is not seeing.
- **`map-trace`** (autonomous) — resolves a stack trace, or a `summarize-log`
  signature like `NullPointerException@com.acme.OrderSvc.price`, to just the
  source snippets around each frame in a checkout (`--repo`). Capped at
  `--max-files` frames and a total line budget — targeted code, never the repo.
  Pure filesystem resolution (`os.walk`); no `git` required.
- **`bundle-digest`** (confirm) — summarises a log, optionally maps a trace to
  code, and packs `digest.json` + `snippets.json` + `manifest.json` into one
  small zip. The console's existing `download_file` then pulls that single
  artifact to local.

End to end: `summarize-log` → a tiny digest crosses the wire → the agent picks a
signature → `map-trace` → a handful of snippets cross the wire → optionally
`bundle-digest` + `download_file`. Whole logs and whole repos never transit.

## The core/wrapper split and private wrapping (the corporate path)

All logic lives in **`runspec-logops-core`**: it has **no `runspec` dependency,
no `runspec.toml`, and no entry points**, so installing it exposes the helper
functions for import while surfacing **zero** runnables to `runspec local` /
`runspec serve` discovery. `runspec-logops` is the reference runnable layer.

Public API (all plain params — corporate settings are injected by the caller, not
baked into the generic code):

```
summarize_log(path, *, level=None, since=None, until=None,
              top=10, max_sample_lines=20, max_bytes=200) -> dict
map_trace_to_sources(repo_root, trace, *, context=8,
                     max_files=5, max_total_lines=200) -> dict
build_bundle(digest, code_map, *, dest_dir, metadata=None) -> dict
```

Because the logic is importable and runnable-free, an operator wraps it privately
and publishes to an internal index (e.g. Nexus) **without the generic package
ever forcing a runnable into the corporate venv**:

```
acme-logops/                 # private, Nexus-only
  pyproject.toml             # deps: runspec, runspec-logops-core (NOT runspec-logops)
  acme_logops/
    runspec.toml             # corporate runnables, defaults baked in
    summarize.py             # main(): rs.parse() -> summarize_log(...) -> print JSON
```

The wrapper imports `summarize_log` / `map_trace_to_sources` / `build_bundle`,
passes corporate-resolved values (default log dirs, repo roots, redaction
patterns) as plain params, and is the **only** package that declares entry
points — so only the wrapped, corporate runnables appear in the venv. The public
repo ships generic, auditable logic; corporate logic and defaults never leave the
private index. This is the same pattern documented for `runspec-jsm` /
`runspec-snow`, and it works for wrapping any core (linux/jsm/snow/logops) the
same way.

## Pillar B — persistent memory + curated runbooks (shipped)

The console bridge kept chat history in memory only, so every run re-learned the
app. This adds a lightweight disk-backed store so repeat queries get cheaper,
**without** bloating the prompt. Four autonomous tools (local + reversible, so no
confirm gate — no model spend on bookkeeping):

- **Memory** (machine-local, auto-accruing): `remember(fact, app?, tags?, pin?)` /
  `search_memory(query, app?)` backed by `runspec_memory.toml`, keyed by an explicit
  `app` tag (free chat has no single active host). Recall is stdlib token-overlap
  scoring — no embedding dependency. Eviction keeps pinned + most-used + newest per
  app. **Never synced** — it's local knowledge.
- **Skills = curated runbooks** (shareable): `save_skill` / `load_skill` over
  `runspec_skills.toml`, a `[[skill]]` collection (symptom + ordered steps naming the
  runnables to call). It **rides the existing Config Sync** — added to `KNOWN_FILES`
  and merged by `id` like filters/credentials — so a team ships vetted procedures
  from one git tag. (A TOML collection, not markdown files, precisely so it reuses
  the single-file/collection sync machinery with near-zero new code.)

Only the **pinned** memory subset is folded into the system prompt — capped, and an
empty string when nothing is pinned — injected per turn through the bridge's existing
`system_override` path. New pins appear immediately while the tool schemas and base
system stay prompt-cached.

## Token-saving levers, together

| Lever | Where |
|---|---|
| Bounded log digest (cluster/dedup → top-N) | `runspec-logops` `summarize-log` |
| Trace → targeted snippets, not the repo | `runspec-logops` `map-trace` |
| On-source filter before transfer | console `download_file` / `relay_file` |
| Tool-search mode (~3 tools/turn) | console `_tool_mode()` (0.86.0+) |
| Prompt caching of tools + system | console Anthropic adapter |
| Persistent memory + curated runbooks | console `memory.py` (`runspec_memory.toml` local; `runspec_skills.toml` synced) |
