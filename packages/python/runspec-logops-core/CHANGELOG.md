# runspec-logops-core Changelog

## [0.3.1] — 2026-06-18

### Fixed
- Read source files and logs as **UTF-8** explicitly. On Windows the default
  locale codec (cp1252) mangled non-ASCII (e.g. em-dashes in comments) into
  mojibake in `map-trace` snippets and `summarize-log` samples.


## [0.3.0] — 2026-06-18

### Changed
- **`summarize_log` understands structured (JSON-lines) logs** — one JSON object
  per line, as runspec itself and many apps emit. The timestamp now comes from the
  record's `ts` field (so `first_ts`/`last_ts` populate and `--since`/`--until`
  work), the level from `level`, and the **signature** from the embedded exception
  (`exc`/`traceback`) — pulling the exception class *and* stack frames out of the
  JSON-escaped traceback (e.g. `BackoffError@_connect_room>_open`, which
  `map-trace` can then resolve) — or else the `message`, so lines cluster by what
  they say instead of by the useless JSON *shape* (`{<str>: <str>, …}`). The sample
  is a readable `ts level logger: message` one-liner rather than raw JSON.
- **`map_trace_to_sources` resolves a bare identifier.** A signature that is just an
  exception class (`BackoffError`) or a dotted name (`pkg.mod.func`) — no `@`, no
  `file:line` — now resolves to where that symbol/class is *defined* in the
  checkout, instead of matching nothing.


## [0.2.0] — 2026-06-18

### Changed
- **`map_trace_to_sources` resolves digest signatures far more reliably.** When a
  signature's class can't be matched to a same-named file, it now searches the
  checkout for the **definition** of the symbol (preferring a keyworded
  `def`/`class`/`function`, ignoring imports and call sites). This fixes the common
  cases that previously matched nothing:
  - Java **inner classes** (`Outer$Inner` → resolves in `Outer.java`).
  - A method whose **file name differs from the class** in the frame.
  - **Python function-only** signatures (e.g. `ValueError@process_order`) that carry
    no class at all.
  `_symbol_line` now lands on the definition line rather than the first reference.
- When nothing resolves, the result carries a `note` explaining how to retry (pass a
  raw `file:line` frame, or check `--repo`).


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
