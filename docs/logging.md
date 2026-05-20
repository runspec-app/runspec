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
level  = "info"        # debug | info | warning | error | critical
rotate = "midnight"    # see "Rotation policies" below
keep   = 7             # number of rotated backups to keep
```

All three fields are optional. Defaults: `level = "info"`,
`rotate = "midnight"`, `keep = 7`. That's enough — `parse()` configures the
rest.

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

File logging is always on, console logging is on outside of agent mode.

| Surface | Path | Format | Level |
|---|---|---|---|
| **File** | `{package_dir}/logs/{runnable}.log` | Structured JSON | `DEBUG` (everything) |
| **Console** (`agent = false`) | stderr | `HH:MM:SS LEVEL logger: msg` | configured `level` |
| **Console** (`agent = true`) | *none — stderr is the MCP stream* | — | — |

`package_dir` is the directory containing `runspec.toml`. When that path is
not writable, runspec falls back to `~/logs/{runnable}.log` rather than
silently dropping log records.

The JSON file log captures **every** record at DEBUG regardless of the
console level — so when something goes wrong in production, you have the full
trace to inspect. The console level is just the live-tail filter.

```json
// excerpt from {package_dir}/logs/deploy.log
{"time":"2026-05-20T14:02:18.412Z","level":"INFO","logger":"deploy","msg":"Starting run for prod"}
{"time":"2026-05-20T14:02:18.430Z","level":"DEBUG","logger":"deploy","msg":"Resolved args","extra":{"target":"prod","dry_run":false}}
```

---

## Agent mode

When a runnable is invoked via `runspec serve`, `RUNSPEC_AGENT=1` is set in
the environment. The logging configurator detects this and:

- **Suppresses the console handler** — stderr is the MCP/SSH side-channel and
  log lines on it would corrupt the JSON-RPC framing.
- **Keeps the file handler** at DEBUG — the file log is your debugging
  interface for agent-driven invocations.

This is automatic. You write the same `logger.info(...)` calls; in human
mode they show up on stderr, in agent mode they go to the file only.

---

## Runtime override: `--log-level`

When `[config.logging]` is present, a `--log-level` argument is automatically
added to every runnable. It accepts the same five values as the `level`
field, defaults to the configured level, and is also settable via
`RUNSPEC_LOG_LEVEL`:

```bash
deploy --target prod --log-level debug
RUNSPEC_LOG_LEVEL=debug deploy --target prod
```

This is per-invocation only — it changes the **console** filter for one run;
the file log is unaffected (it's always DEBUG).

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

## What about `RUNSPEC_LOG_LEVEL`?

It's the same lever as `--log-level`, just delivered through the
environment. Useful when you can't easily add a flag — `runspec serve`
subprocesses, CI environments, container orchestration. CLI flag wins if
both are set.

---

## See also

- [Python Library](python.md) — `parse()` integration details
- [Node Library](node.md) — `getLogger` and ParsedArgs integration
- [Agent Integration](agents.md) — how `[config.logging]` behaves when
  invoked via `runspec serve`
- [CHANGELOG](changelog.md) for 0.10.0 / node-0.9.0 / 0.11.0
