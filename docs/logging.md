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

## See also

- [Python Library](python.md) — `parse()` integration details
- [Node Library](node.md) — `getLogger` and ParsedArgs integration
- [Agent Integration](agents.md) — how `[config.logging]` behaves when
  invoked via `runspec serve`
- [CHANGELOG](changelog.md) for 0.10.0 / node-0.9.0 / 0.11.0 / 0.12.0
