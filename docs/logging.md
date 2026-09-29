# Logging

A runnable that wants stdlib logging shouldn't have to wire up handlers,
formatters, or rotation policies. Add `[config.logging]` to your
`runspec.toml` and `parse()` does it for you.

!!! info "Availability"
    `[config.logging]` is available in **runspec 0.10.0+** (Python) and
    **runspec-node 0.9.0+**. Structured `extra` fields landed in
    **0.11.0 / node-0.10.0**.

---

## Turn it on

Add the block to your spec:

```toml
[config.logging]
rotate = "midnight"    # see "Rotation policies" below
keep   = 7             # number of rotated backups to keep
```

Both fields are optional. Defaults: `rotate = "midnight"`, `keep = 7`. That's
enough — `parse()` configures the rest.

---

## Use a logger

=== "Python"

    Just use stdlib logging — no `runspec` imports needed beyond `parse()`.

    ```python
    import logging
    from runspec import parse

    logger = logging.getLogger(__name__)

    def main():
        args = parse()
        logger.info("Starting run for %s", args.target)
        logger.debug("Resolved args: %s", dict(args._args))
    ```

    `parse()` calls `logging.getLogger(__name__)` work because `[config.logging]`
    configured the root logger before your code ran. You don't need to call
    `logging.basicConfig()` or any runspec-specific setup.

=== "Node"

    Import `getLogger` from `runspec-node`. Loggers are named, lightweight, and
    write to the same file/console handlers `parse()` configured.

    ```typescript
    import { parse, getLogger } from 'runspec-node';

    const logger = getLogger('myapp');

    function main(): void {
      const args = parse();
      logger.info('Starting run for %s', args.target);
      logger.debug('Resolved args: %j', args);
    }

    main();
    ```

    `getLogger` is a no-op until `parse()` has run — if you somehow call it
    earlier, log records are buffered and replayed once configuration completes.

---

## Where logs go

Three surfaces, all always on, routed by record level:

| Surface | Receives | Format | Notes |
|---|---|---|---|
| **stdout** | INFO and below | plain message (reads like `print()`) | Captured as the MCP tool response in agent mode |
| **stderr** | WARNING and above | `LEVEL: message` | Pinned at WARNING — not affected by `--debug` |
| **File** | INFO by default; DEBUG with `--debug` | Structured JSON | `{package_dir}/logs/{runnable}.log` |

`package_dir` is the directory containing `runspec.toml`. When that path is
not writable, runspec falls back to `~/logs/{runnable}.log` rather than
silently dropping log records.

The split between stdout and stderr matches Unix stream conventions: routine
output on stdout (greppable, pipeable), warnings and errors on stderr (still
visible when stdout is redirected). The file is the audit trail — it
defaults to INFO so third-party libraries logging at DEBUG (urllib3, boto3,
sqlalchemy, …) don't flood it, and flips to DEBUG together with stdout when
you need full detail.

```json
// excerpt from {package_dir}/logs/deploy.log
{"time":"2026-05-20T14:02:18.412Z","level":"INFO","logger":"deploy","msg":"Starting run for prod"}
{"time":"2026-05-20T14:02:18.430Z","level":"DEBUG","logger":"deploy","msg":"Resolved args","extra":{"target":"prod","dry_run":false}}
```

---

## Agent mode

When a runnable is invoked via `runspec serve`, `RUNSPEC_AGENT=1` is set in
the environment. The routing is **the same as CLI mode** — there's no
separate agent code path to maintain. `runspec serve` captures the
subprocess's stdout as the MCP tool response, so every `logger.info(...)`
line reaches the calling agent automatically. Stderr stays on stderr (the
serve loop forwards it to the agent's logs). The file handler is
unaffected.

You write the same code for both surfaces — `logger.info("done")` shows up
in your terminal when you run the tool by hand, and shows up as the tool
response when an agent invokes it.

---

## Runtime override: `--debug`

When `[config.logging]` is present, a `--debug` flag is automatically added
to every runnable. It only raises visibility — there is no `level` knob to
silence INFO, because silencing INFO would break agent responses.

```bash
deploy --target prod --debug
RUNSPEC_DEBUG=1 deploy --target prod
```

With `--debug`:
- **stdout** also includes DEBUG records (plus full tracebacks on errors)
- **file** flips from INFO to DEBUG too

Stderr stays pinned at WARNING regardless. The CLI flag wins if both
`--debug` and `RUNSPEC_DEBUG=1` are set.

---

## Sensitive data redaction

Every log record — console and file — is passed through a sensitive-data
filter before emission. The filter replaces matches with `[REDACTED]`:

- Common credential field names (`password`, `passwd`, `token`, `api_key`,
  `apikey`, `secret`, `auth`, `authorization`)
- `Authorization: Bearer …` and `Authorization: Basic …` headers
- URL credentials (`https://user:pass@host/`)
- JSON-encoded credential fields (`"password": "..."`, `"token": "..."`)
- Form-encoded credential fields (`password=...&token=...`)

```python
logger.info('Calling https://admin:hunter2@api.example.com/deploy')
# → "Calling https://[REDACTED]@api.example.com/deploy"

logger.info('Got response: %s', '{"token": "sk_live_abc123", "ok": true}')
# → 'Got response: {"token": "[REDACTED]", "ok": true}'
```

Filter errors are silent — a bad pattern never suppresses a log record.

You don't have to think about this. Code defensively where you can, but
trust that the filter is the safety net.

---

## Structured extra fields

You don't need a wrapper library for structured logs. Pass extra context
through the standard idiom for each language:

=== "Python"

    Use stdlib `extra=` — exactly the API you'd use for stdlib logging:

    ```python
    logger.info('Deploy succeeded', extra={
        'target': args.target,
        'release': args.release,
        'duration_ms': 1240,
    })

    logger.error('Deploy failed', extra={'target': args.target}, exc_info=exc)
    ```

=== "Node"

    Pass an object as the trailing argument; the `error` key is special and
    extracts an `Error`:

    ```typescript
    logger.info('Deploy succeeded', {
      target: args.target,
      release: args.release,
      duration_ms: 1240,
    });

    logger.error('Deploy failed', { target: args.target, error: err });
    ```

Where they appear:

- **JSON file output:** under a nested `"extra"` object on the record.
- **Console output:** appended as `{key=value key=value …}` after the
  message.

Sensitive key names (`token`, `password`, `api_key`, `secret`, etc.) are
unconditionally redacted in `extra` fields too — both the key check and the
string-value filter run.

---

## Rotation policies

The `rotate` field accepts time-based and size-based policies:

| Value | Rotates when |
|---|---|
| `"midnight"` (default) | At local midnight |
| `"daily"` | After 24 hours from first write |
| `"weekly"` | After 7 days from first write |
| `"100 KB"`, `"10 MB"`, `"1 GB"` | When the file exceeds the size |

`keep` controls how many rotated backups are retained. Files older than
`keep + 1` are deleted on the next rotation.

---

## Per-invocation files (shared venvs)

The default `store = "single"` keeps one rotating `{runnable}.log`. That's
correct for local, single-user runs — but in-process rotation is **not safe**
when several users (or one user running in parallel) write the same file at
once: a rotation in one process can lose records from another. For that case,
switch to one file per invocation:

```toml
[config.logging]
store = "per-run"
```

Each run then writes its own file:

```
{runnable}.{utc-ts}.{run_id}.log     e.g.  deploy.20260603T142201Z.4f3c…e7.log
```

Because no file is ever shared, there is no rotation race and no cross-user
ownership conflict — it just works for a shared deployment venv with many
operators. The `run_id` in the filename matches the `run_id` field in the
records inside it.

There is **no automatic rotation** in this mode (`rotate`/`keep` are ignored) —
files accumulate until you clean them up. Viewing and retention are handled by
the `runspec logs` command:

```bash
runspec logs deploy                 # merged view of every invocation, as one stream
runspec logs deploy --follow        # live tail across runs
runspec logs compact --older-than 7d --gzip   # roll old runs into an archive
runspec logs prune   --older-than 90d          # delete old files
```

Schedule `compact`/`prune` from cron or a systemd timer so retention is
automatic. See the shared-venv deployment guide for copy-paste schedules.

### Sharing the logs dir across accounts (`runspec logs share`)

A shared venv is owned by one account, the owner of the venv root (e.g. the
`svc` account `create-venv --run-as svc` built it as). Its runnables are also
run by other accounts: operators logged in as themselves, root (a
`run_as = "root"` runnable), other accounts' crontabs. None of them can write
the owner's `logs/`, so without help their runs fall back to their own
`~/logs`, where the venv's history (and every console) can't see them.

**Set up the logs inbox (recommended, runspec >= 0.66, POSIX).** Run this once
per venv, as the venv's owner:

```bash
sudo -u <owner> {venv}/bin/runspec logs share --inbox
```

It creates `{venv}/logs/inbox`: sticky, `01777`, owned by the owner. It works
like `/tmp` or a mail spool:

- Every other account (root included) writes its per-run file into the inbox
  and never touches the runs index. The file is created exclusively, never
  through a symlink, mode `0644`, and held under a lock while the run is live.
- The owner **sweeps** finished inbox runs into the runs index and moves their
  files into `logs/`. This happens automatically at the end of the owner's own
  runs; `runspec history sweep` does it on demand (or from cron).
- Until a run is swept, readers show it anyway: `runspec history list`,
  `runspec logs <runnable>`, and the console's History tab (tagged **inbox**).

No group, no ACL, no extra sudo rule. Run as root, `logs share --inbox` and
`history sweep` drop to the venv's owner first, so the standard
`enable-passwordless-sudo scoped` grant (which is `(root)` on the venv's `bin/`)
is enough. `runspec-linux`'s `create-venv` creates the inbox for you, and the
owner's (or root's) own runs create or repair it automatically as a safety net.
Opt a venv out with `runspec logs share --no-inbox` (a `logs/.no-inbox` marker);
add `--owner-only` to make `logs/` itself `0755`. `runspec history stats`
reports the inbox's state.

Trust model:

- A swept run's `os_user` (runs index v6) is the file's kernel-attested owner.
  The run summary's `user` stays self-reported.
- One account's file can never rewrite another account's row.
- Symlinks, hard links and FIFOs in the inbox are never followed, indexed or
  read.

Full design: [venv logs inbox](design/venv-logs-inbox.md).

**The older alternative: a shared group.** `runspec logs share --group
<group>` makes the dir group-writable, so a fixed set of accounts can write it
directly:

```bash
runspec logs share --group consoles   # chmod 2775 (setgid) + chgrp to `consoles`
runspec logs share                     # just make it group-writable + setgid, group unchanged
```

For each targeted dir, it:

- ensures the dir exists;
- `chmod`s it `2775` — group-writable **and setgid**, so files created in it
  inherit the dir's group;
- optionally `chgrp`s it to `--group`;
- repairs the existing `runspec_runs.db` (`chgrp` + `chmod 664`). A dir `chmod`
  never touches a file already inside it, so without the repair a group member
  still couldn't write an index a non-member created.

Like `prune`/`compact`, an unset `--scope` acts on this venv's own dir only
(`{sys.prefix}/logs`); `--scope home`/`all` target the shared `~/logs`/both.

It changes only what the invoking user is allowed to change, so run it as the
dir's owner: `sudo -u <owner> {venv}/bin/runspec logs share --group consoles`.
A non-member of the group still logs to `~/logs`, so add the accounts to the
group first. Once an inbox exists, group members use it too (only the owner
writes `logs/` directly).

**No root to add a group member?** Use `--acl-user` instead of (or with)
`--group`. On a host where you can't add an account to a group, the dir's owner
can grant an account write directly with a POSIX ACL:

```bash
sudo -u <owner> {venv}/bin/runspec logs share --acl-user alice
```

This sets an access ACL (recursive) plus a default ACL (new files inherit) so
`alice` can write the dir with no group change and no root. Best-effort — if
`setfacl` or filesystem ACLs are unavailable the share still succeeds (the
`--json` result's per-dir `acl` field says `unsupported`) and you fall back to the
group route. The console's "Make shared" passes your own login user here
automatically when it escalates to the owner, so its runs stop falling back to
`~/logs` with no manual step.

---

## Run summary

When `[config.logging]` is present, runspec emits a **run summary** at process
exit. This has two independently controlled parts:

- **The audit record** (`summary`, default **on**) — one structured JSON record
  written to the audit log. It captures the run's duration, exit code, log-event
  counts by level, the effective autonomy, whether it ran under an agent
  (`runspec serve` / `RUNSPEC_AGENT`), the **trigger provenance** (see below),
  the command path, and any uncaught exception. This is the data the console
  **history**/**analytics** tabs and `runspec logs` read, so it gives every
  invocation an audit-trail entry even when the tool logs nothing.

  **Provenance — `RUNSPEC_TRIGGER`.** A caller can set `RUNSPEC_TRIGGER` in the
  environment (e.g. `RUNSPEC_TRIGGER=schedule`) to record *what caused* the run;
  it lands in the record as `extra.trigger` (empty for an interactive/manual
  run). This is an axis **orthogonal** to `agent` — a single run can be both
  scheduled and an LLM turn — so a consumer can attribute and badge runs on both
  axes independently. runspec-console sets it on scheduled and event-triggered
  runs to show per-run icons.

  **Provenance — `RUNSPEC_RUNBOOK`.** A further orthogonal axis: a caller can set
  `RUNSPEC_RUNBOOK` to the id of a saved *runbook* (runbook) that drove the run; it
  lands as `extra.runbook` (empty otherwise). runspec-console sets it when a run is
  started from its `$` runbooks menu, so the History/Analytics tabs can attribute and
  filter runs by runbook.

- **The console echo** (`summary_console`, default **off**) — one human-readable
  line to **stderr**:

  ```
  runspec: deploy completed in 1.84s — 12 events (2 warn, 0 err)
  ```

The echo is **off by default** for two reasons: stdout stays clean for shell
pipes (`mytool --json | jq` is never polluted — and the line, when on, is on
stderr, which `|` doesn't capture either), and the line never lands on a stream
that **stream-classifying log processors tag as ERROR**. Rundeck, for example,
labels *everything* on stderr as ERROR regardless of content — so an
unconditional echo would paint a clean run red there. Leaving it off keeps
Rundeck/CI output clean with zero configuration; turn it on for interactive use:

```toml
[config.logging]
summary_console = true   # show the closing line in the terminal
```

(The level words are abbreviated `warn`/`err` purely to keep the line terse.)

Both parts are **package-level** controls in `[config.logging]` — there is no
per-invocation override flag on the runnable. Set `summary = false` to drop the
audit record (note: that also removes the run from the console
history/analytics), and leave `summary_console` off (the default) to keep the
terminal quiet.

!!! note "Availability"
    Run summaries landed in **runspec 0.12.x** and **node-0.12.0**. The
    `summary`/`summary_console` split (audit record always; terminal echo
    opt-in) arrived in **runspec 0.33.0** and **node-0.29.0**. The
    per-invocation `--no-summary` flag (and `RUNSPEC_*_ARG_NO_SUMMARY` env
    var) was removed in **runspec 0.36.0** and **node-0.32.0** — once the
    echo became opt-in, the flag mostly served to suppress the audit record
    the console depends on, so suppression now lives only at the package level
    (`summary = false`).

---

## Uncaught exceptions

You don't need `try`/`except` wrappers in your runnable. When `[config.logging]`
is present, runspec installs a process-level handler so **every** uncaught
exception is treated the same way — automatically. Just let exceptions
propagate.

The handler goes up at the *start* of `parse()`, so failures inside runspec's
own work are covered too — a bad type coercion, a validation slip, or reaching
for an argument that doesn't exist:

```python
args = parse()
action = args.no_such_arg   # AttributeError — caught and shown as a one-liner,
                            # not a raw traceback exposing runspec internals
```

On a crash, runspec always writes a structured record to the audit file (even
without `--debug`, even with `summary = false`), and gates the console output on
`--debug`:

```json
{"ts": "...", "level": "CRITICAL", "logger": "runspec.exception",
 "message": "uncaught exception",
 "exc": "Traceback (most recent call last):\n ...full traceback...",
 "exc_structured": {
   "type": "ValueError",
   "message": "invalid release tag 'v9.9.9-bad'",
   "module": "release",
   "frames": [
     {"file": "/abs/deploy.py",  "line": 48,  "func": "main",            "code": "main()"},
     {"file": "/abs/release.py", "line": 212, "func": "resolve_release", "code": "raise ValueError(...)"}
   ]
 },
 "extra": {"run_id": "..."}}
```

The `exc_structured` object is what UI tools render in a table — type, message,
and a frame list (innermost last, absolute paths for click-to-open).

On the console:

```console
$ deploy            # without --debug — one concise line
ERROR: ValueError: invalid release tag 'v9.9.9-bad'  (run with --debug for traceback)

$ deploy --debug    # neat, aligned trace; internal runspec frames filtered out
ValueError: invalid release tag 'v9.9.9-bad'

  deploy.py:48        main()
  release.py:212      raise ValueError(f"invalid release tag {tag!r}")
```

This reuses the existing `--debug` knob: the full traceback shows on the console
only with `--debug`, while the audit file always has the complete record.

!!! note "Behaviour change"
    Before this, an uncaught exception always dumped a full traceback to the
    console. It now prints the one-liner above unless `--debug` is set — the full
    traceback is always in the audit file. Landed in **runspec 0.26.0** and
    **node-0.21.0**. As of **runspec 0.29.1** the handler also covers exceptions
    raised inside runspec's own parse pipeline (it is installed at the start of
    `parse()`, not only at the end), so an undeclared-argument access or a
    coercion failure gets the same one-liner instead of a raw traceback.

---

## See also

- [Python Library](python.md) — `parse()` integration details
- [Node Library](node.md) — `getLogger` and ParsedArgs integration
- [Agent Integration](agents.md) — how `[config.logging]` behaves when
  invoked via `runspec serve`

---

**Next:** [Typed arguments](typed-args.md) — precise types, typos caught early.
