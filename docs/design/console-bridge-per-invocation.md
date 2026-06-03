# Design: runspec-console `bridge.py` changes for per-invocation logs

Companion to `multi-writer-audit-logging.md` (step 2 of its rollout order).
This is the implementation-level spec for making console's log consumption
**filename-scheme-agnostic**, so it works identically against today's single
`{runnable}.log` files and the new per-invocation
`{runnable}.{ts}.{run_id}.log` files — and, as a bonus, surfaces the
`exc_structured` crash frames added by #102.

## Goal

Two properties, in priority order:

1. **No regression.** Every change must keep working against legacy
   `{runnable}.log` files (single rotating file, runspec ≤ current). Because of
   this, step 2 can land *before or independently of* the write-side change.
2. **Forward-compatible.** Correctly attribute and group per-invocation files
   once `store = "per-run"` ships.

## The two filename couplings to remove

console reads logs in `bridge.py` for History (`get_history` /
`_get_remote_history`) and Analytics (`_collect_host_records`). Both are
coupled to the filename in exactly two places:

| # | Coupling | Code today | Breaks because |
|---|---|---|---|
| 1 | **Glob** picks files named `{runnable}.log` | `pattern = f"{runnable}.log"` (`bridge.py:174,202,254,266`) | won't match `{runnable}.{ts}.{run_id}.log` |
| 2 | **Runnable name = filename** | `_parse_log` passes `log_file.stem` (`:2175`); remote frames with `basename "$f" .log` (`:205,270`) | stem of `greeter.20260603T142201.<uuid>.log` is `greeter.20260603T142201.<uuid>`, not `greeter` → every invocation looks like a different runnable |

Everything downstream — `_parse_log_by_run_id` grouping by `extra.run_id`
(`:2211`), the run_summary field extraction, the History 200-cap, Analytics
bucketing — is already filename-independent and stays as-is.

## Design

### Principle: filename is for *discovery*, the record is for *attribution*

The runnable name already lives **inside** the run_summary record as
`extra.runnable` (written by `logging_setup.py`; see the `_emit_run_summary`
record shape). So:

- **Attribution** (the `runnable` field on every output record) comes from
  `extra.runnable`, with the filename-derived name kept only as a **fallback**
  for legacy logs that predate the field or in-progress runs without a summary.
- **Discovery** (which files to read) stays a glob, widened to also match the
  per-invocation pattern. The glob no longer determines the runnable name — it
  only narrows I/O.

This is strictly more robust than parsing filenames: it tolerates runnable
names containing dots (which would make `{runnable}.{ts}.{run_id}` ambiguous to
split), and it's identical for local and remote paths.

### Change 1 — discovery glob

Introduce one helper so local and remote agree:

```python
def _log_globs(runnable: str | None) -> list[str]:
    """Filename patterns to read for a runnable (or all). Order doesn't matter;
    de-dup happens by run_id downstream. Matches both the legacy single file
    and per-invocation files."""
    if runnable:
        return [f"{runnable}.*.log", f"{runnable}.log"]
    return ["*.log"]
```

- Local: iterate the patterns, union the matches, de-dup by path.
  `*.log` (the "all" case) already matches per-invocation files — no change
  needed there; only the *runnable-filtered* case widens.
- Remote: the shell `for f in …` loop takes both patterns:
  `for f in {dir}/{runnable}.*.log {dir}/{runnable}.log; do …`. A non-matching
  pattern expands to itself in POSIX `sh`, but the existing `[ -f "$f" ]` guard
  already skips those, so no `nullglob` dependency. (Verify on the target
  shells; if a login shell has `failglob`/`nullglob` set unexpectedly, prefer
  `ls` piped, but `[ -f ]` is the current idiom and works.)

`{runnable}.*.log` vs `{runnable}-other`: the `.` delimiter means
`foo.*.log` matches `foo.<ts>…` but not `foobar.*.log` — correct.

### Change 2 — attribution from the record

`_parse_log_by_run_id` already groups by `run_id` and reads the group's summary
entry. Take the runnable from there:

```python
# inside _parse_log_by_run_id, when building each record:
summary_extra = summary_entry.get("extra", {})
runnable_name = summary_extra.get("runnable") or name   # `name` = filename fallback
record["runnable"] = runnable_name
```

Same one-line change in `_parse_log_sequential` (legacy path): prefer
`extra.get("runnable")`, fall back to the passed `name`.

`_parse_log` keeps passing `log_file.stem` as that fallback `name` — harmless
now that it's only used when `extra.runnable` is absent. **No signature changes
needed**; `name` simply demotes from "the answer" to "the fallback."

### Change 3 — surface `exc_structured` crash frames (#102 bonus)

#102 writes a file-only `runspec.exception` record per uncaught exception:
top-level `exc_structured` (`type`, `message`, `module`, `frames[]`) plus
`extra.run_id`. Today `_parse_log_by_run_id` would file it under the group's
`lines` as a generic `CRITICAL "uncaught exception"` entry. Instead, detect and
lift it:

```python
for entry in entries:
    extra = entry.get("extra", {})
    run_id = extra.get("run_id")
    if not run_id:
        continue
    g = groups.setdefault(run_id, {"lines": [], "summary": None, "exc": None})
    if entry.get("exc_structured"):
        g["exc"] = entry["exc_structured"]          # lift, don't show as a log line
    elif extra.get("event") == "run_summary":
        g["summary"] = entry
    else:
        g["lines"].append({...})                    # unchanged
```

Attach to the output record so History/Analytics can render a real frames
table:

```python
if g["exc"]:
    record["exception"] = {                          # richer than summary's extra.exception
        "type": g["exc"]["type"],
        "message": g["exc"]["message"],
        "module": g["exc"].get("module"),
        "frames": g["exc"].get("frames", []),
    }
```

This *supersedes* the lean `extra.exception` (type/message/traceback string)
the Analytics tab reads today via `_summary_extra` — keep that as a fallback
when no `exc_structured` record is present (older logs). Net: `_summary_extra`
gains nothing mandatory; the per-run-id record carries the structured form when
available.

### Change 4 — read compacted archives (`*.log.gz`)

Deferred to step 3 (when `runspec logs compact` exists), but specified here so
the parser is built to expect it: `_parse_log` should transparently
`gzip.open` files ending in `.gz`, and `_log_globs` should also yield
`*.log.gz` / `{runnable}.*.log.gz`. Until compaction ships there are no such
files, so this is a no-op addition that prevents a second pass over the same
functions later.

## Function-by-function change list

| Function | Change |
|---|---|
| `get_history` (`:164`) | use `_log_globs(runnable)`; union local matches |
| `_get_remote_history` (`:191`) | widen the shell glob to both patterns; framing unchanged |
| `_collect_host_records` (`:239`) | same glob widening, local + remote |
| `_parse_log` (`:2168`) | gzip-aware open (Change 4); still passes `stem` as fallback name |
| `_parse_log_text` (`:2178`) | unchanged (already dispatches on run_id) |
| `_parse_log_by_run_id` (`:2211`) | runnable from `extra.runnable` fallback `name`; lift `exc_structured` into `record["exception"]` |
| `_parse_log_sequential` (`:2264`) | runnable from `extra.runnable` fallback `name` |
| `_summary_extra` (`:2151`) | unchanged; `exc_structured` is surfaced on the record, not here |
| new `_log_globs` | discovery patterns helper |

No changes to `_paths`, `venv_name`, `_aggregate_analytics`, or the UI data
contract — output record shape is unchanged except the *richer*
`record["exception"]` (additive: adds `module`/`frames`).

## Compatibility matrix

| Log source | Glob finds it? | Runnable attributed correctly? |
|---|---|---|
| Legacy `{runnable}.log` (no run_id, ancient) | ✅ `*.log` / `{runnable}.log` | ✅ via `name` fallback (stem) |
| `{runnable}.log` with run_id (≥0.18) | ✅ | ✅ `extra.runnable`, fallback stem (same value) |
| Per-invocation `{runnable}.{ts}.{run_id}.log` | ✅ `{runnable}.*.log` | ✅ `extra.runnable` (stem would be wrong; record wins) |
| Compacted `{runnable}.*.log.gz` | ✅ (Change 4) | ✅ `extra.runnable` |

## Test plan (`tests/test_analytics.py`, `test_chat_history.py`)

1. **Per-invocation fixtures:** write two `{runnable}.{ts}.{run_id}.log` files
   for the *same* runnable, each with one run's lines + run_summary. Assert
   `get_history` returns two records both with `runnable == "<name>"` (not the
   stem), grouped/ordered by ts.
2. **Mixed dir:** legacy `{runnable}.log` + per-invocation files in one `logs/`;
   assert all attribute to the right runnable and counts are right.
3. **Dotted runnable name** (e.g. `deploy.web`): assert attribution comes from
   the record, not a mis-split filename.
4. **`exc_structured` lifting:** a per-invocation file whose run crashed
   (exception record + run_summary with `exit_code=1`); assert
   `record["exception"]["frames"]` is populated and the exception is **not**
   duplicated as a `logLines` entry.
5. **Runnable filter:** `get_history(host, runnable="foo")` in a dir also
   containing `foobar.*.log` returns only `foo` runs (glob boundary correctness).
6. **Remote framing:** simulate the SSH stdout (`\x00RUNSPEC_LOG:` frames over
   multiple per-invocation files) and assert the same attribution as local.
7. **Regression:** existing single-file fixtures still pass unchanged.

## Open questions

1. **Discovery vs filter for the runnable case.** `_log_globs` keeps the I/O
   optimization (only read that runnable's files). The simpler alternative —
   always glob `*.log` and filter parsed records by `record["runnable"]` — is
   even more robust but reads every runnable's files on a single-runnable view.
   Recommend the glob approach for the hot History path; Analytics already reads
   everything so it's moot there.
2. **Remote file-count ceiling.** SSH-`cat` over many per-invocation files is
   the cost noted in the parent doc; if it bites before `compact` lands,
   consider a server-side `runspec logs --json` (once it exists) instead of raw
   `cat`, so console consumes the merged stream rather than globbing files. This
   is the natural convergence point between the two steps.
