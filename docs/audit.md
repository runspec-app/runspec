# Audit events

Every run of a runnable is already recorded: who ran it, with which arguments
(secrets left out), how it ended and how long it took. See [Logging](logging.md).

**Audit events** are for the moments *inside* a run that must be on the record
on their own, such as a refund issued, a host rebooted or an account unlocked.
You record one with a single call. It goes to an append-only, tamper-evident
ledger and to the operating system's log.

!!! info "Availability"
    `runspec.audit()` and `runspec audit` are available in **runspec 0.71.0+**
    (Python). The Node pack follows.

---

## Record an event

=== "Python"

    ```python
    import runspec

    def main():
        args = runspec.parse()
        refund = issue_refund(args.order.value, args.amount.value)
        runspec.audit(
            "payment.refund",
            "succeeded",
            target={"order": args.order.value},
            detail={"amount": str(args.amount.value), "refund_id": refund.id},
        )
    ```

=== "Shell"

    ```bash
    runspec audit record payment.refund --outcome succeeded \
        --target order=A-1043 --detail amount=12.50
    ```

- **`event`**: a dotted, lowercase name you choose, like `payment.refund`,
  `host.reboot` or `account.unlock`. Pick a `noun.verb` scheme and keep to it,
  because people will search by these names.
- **`outcome`**: a short word, such as `started`, `succeeded`, `failed` or
  `denied`. Optional.
- **`target`**: what was acted on. **`detail`**: anything else worth keeping. Both
  are JSON objects. Keys like `password`, `token`, `api_key` and `secret` are
  redacted, and so are secret-looking values such as `password=…` or a URL with
  credentials in it. Don't put secrets there anyway.

Inside a run, the event is tied to it automatically. It records the runnable,
the subcommand, the `run_id`, whether an agent ran it, and what triggered it.
It also appears in the run's log, next to the run, in History and in
`runspec logs`.

### When it must not go unrecorded: `strict=True`

```python
runspec.audit("payment.refund", "started", target={"order": order}, strict=True)
issue_refund(order, amount)   # only reached if the event was recorded
```

`strict=True` raises `runspec.AuditError` if the event couldn't be written to
the ledger, the OS log or the run log. Use it before an action that must not
happen without a record. From the shell, `runspec audit record … --strict`
exits 1 instead.

Without `strict`, `audit()` never raises because of a recording failure. It
only raises `ValueError` for an invalid event name, since that's a bug in the
calling code.

### A start/finish pair

For a long or risky action, record the attempt and the result separately. Then
a crash in between still leaves a `started` event with no matching end:

```python
runspec.audit("host.reboot", "started", target={"host": host}, strict=True)
try:
    reboot(host)
except Exception as exc:
    runspec.audit("host.reboot", "failed", target={"host": host}, detail={"error": str(exc)})
    raise
runspec.audit("host.reboot", "succeeded", target={"host": host})
```

---

## Where events go

| Place | What it gives you |
|---|---|
| `{venv}/logs/audit.jsonl` | The venv's **ledger**. It's append-only and hash-chained, and it is never rotated, pruned or compacted. |
| syslog / journald (Linux, macOS), Application event log (Windows) | A copy that the user who wrote it can't edit. It's usually already shipped to your central log system. |
| The run's own log | The event shows up next to the run in History and `runspec logs`. |

Each ledger line carries the hash of the line before it (`prev`) and its own
hash (`hash`). If a line is edited, deleted, inserted or reordered, the chain
breaks. The OS-log copy also carries the ledger hash, so cutting lines off the
end of the ledger shows up when you compare the two.

**Shared venvs.** Only the venv owner appends to the ledger. When another account
runs a tool in a shared venv, its events go into its per-run file in the
[logs inbox](logs-inbox.md). The owner's sweep then adds them to the ledger,
attributed to the account that actually owns the file. The name the writer
claimed is kept as `claimed_actor` if it differs.

To control the OS-log copy:

```toml
[config.audit]
syslog   = true     # default; false = ledger + run log only
facility = "auth"   # syslog facility (POSIX); default auth
```

There's no setting that turns the ledger off.

---

## Read and verify

```bash
runspec audit list                  # newest 50 events in this venv's ledger
runspec audit list --json -n 500    # raw records, one per line (pipe to jq)
runspec audit verify                # check the hash chain; exits 1 if broken
runspec audit verify --file /path/to/audit.jsonl
```

`verify` names the first bad line and says why: it was edited, the chain broke
(a line was removed, inserted or reordered), or it isn't JSON.

From Python: `runspec.auditlog.verify(path)` and `runspec.auditlog.read_ledger(path)`.

---

## What runspec records for you

You don't need to call `audit()` for these:

- **Every run**: the run record (`run_start` / `run_summary`).
- **`runspec deny`** changes: `denylist.change`.
- **Log retention and sharing**: `logs.prune`, `logs.compact` and `logs.share`.
  Deleting or rolling up run logs is itself recorded, and the ledger is never
  one of the files pruned.

---

## What it can and can't promise

Local records are **tamper-evident, not tamper-proof**. Root or a local
administrator can always delete a file or clear a log. The hash chain and the
OS-log copy make that kind of tampering visible, but they can't prevent it. For
a record nobody on the machine can change, ship syslog/journald, or the Windows
event log, to a central log system. For an extra local guard, root can make the
ledger append-only at the kernel level on Linux with `chattr +a {venv}/logs/audit.jsonl`.

---

## In tests

Set `RUNSPEC_AUDIT=off` in a test suite or CI job that runs your runnables
in-process. Runnables without a `[config.logging]` section then configure no
logging, and `audit()` records nothing. It's meant for tests only. Production
runs must never set it. `runspec.testing.ParseHarness` already skips the default
logging setup.
