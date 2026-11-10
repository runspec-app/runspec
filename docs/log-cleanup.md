# Log cleanup (retention policy)

Every run of a runnable writes its own log file. On a shared venv, runs by other
accounts each land in `{venv}/logs/` as separate files, and nothing rotates
them. Scheduled jobs that fire every few minutes add up fast. A **log cleanup
policy** lets the venv tidy up after itself:

- it compresses old run logs;
- it deletes very old ones;
- optionally, it caps the folder's total size.

It needs runspec 0.80.0 or later in the venv. The console's
**Provision venv…** sets one by default.

## What a cleanup pass does

A pass works on `{venv}/logs/` in three steps:

1. **Delete** run files and archives older than *delete after*.
2. **Compress** the remaining run files older than *compress after* into gzip
   archives, one per runnable per day (`{runnable}.archive.{YYYYMMDD}.log.gz`).
   An archive's date is the newest run in it, so a day's runs are deleted
   together once all of them are old enough.
3. **Cap the size**: if *keep at most* is set and the folder is still bigger,
   delete the oldest files until it fits.

Archives are still JSON lines, so `runspec logs <runnable>` and the console's
History and Analytics keep reading them.

A pass never touches:

- the audit ledger (`audit.jsonl`);
- the runs index;
- the logs inbox;
- `~/logs`;
- a runnable's single rolling `{runnable}.log`, which rotates itself;
- anything outside `{venv}/logs/`.

Each pass is recorded on the venv's audit ledger as `logs.compact` and
`logs.prune` events.

## Who runs it, and when

No cron line is needed. At the end of any run by the **venv owner**, runspec
checks whether a pass is due. Passes run **at most once a day**, in a separate
background process, so the run that started it exits as normal. Scheduled fires
that run as the owner count, so a venv with shared schedules on it is cleaned up
every day.

Runs by other accounts never start a pass. A venv that only other accounts use
is cleaned up at the owner's next run. If the owner never runs anything there,
start a pass by hand (`runspec logs retention --now`, as the owner), or add a
daily crontab line for the owner that does it.

The policy file is ignored if it is writable by group or others, or owned by
anyone other than the owner or root. That stops another account from making
the owner delete files.

## Settings

The policy is `{venv}/runspec_logs_retention.toml`:

```toml
[logs]
compact_after  = "7d"     # compress run logs older than a week
delete_after   = "90d"    # delete run logs and archives older than about 3 months
max_total_size = "5GB"    # optional size cap
```

| Setting | Console label | What it does | Turn it off with |
|---|---|---|---|
| `compact_after` | Compress after | Run files older than this are compressed into daily gzip archives. | blank or `off` |
| `delete_after` | Delete after | Run files and archives older than this are deleted. | blank or `off` |
| `max_total_size` | Keep at most | After the two steps above, the oldest files are deleted until `logs/` fits. Only in **Log cleanup…** and the CLI. | blank or `off` |

- **Durations** are a number and `m`, `h`, `d` or `w`: `30m`, `72h`, `7d`, `2w`.
- **Sizes** are like `500KB`, `10MB` or `5GB`.
- **Compress after** must be shorter than **delete after** when both are set.
  Otherwise everything would be deleted before it could be compressed.
- **At least one** setting must be on. To have no cleanup at all, remove the
  policy instead.

Some common choices:

| You want | Compress after | Delete after | Keep at most |
|---|---|---|---|
| The default | `7d` | `90d` | |
| Keep everything, but small | `1d` | off | |
| Only delete, never compress (logs stay plain text and easy to `grep`) | off | `30d` | |
| A hard ceiling on a small disk | `1d` | `30d` | `2GB` |

## In the console

- **Provision venv…** (Settings → Jump Hosts) sets a policy as the last setup
  step. **Log cleanup** is ticked by default with 7d and 90d. Change either
  box, or leave a box blank (or type `off`) to switch that step off. Untick
  **Log cleanup** to give the venv no policy.
- **Update venv…** applies the same two settings to venvs that already exist.
  Its preview shows each venv's current policy. With **Set the log cleanup
  policy** ticked, the two boxes **replace** those settings: a blank box (or
  `off`) switches that step off. For example, leave *compress after* blank to
  stop compressing. Any **keep at most** already set is kept. Untick the option
  to leave each venv's policy as it is.
- **Logs** tab → a venv's logs → **Log cleanup…** shows the policy, when the
  last pass ran and what it did. You can:
  - change all three settings (blank or `off` switches one off);
  - clear every box and press **Remove policy**;
  - press **Preview now** to see what a pass would compress and delete today,
    without changing anything;
  - press **Apply now** to run a pass straight away instead of waiting.

The console makes every change by running the venv's own
`runspec logs retention` as the venv owner, so the change is recorded on the
venv's audit ledger.

## From a shell

As the venv owner (root switches to the owner first):

```bash
runspec logs retention                                          # show the policy and the last pass
runspec logs retention --compact-after 7d --delete-after 90d    # set it (other settings are kept)
runspec logs retention --compact-after off                      # stop compressing
runspec logs retention --max-total-size 5GB                     # add a size cap
runspec logs retention --now --dry-run                          # what a pass would do now
runspec logs retention --now                                    # run a pass now
runspec logs retention --off                                    # remove the policy
```

Each option only changes the setting it names. `off` clears a setting.

## Checking it worked

`runspec logs retention`, and the console's **Log cleanup…**, show the last pass:

- when it ran;
- how many run files it compressed and deleted;
- how much space it freed;
- any error.

The pass writes the same details to `{venv}/logs/.retention-last.json`. That
file's timestamp is also the once-a-day clock, so the next pass is due 24 hours
after it.

If no pass has run, check:

- **Has the owner run anything since the policy was set?** Only the owner's
  runs start a pass. Use **Apply now** to run one.
- **Is the file trusted?** `ls -l {venv}/runspec_logs_retention.toml` should
  show the owner (or root) as its owner, and no write bit for group or others.
- **Is runspec 0.80.0 or later?** Older versions ignore the file. **Update
  venv…** with **Upgrade runspec** fixes that.

If you'd rather use cron, run `runspec logs prune` and `runspec logs compact` from
a crontab instead. See [the CLI reference](cli.md#prune-compact-retention-by-hand).
