# Shared venvs — the logs inbox

A **shared venv** is a venv that one account owns but several accounts run
runnables from: operators logged in as themselves, root (a `run_as = "root"`
runnable, or `sudo`), and other accounts' crontabs. The **logs inbox**
(runspec >= 0.66.1, Linux/macOS) makes sure every one of those runs lands in that
venv's history — `runspec history`, `runspec logs`, and the console's History /
Analytics tabs — with no groups, ACLs, extra sudo rules or daemons.

**Users need no sudo.** Anyone who can run the venv's runnables records their
runs in its history *as themselves* — they don't need to be able to `sudo` to
the venv owner, belong to a group, or have any other rights. The inbox is
world-writable (like `/tmp`), so their own account writes their run's file
there. Only the one-time set-up below is done as the owner.

!!! note "Since runspec 0.68.0 most runs don't need the inbox"
    Runnables now run as their venv's owner by default (`run_as = "@owner"`):
    the console, `runspec serve` and the MCP gateway escalate to the owner, and
    a direct run by anyone else is refused. So a shared venv's runs are
    normally all made by its owner and write `logs/` directly. The inbox still
    records runs by other accounts, i.e. root (`run_as = "root"`) and packages
    that opt out with `run_as = ""`.

This page explains how it works, how to set it up, how to check it's working,
and what to do when it isn't. The full design rationale (alternatives
considered, threat model) is in [the design doc](design/venv-logs-inbox.md).

!!! tip "The short version"
    As the account that owns the venv, with `umask 022`:

    ```bash
    python3 -m venv /opt/venvs/app
    /opt/venvs/app/bin/pip install "runspec>=0.66.1" <your runnable packages>
    /opt/venvs/app/bin/runspec logs share --inbox --owner-only
    ```

    That's it. You don't need `runspec-linux` for this.

---

## The problem it solves

Every runspec venv keeps its own history in `{venv}/logs`: one audit log file
per run, plus the **runs index** (`runspec_runs.db`, a SQLite file) that History
and Analytics read.

Only an account that can write `{venv}/logs` can record a run there. Without the
inbox, anyone else (an operator, root on a service account's venv) silently
falls back to **their own `~/logs`**. The run still happens, but it never shows
up in the venv's history, so other operators and every console miss it.

The old fix was to share `logs/` with a Unix group (`2775`, setgid). That needs
group admin on every host, every account in the group, and every account's
umask right, and any group member can rewrite anyone's rows. The inbox replaces
that with one directory that needs no setup beyond one command.

## How it works

### Who writes where

```
/opt/venvs/app/                 owner: svc   0755
└── logs/                       owner: svc   0755   only svc writes here
    ├── runspec_runs.db         owner: svc          the runs index (only svc writes it)
    ├── hello.20260929T….log    per-run logs (svc's own, and swept inbox files)
    └── inbox/                  owner: svc   01777  anyone drops their OWN run file here
        └── hello.20260929T….log     owner: alice, 0644
```

- **The owner** is the account that owns the venv root directory (here `svc`).
  It is the only account that ever writes `logs/` or the runs index. Its own
  runs write there directly, exactly as before.
- **Everyone else** — operators, root, other crontabs — writes its per-run log
  file into `logs/inbox/` and **never touches the runs index**.

`inbox/` is mode `01777`: world-writable with the **sticky bit**, like `/tmp` or
a mail spool. Anyone can create a file in it, but only a file's own creator
(and the directory's owner) can rename or delete it. So alice can't touch bob's
run file, and bob can't touch alice's.

### The life of an operator's run

1. **Start.** alice runs `hello`. runspec sees that alice isn't the venv owner
   and that the inbox is healthy, so it creates
   `logs/inbox/hello.<timestamp>.<run-id>.log` — exclusively (never reusing or
   following an existing name or symlink), mode `0644` whatever alice's umask.
   It holds an exclusive **lock** on that file for the whole run; the lock is
   how readers know the run is still live. The first record is `run_start`.
2. **Run.** Log records go to that file as usual.
3. **Finish.** The last record is `run_summary` (or a slim `run_end` when
   `[config.logging] summary = false`), and the lock is released.
4. **Visible immediately.** Readers (`runspec history list`, `runspec logs`, the
   console) read the inbox as well as the index, so the run shows up straight
   away — marked **pending** (the console tags it **inbox**).
5. **Sweep.** Later the owner **sweeps** the inbox: each finished run is added
   to the runs index and its file is moved from `inbox/` into `logs/`. From
   then on it's an ordinary indexed run. A run whose lock is still held is
   never swept.

### When the sweep happens

The sweep only ever runs **as the owner** (root is dropped to the owner first):

- **Automatically** at the end of each of the owner's own runs — at most once a
  minute, and for at most 2 seconds, so it never slows a run down noticeably.
- **On demand** with `runspec history sweep` (as the owner, or as root).
- **From the console**: Logs → Runs index… → **Sweep inbox**.
- **From cron**, if the owner is a service account that never runs anything
  itself (see [below](#a-service-account-that-never-runs-anything)).

The sweep is housekeeping, not what makes runs visible. Nothing is lost if no
sweep happens for a while — unswept runs stay visible as pending in every
reader. The sweep just turns them into permanent index rows and keeps the
inbox small.

### Set-up happens automatically too (as a safety net)

The owner's and root's own runs check the inbox on every run (one `lstat`), and
create or repair it if it's missing or has the wrong mode. So a venv whose owner
has run anything at all already has an inbox. The explicit set-up command below
is still the documented step, because until *something* has run as the owner or
root, operators' runs fall back to `~/logs`.

To opt a venv out entirely: `runspec logs share --no-inbox` (writes a
`logs/.no-inbox` marker; the automatic set-up respects it).

---

## Setting up a shared venv

### Plain venv (no runspec-linux needed)

Do all three steps **as the account that will own the venv**, with
**`umask 022`**:

```bash
sudo -u svc sh -c 'umask 022 &&
  python3 -m venv /opt/venvs/app &&
  /opt/venvs/app/bin/pip install "runspec>=0.66.1" my-runnables'

sudo -u svc /opt/venvs/app/bin/runspec logs share --inbox --owner-only
```

Output:

```
logs inbox created: /opt/venvs/app/logs/inbox (01777) — other accounts' runs now reach this venv's history; /opt/venvs/app/logs is owner-only (0755)
```

Why each piece matters:

- **As the owner.** Whoever creates the venv owns it, and the owner is the one
  account that writes the history. `logs share --inbox` refuses to run as any
  other account except root (root drops to the owner automatically).
- **`umask 022`.** Service accounts reached with `sudo -u` often get umask
  `027`, which makes the venv `drwxr-x---`. Other accounts then can't even
  *execute* its runnables (`Permission denied`), let alone log. `022` keeps
  directories `0755` and files `0644`. Use it for later `pip install`s into the
  venv too.
- **`--owner-only`.** Forces `logs/` to `0755`, whatever umask it was created
  with. Other accounts must be able to *traverse* `logs/` to reach `inbox/` and
  to read the index. (Without the flag, `--inbox` leaves `logs/`'s mode alone.)

Check it looks like this:

```
$ ls -ld /opt/venvs/app /opt/venvs/app/logs /opt/venvs/app/logs/inbox
drwxr-xr-x 6 svc svc 4096 … /opt/venvs/app
drwxr-xr-x 3 svc svc 4096 … /opt/venvs/app/logs
drwxrwxrwt 2 svc svc 4096 … /opt/venvs/app/logs/inbox
```

`drwxrwxrwt` (`01777`) owned by the venv owner is the only shape runspec
accepts for the inbox.

### With runspec-linux

If you already manage venvs with [`runspec-linux`](https://pypi.org/project/runspec-linux/)
(0.18+), `create-venv --run-as svc` does all of the above for you — it
normalises the umask, and creates the inbox (`--no-inbox` opts out).
`install-into-venv` keeps the permissions intact on later installs.

### From the console

For a venv on a connected host, **Logs → Make logs shared** with the group left
blank runs `runspec logs share --inbox` on it, escalating to the venv owner (or
root, which drops to the owner) as needed.

### An existing venv

Run the same command on it as the owner:

```bash
sudo -u svc /opt/venvs/app/bin/runspec logs share --inbox --owner-only
```

Upgrade `runspec` in the venv to 0.66.1+ first (0.66.0 has the inbox, but its readers can't open a swept index from a non-owner account). If the venv was built with a
restrictive umask, also open it up (as the owner): `chmod -R go+rX /opt/venvs/app`.

---

## Checking it works

Run something as another account, then look from that account:

```
$ sudo -u alice /opt/venvs/app/bin/hello
$ sudo -u alice /opt/venvs/app/bin/runspec history list --scope venv
✓ 2026-09-29 20:35:49  completed    0.00s  hello exit=0  [alice@web-01 via root]
```

The bracket shows the account the run executed as (`alice` — read from the
file's owner, so it can't be faked), plus the self-reported invoker when that's
different (`via root`, because root ran `sudo -u alice`).

The file is waiting in the inbox:

```
$ ls -l /opt/venvs/app/logs/inbox
-rw-r--r-- 1 alice alice 1221 … hello.20260929T203549Z.660c936d-….log
```

`runspec history stats` shows the whole picture:

```
$ sudo -u alice /opt/venvs/app/bin/runspec history stats --scope venv
/opt/venvs/app/logs/runspec_runs.db
   1 row(s) (completed=1), 56.0 KiB incl. WAL, schema v6
   oldest 2026-09-29T20:35:49  newest 2026-09-29T20:35:49
   change seq 1 (floor 0, 0 tombstone(s)) — incremental pull ready
   read-only for you — your runs go through the logs inbox
   inbox: 2 pending file(s), 2 finished awaiting sweep (2.4 KiB)
```

"read-only for you" is expected for everyone but the owner. Sweep as the owner
(or root):

```
$ sudo -u svc /opt/venvs/app/bin/runspec history sweep
✓  swept 2 run(s) into the runs index
```

The files are now in `logs/` (still owned by the account that ran them) and the
runs are ordinary index rows, readable by everyone.

---

## Commands

| Command | Who | What it does |
|---|---|---|
| `runspec logs share --inbox` | owner (root drops to owner) | Create or repair `logs/inbox` (`01777`, owned by the owner). |
| `runspec logs share --inbox --owner-only` | owner | …and force `logs/` to `0755`. |
| `runspec logs share --no-inbox` | owner | Opt this venv out (`logs/.no-inbox` marker). |
| `runspec history sweep` | owner (root drops to owner) | Index finished inbox runs and move them into `logs/`. |
| `runspec history list` | anyone | Runs, index + inbox (pending runs included). |
| `runspec history stats` | anyone | Index health plus the inbox's state and pending count. |
| `runspec logs <runnable>` | anyone | Log records, including runs still in the inbox. |

An operator (not owner, not root) running `logs share --inbox` or
`history sweep` gets a one-line refusal naming the owner:

```
✗  only the venv owner (svc) can sweep the inbox
```

### A service account that never runs anything

The automatic sweep happens at the end of the owner's own runs. If the owner is
a service account that only *owns* the venv and never runs anything itself,
nothing triggers it — runs stay pending (still visible) until someone sweeps.
Add a line to the **owner's** crontab (`sudo crontab -u svc -e`):

```
*/15 * * * * /opt/venvs/app/bin/runspec history sweep >/dev/null 2>&1
```

Or sweep from the console's Runs index… dialog when you want to.

---

## Troubleshooting

`runspec history stats` (run as the operator whose runs are missing) names the
problem. The common cases:

**"Permission denied" running a runnable at all.** The venv was created with a
restrictive umask (`drwxr-x---`). Fix as the owner:
`chmod -R go+rX /opt/venvs/app`, and use `umask 022` from now on.

**The run works but isn't in the venv's history; it's in `~/logs`.** The run
fell back because there's no usable inbox. `history stats` says:

```
no logs dir or inbox yet — other accounts' runs fall back to their own ~/logs. Set it up (as the venv owner): runspec logs share --inbox
```

(or `no logs inbox — …` when `logs/` exists). Run the set-up command. Runs
already in `~/logs` stay there; only new runs use the inbox.

**Other `history stats` messages:**

| Message | Meaning | Fix |
|---|---|---|
| `logs inbox disabled (.no-inbox marker)` | Someone opted this venv out. | Remove `logs/.no-inbox` (as owner), then `logs share --inbox`. |
| `logs inbox has the wrong mode` | Not `01777`. | The owner's next run repairs it, or run `logs share --inbox`. |
| `‼ logs/inbox is a symlink` / `is not a directory` / `is owned by another account` | Something other than a proper inbox is at that path. runspec won't use or touch it. | As the owner, remove it and run `logs share --inbox`. |
| `‼ logs/inbox is on another filesystem` | A mount at `logs/inbox`. The sweep moves files by rename, which can't cross filesystems. | Unmount it. |
| `‼ N subdirectory(ies) in the inbox` | Someone created directories in it. | Only root can remove a non-empty one. |

**Nothing is logged at all.** A runnable is only recorded when its
`runspec.toml` has a `[config.logging]` section (see [Logging](logging.md)).
`runspec exec <runnable>` always records, with or without it.

**Windows.** There is no inbox on Windows; runs are recorded where the running
account can write, as before.

---

## Safety

A world-writable directory is only safe if nothing trusts what's in it. The
sweep treats every inbox file as untrusted:

- **Attribution comes from the kernel.** A swept run's account (`os_user` in the
  index) is the file's owner on disk — it can't be forged. The `user` field in
  the run summary is what the process *said* and stays self-reported.
- **No cross-account overwrites.** A file can only add or complete *its own*
  account's run; it can never rewrite another account's row, even with a copied
  run id.
- **No tricks with special files.** Symlinks, hard links, FIFOs, devices,
  unreadable files and files over 64 MiB are never followed, read or indexed.
  Leftover junk is cleaned up after an hour.
- **Bounded.** At most 500 files per account per sweep; a record's timestamps
  can't claim to be newer than the file itself.
- **The index never lives in the inbox.** runspec refuses to open a runs index
  in any world-writable or sticky directory.

The residual risk is disk use: any account that can run the venv's runnables can
fill the inbox. The same is true of `/tmp`.

This is stricter than the group alternative, where every group member can
rewrite every row.

---

## See also

- [Logging](logging.md) — `[config.logging]`, log locations, retention
  (`runspec logs prune`/`compact` also cover swept files).
- [Hosts & fleet](console-hosts.md) — bootstrapping a host for the console.
- [CLI reference](cli.md) — `runspec logs share`, `runspec history`.
- [Design: venv logs inbox](design/venv-logs-inbox.md) — alternatives
  considered, the writer protocol, the full threat model.
