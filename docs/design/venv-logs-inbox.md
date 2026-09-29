# Shared-venv run history — a drop-box inbox with a single-writer index

> **Status: implemented.** The standalone fixes (§2) shipped in core 0.65.0 and
> the inbox itself (§3 onwards) in core 0.66.0 (`runspec/inbox.py`), with the
> linux, scheduler and console follow-ups listed in §13. It keeps the
> self-contained venv (everything a venv records lives under `{venv}/logs`;
> delete the venv and it's all gone), and changes only **who writes what** in
> there.
>
> **One deviation from §9:** when a `runspec exec` wrapper and its child are
> two *different non-owner* accounts (wrapper `alice`, child `run_as = "bob"`),
> the child can't append to the wrapper's file. It records its run under a
> fresh run id rather than collide with the wrapper's, so the run shows as two
> rows — as before the inbox. Every other combination is one row.

## 1. Problem

Every runspec venv is self-contained. Per-run JSON-lines audit logs, and the
runs index built from them (`runspec_runs.db` — the source for the console's
History tab, its cache and Analytics), live in `{venv}/logs`.

A shared venv is owned by a service account (`create-venv --run-as svc`). Its
runnables are also run by other accounts:
- operators logged in as themselves;
- root, via `run_as = "root"`;
- cron under other accounts.

Any account that can't write `{venv}/logs` **silently falls back to its own
`~/logs`**. Its runs disappear from the venv's history, and from every other
operator's console. The console reads over SSH as its own login user, so it
never sees another user's `~/logs`.

The only remedy so far has been permission admin on `logs/`: 2775 setgid with a
shared group (`create-venv --group`, `runspec logs share --group`), or per-user
ACLs (`--acl-user`). In practice that's hard to achieve. It needs group
membership managed per operator, re-logins, umask discipline, and ACL support.
The console's own "Make logs shared" escalates with `sudo -n -u <owner>`, which
the standard `scoped` sudo grant (`(root)` only) doesn't even allow.

The root cause is the **writer model**, not the location:
- **A single SQLite file with many writers** needs every writer to have write
  access to the file *and* its directory (for the `-wal`/`-shm` sidecars). That
  is exactly the group admin that's failing.
- Making it world-writable instead would let any local user plant sidecars, or
  rewrite anyone's history.

## 2. Standalone fixes (core 0.65.0 — shipped)

These made the existing model stop hurting people, independent of the inbox:
- A race-free write probe (`mkstemp`, not a fixed `.wtest`).
- Logging never fails a run: a private temp dir is the last resort.
- The rotating `single` file is only for the account that owns the logs dir.
  Everyone else logs per-run, which fixes a `PermissionError` crash.
- Root hands a `logs/` dir or runs index it creates to the venv owner, which
  fixes the root lockout.
- The runs index refuses world-writable or sticky dirs.
- `hidepid`-aware liveness.
- `compact` never deletes a file it couldn't read.

## 3. The design in one picture

```
{venv}/               owner svc 0755          "the owner" = owner of the venv root
{venv}/logs/          owner svc 0755 (2775)   only the owner writes here — incl. the index
{venv}/logs/inbox/    owner svc 01777         anyone drops *their own* per-run file (sticky)
```

This is the classic Unix drop-box: mail spools, `/var/spool/cron`, `/tmp`
itself. Many users contribute, one owner consolidates, and no shared group is
needed. runspec already has the right unit for it: the **per-run log file**
(`multi-writer-audit-logging.md`). It has one writer and a unique name, and it
ends with the `run_summary` the index is derived from.

- **The owner** writes directly, as today.
- **Everyone else** (operators, root, and group members too) writes its
  per-run file into `inbox/` and **never touches SQLite**.
- **The owner sweeps** finished inbox runs into the index and moves the files
  into `logs/`.
- **Readers** show index ∪ inbox, so a run is visible the moment it starts.

No groups, ACLs, sudo rules or daemons are needed.

## 4. Setting up the inbox

**Required step when you create a shared venv:** as the venv owner, run

```bash
{venv}/bin/runspec logs share --inbox          # or: sudo -u <owner> {venv}/bin/runspec logs share --inbox
```

- `runspec-linux create-venv` and the console's **Make logs shared** do this
  for you.
- Until it has run (or the safety net below has kicked in), other accounts'
  runs keep falling back to their own `~/logs`.
- The user-facing docs gain this step when the command ships (core 0.66.0).

**Safety net — core sets it up automatically.** On every run as the venv owner
or as root, core does a cheap check-and-set (one `lstat`):
- **Missing:** `mkdir(0700)`, then `chown` to the owner (when root), then
  `chmod 01777`. In that order, a root-owned world-writable directory never
  exists, even briefly.
- **Wrong mode:** the mode is re-applied.
- **Something else at the path** — a symlink, a non-directory, or a directory
  owned by another account: never touched; reported by
  `runspec history stats`.
- **Opt-out:** a marker file `logs/.no-inbox`, written by
  `create-venv --no-inbox` / `logs share --no-inbox`.
- **Quiet:** creation is recorded in the audit log only
  (`event=inbox_created`), never on stdout or stderr.

Operators can't create the inbox, because they can't write `logs/`. That's
exactly why the explicit setup step exists.

## 5. Writer protocol (non-owner accounts)

**Using the inbox.** A run uses it only when `logs/inbox` is sane:
- a real directory (checked with `lstat`, not a symlink);
- sticky, and writable by others;
- owned by the venv root's owner;
- on the same filesystem as `logs/`.

Otherwise the run falls back as today.

**Creating the file.**
- Opened relative to a handle on the inbox dir, with
  `O_WRONLY|O_APPEND|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC`.
- The name keeps the per-run pattern `{runnable}.{utc-ts}.{run_id}.log`. On a
  name clash it retries with a `-<8 hex>` suffix.
- `fchmod 0644`, so it stays readable under any umask.
- The file is always per-run, whatever `store` says.

**The lock.** The file is held under an exclusive `flock` for the life of the
process. The lock *is* the liveness signal (§7).

**The handler.** Writes go through a handler that holds the open file
descriptor and **never reopens by path**. Python's `FileHandler` reopens by
name after close, and in a world-writable directory that name could be replaced.

**The records.**
- **`run_start` first:** run_id, runnable, command, pid, host, proc_start,
  user, user_target, invocation args, start_ts, label, trigger, and the venv.
  It is emitted on a bookkeeping logger the console handlers and event counter
  already ignore.
- **`run_summary` last**, as today. When `summary = false`, a slim `run_end`
  instead, so a clean run is never mistaken for a crash.
- **No `run_start` when `[config.history] db = false`.** A `history_db: false`
  marker is written instead, and the sweep moves the file without indexing it.

**No SQLite writes**, ever, from a non-owner.

## 6. The sweep (owner only)

**When it runs.**
- On the owner's own finalize, after `_maintain`; throttled and time-boxed.
- `runspec history sweep` (and so cron if wanted).
- The console's **Runs index… → Sweep**.
- When run as root, `history sweep` and `logs share --inbox` **drop privileges
  to the venv owner** before touching anything. The standard `(root)` sudo
  grant is therefore enough.

**What it accepts.** A file is swept only if all of these hold:
- `lstat` and `fstat` agree on device and inode;
- it's a regular file;
- `st_nlink == 1`, so no hard links to files the writer couldn't read;
- it's world-readable;
- it's not owned by the owner;
- it's under the size cap.

Files are opened with `O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_NOCTTY`, relative to the
inbox dir handle, so a FIFO can't hang the owner's run. Symlinks, FIFOs,
sockets and empty directories are removed after a grace period.

**What it does with each file.**
- **Finished** (it has `run_summary`, `run_end` or `exec_result`): upsert the
  index row, then `rename` the file into `logs/`. The owner of a sticky dir may
  rename others' files.
  - The rename never overwrites a file already in `logs/`.
  - After the rename it re-checks the moved file is the one it validated (same
    device and inode).
- **Dead:** the lock is free *and* the death is confirmed — same host:
  `/proc` start token or `kill(pid, 0)`; other host: past `max_run_hours`.
  Indexed as `crashed` and moved.
- **Still running** (lock held): left alone, and **not indexed**. In-flight
  state comes only from reading the inbox.

**Attribution — the new schema v6 columns.**

| Column | Meaning |
|---|---|
| `os_user` | Kernel-attested: the file's `st_uid` for swept rows, the process's effective uid for direct rows |
| `source` | `direct` or `inbox` |
| `exec_uid` | For `runspec exec` binding |

`user` and `user_target` stay as they are (self-reported); the UI flags a
mismatch.

**Binding table.** One user's file can never overwrite another user's row. For
run_id R from a file owned by uid U:

| Existing row for R | Action |
|---|---|
| none | insert (`source=inbox`, `os_user=U`) |
| `source=inbox`, `os_user=U` | update |
| direct row whose `run_as` resolves to U (exec wrapper direct, child inbox) | fill empty columns only; never status / exit code / duration |
| direct row with `exec_uid=U` (wrapper inbox, child direct) | take the observed outcome from `exec_result`; fill label/trigger |
| anything else | reject; leave the file; count a collision |

**Other guards.**
- Timestamps are clamped to the file's kernel-set ctime, so a user can't flood
  the index with future-dated runs to evict real history through the row cap.
- A per-user cap on how many files one sweep will take.

## 7. Liveness

The pid alone is the trap: `/proc` hides other users under `hidepid`, and pids
get reused. The inbox uses the file lock instead:
- **Held:** the run is alive.
- **Free:** probably dead; confirm before acting (§6).

Readers probe with a shared, non-blocking lock on a read-only handle. On NFS,
flock is emulated with POSIX locks, which work there on a read-only handle.

## 8. Readers

**The contract: index ∪ inbox.**
- Read the inbox **before** the index, and dedupe by `run_id` with the index
  winning. A run swept between the two reads is then seen once, not zero times.
- One core parser, `parse_run_file()`, is shared by the sweep, inbox reads and
  `reindex`. It turns a file into a row-shaped dict.
- A per-run file contributes records only for its own run_id.

**The inbox is never a DB dir.** `log_dirs()`/`scope_dirs()` stay index-only.
The inbox is a read-only overlay for view and status. So prune, compact,
reindex and share never treat it as a DB dir, and never delete a pending file.

**Console.**
- One `log_layout()` helper replaces the ~7 duplicated dir-list computations.
- The stdlib snippets return pending inbox rows, mirroring the core parser, with
  a parity test like the existing `changes()` one.
- The History cache keeps them as a per-pull snapshot, deduped by run_id.
- Run detail fetches by the row's `log_file`, filtered to the row's attested
  owner.
- The shell log stream's frame marker gets a per-call nonce, so a file's content
  can't forge frames.

**Scheduler.** `status` reads index ∪ inbox, and accepts a `sched:<id>` inbox
row only from the schedule's own `run_as` account.

## 9. `runspec exec` in inbox mode

- **The file.** The wrapper creates the run's inbox file (and its `run_start`,
  with label, trigger and `run_as`), and holds the handle and lock. It hands
  the child `RUNSPEC_RUN_ID` and `RUNSPEC_RUN_LOG`. The child opens that same
  file to append; it checks the file is its own and has `nlink == 1`, and takes
  no lock.
- **After the child exits,** the wrapper appends `exec_output`/`exec_result`
  through its held handle, never by path.
- **`--skip-if-running`** reads index ∪ inbox, and trusts only runs by your own
  account, the owner or root, so a forged "running" file can't block a schedule.
- **Mixed modes** (wrapper direct and child inbox, or the reverse) resolve
  through the binding table (§6). This fixes today's "two rows" limitation for
  `run_as` children (`runspec-exec.md` §3.6).

## 10. Threat model

The inbox is world-writable. What stops misuse:

| Attack | Why it fails |
|---|---|
| Delete or rename someone else's run | Sticky bit: only the file's writer, the venv owner or root may |
| Trick a writer through a planted symlink or name | `O_EXCL\|O_NOFOLLOW`, unpredictable uuid names, fd-held writes. Doesn't rely on `fs.protected_*` sysctls. |
| Hard-link a file you can't read so the owner indexes or archives it | `nlink == 1` check |
| Hang the owner's run with a FIFO | `O_NONBLOCK`, regular files only |
| Rewrite another user's row via a forged run_id | Binding table |
| Evict real history with future-dated runs | Timestamps clamped to ctime; per-user caps |
| Plant SQLite sidecars or a fake DB | The index refuses world-writable or sticky dirs, and never lives in the inbox |
| Impersonate another account | `os_user` comes from the kernel (`st_uid`) |

This is stricter than today's group model, where any member can rewrite any row.

**Residual risks (documented, accepted):**
- Disk filling. Same as `/tmp`; there are caps and a grace-period cleanup.
- `user` (the human behind sudo) is self-reported. It always has been;
  `os_user` is the trustworthy field.
- A user can create a non-empty subdirectory in the inbox. The owner can't
  remove it, only root can, and it would block `rm -rf venv` for a non-root
  owner. It's reported by `history stats`.
- Swept files stay owned by their writer, so disk quota stays with the writer.
  The writer can still `chmod` or `touch` its own file afterwards: the same as
  today's 2775 dir. `compact` already skips an unreadable source (0.65.0).

## 11. Alternatives considered

| Option | Why not |
|---|---|
| Group/ACL-writable `logs/` (today) | Needs per-operator admin; doesn't cover root or filesystems without ACLs |
| World-writable `logs/`, or the DB in a world-writable dir | Sidecar planting and forged or rewritten history |
| Per-user DB shards | No central retention; each user's dir blocks the owner's cleanup; the owner opens SQLite files written by arbitrary users |
| Collector daemon on a Unix socket (kernel peer credentials, `SO_PEERCRED`) | Best attribution, but an always-on daemon per owner, and still needs a spool when it's down. Possible later hardening. |
| journald / syslog | Linux-only; reading needs a group; retention outside runspec |
| "Everyone runs as the owner" (`run_as` + sudo) | Still recommended for team runnables, but needs sudo rules per operator and can't cover personal-credential runs |

## 12. Compatibility and migration

- **Old writers** ignore the inbox, so their behaviour is unchanged.
- **Downgrading** is safe: files simply wait in the inbox.
- **Old readers** don't see pending runs until they're swept. The console
  raises its minimum core version to match.
- **Existing venvs**, in order:
  1. upgrade core, as the owner;
  2. run `runspec logs share --inbox` (or let the owner's next run create the
     inbox);
  3. optionally, run `runspec history sweep` once.
- **Group-shared venvs keep working.** Group members switch to the inbox once
  it exists (the single-writer rule), so their runs reach the index on the next
  sweep and are visible immediately via the inbox read.
- **Old `~/logs` runs** can't be migrated, because their records don't say
  which venv they came from. From now on, `run_start` records the venv.
- **Windows** has no POSIX sticky semantics, so it keeps today's behaviour.
- **The Node pack is deferred.** Its log dir is the npm project root and it has
  no runs index; the inbox records are Python-pack only for now.

## 13. Rollout

| Stage | Contents |
|---|---|
| core 0.65.0 (done) | Standalone fixes (§2) |
| core 0.66.0 | SPEC; inbox writer; auto-provisioning; schema v6; parser; sweep + binding; exec; `history sweep`; `logs share --inbox` |
| runspec-linux-core 0.12.0 + runspec-linux 0.18.0 | `create-venv`/`share-venv-logs` create the inbox (`--no-inbox`); `install-into-venv` keeps the `logs/` mode |
| runspec-scheduler 0.4.0 | `status` reads index ∪ inbox |
| runspec-console (next minor) | layout helper, snippets, cache, detail by `log_file`, stream nonce, Make logs shared → inbox, Sweep button, UI |
| docs | The **"set up the logs inbox"** step next to every venv-creation instruction: `console-hosts.md` bootstrap, `logging.md`, `cli.md`, `console-marketplace.md` |

**Verification** includes a real multi-user CI job:
- users `svc`, `alice` and `bob`, sharing one venv;
- the attacks in §10;
- `fs.protected_*` off;
- `hidepid=2`;
- parallel runs with sweeps looping alongside, which must give exact counts.
