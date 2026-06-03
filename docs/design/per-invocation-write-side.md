# Design: per-invocation log files — write side (`store` mode)

Companion to `multi-writer-audit-logging.md` (step 1 of its rollout order).
Implementation-level spec for the **producer**: a new `[config.logging]` mode
that writes one log file per invocation instead of one rotating file per
runnable. Opt-in; today's single rotating file stays the default.

## The config knob

A new key under `[config.logging]`:

```toml
[config.logging]
store = "per-run"      # default "single" = today's one rotating file per runnable
```

- **Enum, not boolean.** `store = "single" | "per-run"`. An enum leaves room
  for a future sink (e.g. `"syslog"`) without a second flag; a boolean
  `per-invocation = true` would paint us into a corner.
- **Key name `store` is final** — chosen precisely because it generalises past
  file layout to a future `"syslog"`/sink value, unlike `files`.
- Default `"single"` → **zero behaviour change** for existing users.

### This is a format change → four files move in lockstep

Per the CLAUDE.md rule ("change the format → update `spec/SPEC.md` first") and
the `check_docs.py` guards:

1. `schema/runspec.schema.json` — add `store` to the `logging` properties
   (the block is `additionalProperties: false`, so an undeclared key fails
   validation and the **toml-examples** docs check would reject any example
   using it). Add:

   ```json
   "store": {
     "type": "string",
     "enum": ["single", "per-run"],
     "default": "single",
     "description": "Log file layout: 'single' = one rotating {runnable}.log; 'per-run' = one {runnable}.{ts}.{run_id}.log per invocation (multi-writer safe)."
   }
   ```

2. `spec/SPEC.md` — document `store` in the `[config.logging]` section: the two
   values, the per-invocation filename pattern, and that `rotate`/`keep` are
   **inert** under `per-run` (rotation is delegated to `runspec logs compact`).
3. `docs/logging.md` — a "Per-invocation files (shared venvs)" subsection (when
   to use it: multi-user / same-user-parallel; the filename; rotate/keep don't
   apply; forward-ref to `runspec logs` for viewing). Any ```toml example using
   `store` only validates *after* step 1.
4. `CHANGELOG.md` (+ `pyproject.toml` version bump) — `## [x]` entry, or
   **version-sync** fails.

`docs/format.md:542`'s short example can optionally mention `store`, but isn't
required to.

## Filename

```
{runnable}.{ts}.{run_id}.log        e.g.  greeter.20260603T142201Z.4f3c…e7.log
```

- `ts` = `datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")` — **UTC
  (final)**: sorts correctly across a multi-host fleet, no DST ambiguity, and
  compact (no `:` — safe on every filesystem incl. Windows). Purely for human
  `ls` ordering; `mtime` remains the source of truth for retention.
- `run_id` is the **same `uuid4` already minted at `logging_setup.py:132`** and
  injected into every record's `extra.run_id`. Reusing it gives a clean
  invariant: **the file's run_id == its records' run_id**, so tooling can map a
  file to an invocation without reading its contents. (And same-second parallel
  runs by one user can't collide — the uuid disambiguates.)

## Handler change in `configure_logging`

Today (`logging_setup.py:155-157`):

```python
log_dir = _resolve_log_dir()
log_path = log_dir / f"{runnable_name}.log"
fh = _make_file_handler(log_path, log_cfg["rotate"], log_cfg["keep"])
```

Becomes a branch on `store`:

```python
log_dir = _resolve_log_dir()
if log_cfg.get("store") == "per-run":
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"{runnable_name}.{ts}.{run_id}.log"
    # Plain handler — no rotation. delay=True so a no-op run leaves no empty file.
    fh = logging.FileHandler(log_path, encoding="utf-8", delay=True)
else:
    log_path = log_dir / f"{runnable_name}.log"
    fh = _make_file_handler(log_path, log_cfg["rotate"], log_cfg["keep"])
```

Everything after (`fh.setLevel(floor)`, the `sensitive` / `_RunIdFilter` /
`_JsonFormatter` wiring) is **unchanged** — the per-run file carries identical
JSON records, including the run_summary and the #102 `exc_structured` record.
So one per-run file ends up holding exactly one invocation's lines + its
summary + (if it crashed) its exception record. That's the shape
`console-bridge-per-invocation.md` consumes.

Notes:
- **`delay=True`** avoids creating empty files for runs that emit nothing (the
  default `summary` record means most runs still produce a file).
- `_make_file_handler` is bypassed in per-run mode, so a bogus `rotate` string
  won't raise at runtime — but the **schema still constrains `rotate`**, so a
  typo is caught by the docs/schema check at authoring time. Acceptable; noted
  as a minor open point.
- `_resolve_log_dir` is **unchanged** — still `{sys.prefix}/logs` with the
  `~/logs` write-probe fallback. This is exactly the interplay the deploy recipe
  relies on: lock `logs/` read-only and each user's per-run files land in their
  own `~/logs`; make it group-writable (setgid) and they share the dir with no
  contention (no shared *file* ever exists).

## Loader normalization

`packages/python/runspec/runspec/loader.py:77-81` (`_normalise_logging`) — add
the key with the safe default and clamp to the enum:

```python
store = str(raw.get("store", "single"))
return {
    "rotate": str(raw.get("rotate", "midnight")),
    "keep": int(raw.get("keep", 7)),
    "summary": bool(raw.get("summary", True)),
    "store": store if store in ("single", "per-run") else "single",
}
```

(Unknown value → fall back to `"single"` rather than raising — schema validation
is the place that flags typos; runtime stays lenient, consistent with the rest
of the normaliser.)

## Node parity

Mirror in lockstep (the parent doc tracks this as a node-* bump):

- `packages/node/src/loader.ts:44` (`normaliseLogging`) — add `store` with the
  same default/clamp.
- `packages/node/src/logging_setup.ts:352` (`makeFileHandler`) — add a plain
  append handler (no `_rotateIfNeeded`) selected when `store === 'per-run'`, and
  build the per-run path. Node resolves logs at `{project_root}/logs` (nearest
  ancestor `package.json` skipping `node_modules`, fallback `~/logs`) rather
  than `sys.prefix` — per-run just changes the **filename**, not the dir logic.
- Ensure Node uses the **same run_id in the filename and in `extra.run_id`**
  (same invariant as Python), so console's record-based attribution works
  identically against Node-produced logs.

## Tests (`tests/test_logging_setup.py`)

1. `store="per-run"`: file matches
   `^{runnable}\.\d{8}T\d{6}Z\.[0-9a-f-]{36}\.log$`, and the handler is a plain
   `FileHandler` (not `RotatingFileHandler`/`TimedRotatingFileHandler`).
2. **run_id invariant:** the `run_id` in the filename equals `extra.run_id` in
   the file's records.
3. **No empty file:** a run that emits nothing *and* has `summary=false`
   produces no file (delay=True); with summary on, exactly one summary record.
4. **Two configures in two processes** (simulate via monkeypatched
   `_configured` reset + distinct run_ids) → two distinct files, no overwrite.
5. **Default unchanged:** absent `store` → `{runnable}.log` rotating handler,
   byte-for-byte the existing behaviour (regression guard).
6. **rotate/keep inert under per-run:** setting `rotate="10 MB"` with
   `store="per-run"` still yields a plain non-rotating handler.

## Decided

- **Key name:** `store` (generalises to a future `"syslog"` sink, not just file
  layout). Values `single` (default) / `per-run`.
- **`ts` timezone:** UTC — fleet-sortable, DST-safe.

## Open questions

1. **Inert `rotate`/`keep` under per-run** — leave lenient (schema catches
   typos) vs warn at runtime. Recommend lenient.
