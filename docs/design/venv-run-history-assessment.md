# Run history on shared hosts — an assessment

**Status:** assessment for a decision. Nothing here is implemented; it examines
what we have and the alternatives before we build anything more.

**Why now.** Run history started simple: one venv, one account, one log
directory. It grew with the shared scheduler and fleet-wide history. We added a
SQLite runs index, then an inbox for accounts that can't write it, then a sweep
to move the inbox into the index, then a binding table to stop one account
rewriting another's rows. Then a fix for readers who can't open the index. Each
step was reasonable. Together they raise a fair question: are we on a road that
only gets more complex? This document steps back and looks at the problem from
every angle that matters: who owns the venv, who runs things, who runs as
someone else, and who reads the history. Then it compares the options,
including dropping the index and making `logs/` itself the inbox.

The guiding value: **a clean architecture beats a clever one.**

---

## 1. The Unix facts everything rests on

Every design below is bounded by these. None of them are runspec choices.

1. **Only an account with write permission on a directory can create a file
   there.** A run writes its history as the account it runs as, never "as the
   venv".
2. **SQLite needs every writer to write the database file *and* its directory**
   (for the `-wal`/`-shm` sidecars). In WAL mode even a *reader* needs to create
   `-shm` if it's missing; that is the 0.66.1 bug. So a shared SQLite file means
   either one writer, or a directory every writer can write.
3. **A sticky world-writable directory (`01777`, like `/tmp`) lets anyone create
   files, but only a file's owner — or the directory's owner, or root — can
   rename or delete it.** A file's owner is recorded by the kernel, so it can't
   be faked.
4. **`sudo -u X` only works where sudoers says so.** Some operators can become
   the venv owner, some can't, and some service accounts (e.g. an Ansible
   deploy account) nobody can become.
5. **Home directories are often closed.** Debian 12 and later create homes
   `0700` by default. A venv inside someone's home is invisible to every other
   account, including the account the console connects as.
6. **A process can only read what its account can read.** The console reads
   history over SSH as the account it logs in with, and it doesn't escalate to
   read.

## 2. The actors

| Actor | Example | Can write the venv's `logs/`? |
|---|---|---|
| **Owner** | the account that owns the venv root | yes |
| **Sudo operator** | a person who can `sudo -u owner` | as themselves no; as the owner yes |
| **Plain operator** | a person who can run the venv's scripts but can't become the owner | no |
| **Root** | a `run_as = "root"` runnable, or `sudo` | yes, technically; but a root-written index or `logs/` locks everyone else out |
| **Other crontabs** | a schedule in someone else's crontab | as whoever owns that crontab |
| **The reader** | the console's SSH login account; `runspec history` run by anyone | read-only |

"Running as another user" (`run_as`, or the Forms tab's per-invocation
`--run-as`) just changes which of these rows a run falls in. A run as `svc` is
an owner run; a run as root is a root run.

## 3. What we have today (core 0.67)

```
{venv}/logs/                 owner-only (0755)
    runspec_runs.db          the runs index — only the owner writes it
    <runnable>.<ts>.<id>.log per-run audit files (or one rotating <runnable>.log)
    inbox/                   01777 sticky — every non-owner drops its per-run file here
```

- **Owner runs** write a log file to `logs/` and a row to the index.
- **Everyone else** writes a per-run file to `logs/inbox/`, held under a lock
  while the run is live, and never touches the index.
- **The sweep** runs only as the owner, or as root dropping to the owner. It
  validates each finished inbox file, adds a row to the index, and moves the
  file into `logs/`.
- **Readers** show the index plus the inbox, with inbox rows marked pending.
- **Fallback.** Without a usable inbox, a non-owner's run falls back to its own
  `~/logs`, which is invisible to everyone else.
- **The console** reads, as its login account, each venv's `logs/` and inbox
  plus that account's own `~/logs`. It copies what it reads into its own local
  history cache (a SQLite file on the operator's machine). History and Analytics
  are served from that cache.

Moving parts: the index (schema v6, change sequence, tombstones), the inbox
writer, the sweep, the binding table, liveness by lock, the index-plus-inbox
read path, the read-only open fallback, and inbox provisioning and repair. That
is roughly 2,400 lines of core (`runsdb.py` + `inbox.py`), plus the console's
mirrored snippets.

## 4. The scenarios

These are your three examples, plus the two variants that always turn up.

### S1 — A plain user's venv in their own home

*A user with no service-account privileges puts a venv in `~/venvs/tools`
because they can't create a shared directory.*

- **Who can run it:** only that user. Other accounts can't even reach the path
  (a `0700` home, fact 5).
- **History today:** single writer; `logs/` and the index work perfectly. The
  inbox is never needed.
- **Console:** it sees the history only if it connects **as that user**.
  Connected as anyone else, both the runnables and the history are invisible,
  and that's correct.
- **Verdict:** simple in every design. It's the baseline: whatever we choose
  must add *nothing* here.

### S2 — A service-account venv nobody can become

*The Ansible deploy account creates `/opt/venvs/app`. Operators can run its
scripts but can't `sudo -u ansible`.*

- **Who can run it:** every operator as themselves; root for `run_as = "root"`;
  the deploy account itself only when Ansible runs something.
- **History today:** every human run goes through the inbox. The sweep happens
  only when the owner runs something, and the deploy account rarely does. So
  runs stay **pending forever** unless someone adds a cron sweep *as the deploy
  account* (which the deploy tooling would have to install) or a root sweep.
- **Console:** a plain operator's console sees the pending runs, and the index
  if it has ever been swept. Without an inbox, every run lands in each
  operator's own `~/logs` and nobody sees anyone else's.
- **Verdict:** this is where the owner-centric design strains. The owner is
  the *least* active account, yet it is the only one allowed to finish
  recording history.

### S3 — A service-account venv some users can become

*Your current practice: `svc` owns the venv; some operators can `sudo -u svc`,
others can't.*

- **Who can run it:** sudo operators as `svc` (an owner run), or as
  themselves; plain operators as themselves; root.
- **History today:** two paths at once. Owner runs go straight into the index;
  everyone else goes through the inbox and waits for a sweep. The same runnable
  appears as indexed or pending depending on *who* ran it.
- **Console:** any operator sees both, merged. The sweep problem from S2 is
  milder here because owner runs happen, but it's still tied to someone
  happening to run something as `svc`.
- **Verdict:** works, but every reader and the sweep must understand both paths.

### S3a — The same venv, run as root

A `run_as = "root"` runnable or `sudo` without `-u`. Root isn't the owner, so it
uses the inbox too. Root *could* write `logs/` directly, but a root-created
`logs/` or index would lock the owner out; core 0.65 hands such a directory to
the owner for exactly that reason.

### S4 — The console connects as someone else

The console's SSH account is independent of the scenario. It sees history
exactly as far as that account can read it: everything in S2/S3 if `logs/` is
world-readable, nothing in S1 unless it *is* the home's owner. It can't see
other accounts' `~/logs` fallbacks in any scenario.

## 5. What the runs index actually buys us

The index is the source of the single-writer requirement, and so of the inbox
sweep, the binding table and the owner-only rule. Here is what depends on it,
and what replaces it if it goes:

| Feature | Needs the index today? | Without it | Cost of losing it |
|---|---|---|---|
| History list / filter | yes (SQL) | scan per-run files; the timestamp in the file name prunes by date without opening them | slower on very large histories, but still only one directory listing |
| Console incremental sync | yes (change sequence) | cursor on file name (timestamp + run id); a finished file is immutable | same effect; must treat still-running files as "re-read until finished" (already done for the inbox) |
| "Running now" + crashed detection | yes (pid + reaper) | a held lock = running; no summary and no lock = crashed (the inbox already does this) | none — the lock is *more* reliable than the pid, and needs no reaper |
| `runspec exec --skip-if-running` | index + inbox | scan live, locked files for the label | small scan |
| `runspec exec --lock` | no (a lock file) | unchanged | none |
| Scheduler `status` | yes | read the newest file for the label | small scan |
| `history stats` / `prune` | yes (rows) | directory totals; prune by file age | row-level stats go |
| Rows that outlive their log file | yes | not possible (the file *is* the record) | none worth keeping |
| Analytics | no — the console computes it from its own cache | unchanged | none |
| Console History tab | no — served from the console's cache | unchanged | none |

What we can say with confidence: **no user-facing feature needs a database on
the host.** The console already has the database that users look at. The host
index is a pre-computed copy of the log files that speeds up host-side queries,
and it's the source of most of the complexity.

## 6. The options

### A. Keep the status quo (index + inbox + sweep)

- S1 fine; S3 fine; S2 needs a cron sweep as the deploy account, or pending
  runs accumulate.
- Complexity: highest. Two write paths, two read paths, a sweep, bindings, and
  SQLite permission edge cases (we have fixed three so far).
- Honest take: it's correct and it's tested, but every future feature, such as
  the shared scheduler or retention, has to reason about both paths.

### B. Everything runs as the owner (enforced)

Require every run to be `sudo -u owner` (`enforce_run_as`), so there is a single
writer again and no inbox.

- S1 fine; S3 fine *for sudo operators only* — plain operators can no longer
  run the venv at all; **S2 impossible**, because nobody can become the deploy
  account.
- Good for governance (sudoers becomes the access list), but it can't be the
  history design, because it doesn't cover S2. Keep it as an *optional* policy
  on top of whatever we pick.

### C. No host index — `logs/` itself is the inbox

Every run, including the owner's, writes one per-run file into `logs/`.
For a shared venv, `logs/` is `01777` sticky; for a personal venv (S1) it stays
`0755`. There's no SQLite file on the host, no sweep and no bindings.

```
{venv}/logs/                    01777 sticky (shared) or 0755 (personal)
    <runnable>.<ts>.<id>.log    one file per run, owned by whoever ran it
```

- **S1:** identical to today minus the index. Nothing new.
- **S2:** every operator's run lands in `logs/` straight away and is final. The
  deploy account never has to do anything.
- **S3:** sudo runs and plain runs are the same kind of file; only the owner
  differs, and the owner is kernel-attested.
- **S3a:** root's file is just another file; nothing gets locked out.
- Complexity: one write path, one read path, one format. Details in §7.

### D. The console is the database (combine with C)

The console already keeps the queryable history (its cache) and serves History
and Analytics from it. Under C, the host keeps only the files, which are the
record. The console pulls new files by cursor and indexes them locally.
`runspec history` on the host reads files directly. This isn't a separate
option so much as recognising where the database already lives.

### E. Push to a collector (optional layer, later)

Runnables, or `runspec exec`, send each run's summary to a central endpoint
(OTLP logs, since the console already speaks OTLP for metrics). That gives a
fleet-wide history without any shared host directory. It needs network access
and a collector, so it can only be an *addition* for sites that want it, never
the baseline. Files stay the local record.

### Rejected before, still rejected

- **One SQLite file per user** (shards): readers must open files written by
  arbitrary accounts, and retention is scattered.
- **journald or syslog**: Linux-only, reading needs a group, and retention is
  outside runspec.
- **A group-writable `logs/`**: the old answer. It needs group admin on every
  host, depends on every account's umask, and lets any member rewrite anyone's
  history.

(Details in `venv-logs-inbox.md`.)

## 7. Option C in detail — what "just an inbox" looks like

**Writing.**
- Every run, as whoever runs it, creates `logs/<runnable>.<ts>.<id>.log`.
  It's created exclusively, never through a symlink, mode `0644`, and holds a
  lock for the run's lifetime. It starts with `run_start` and ends with
  `run_summary` or `run_end`. This is exactly today's inbox writer, used for
  everyone.
- The rotating single `<runnable>.log` mode goes. Per-run files are the only
  format.
- `runspec exec` appends its observed exit code and captured output to the same
  file, as it does today.

**Reading.**
- List the directory. File names carry the UTC start time, so a date window
  filters by name without opening anything.
- Open only the files in the window, and at most the first and last records
  (`run_start` and the summary).
- A file is **running** if its lock is held, **finished** if it has a summary,
  and **crashed** if neither. No pid checks and no reaper.
- Attribution is the file's owner, from the kernel.

**The console.**
- It keeps a per-venv cursor: the newest finished file name it has read.
- Each pull lists names past the cursor, plus any files still running at the
  last pull. That is the same incremental behaviour as the change sequence,
  with no database on the host.
- History and Analytics are unchanged, because they already read the console's
  cache.

**Retention without an owner.** In a sticky directory each account may delete
its own files, and the owner or root may delete any. So:
- each run's finalize step prunes **its own account's** files older than the
  retention window (throttled, like today's sweep throttle);
- the owner or root can prune everything, for example from the existing
  `runspec logs prune`;
- compaction (gzip) follows the same rule, and names its archives so they sort
  like the originals, which keeps the console cursor valid.

Nobody *has* to run anything as the owner.

**Security** keeps everything the inbox has today:
- exclusive create, no symlink following, and lock-based liveness;
- a size cap and a per-account file cap per read;
- attribution by file owner (kernel);
- no index anywhere near a world-writable directory, because there is no index.

The binding table becomes unnecessary: a file can only ever describe its own
run.

**What we lose — honestly.**
1. **Fast host-side SQL** over very large histories. For example, a venv with
   100,000 runs: `runspec history list --since 7d` lists 100,000 names and opens
   the ones in the window, instead of one indexed query. Directory listing is
   fast (ext4 hashes directory entries); it's noticeable only at that scale, and
   the console isn't affected because it queries its own cache.
2. **Row-level `history stats`** such as counts by status across all time, which
   become a scan. A directory size and file count stay instant.
3. **An existing, tested implementation.** The index, sweep and bindings would
   be retired. The inbox writer and reader, which is most of the hard,
   security-sensitive code, is kept.
4. **One migration.** Existing `runspec_runs.db` files stay readable for a
   transition period; old rows are shown alongside files, then retired.
5. **Disk use by any account** that can run the venv, which exists today with
   the inbox too. The sticky directory limits who can *delete* files, not who
   can *add* them.

**What we gain.**
- One write path, one read path, one format, the same on every scenario.
- No sweep, no owner obligation, no pending state.
- No SQLite permission or WAL edge cases on the hosts.
- A roughly 1,400-line module (`runsdb.py`) and the sweep and binding half of
  `inbox.py` retired.
- S2 — the scenario today's design handles worst — becomes the simplest case.

## 8. Recommendation

**C + D, with B available as an optional policy and E as a possible later
layer:**

- **On the host:** per-run files are the record. Personal venvs get `logs/`
  `0755`; shared venvs get `01777` sticky, set up by the existing
  `runspec logs share` command (the flag would change meaning). No host
  database.
- **In the console:** the existing cache is the queryable history, fed by a
  file-name cursor.
- **Governance** (who may run what) stays a separate, optional choice:
  `enforce_run_as` with sudoers, or the denylist. It no longer affects whether
  history works.

This is the only option where all four scenarios behave the same and nothing
depends on a particular account doing something. It removes more code than it
adds. The cost is host-side query speed at very large scale, which the console
doesn't feel.

Before committing, the lab tests in §9 should confirm two things:
- the file-scan read cost on a realistically large directory;
- the console's cursor sync against sticky directories written by several
  accounts.

## 8a. Update — shared venvs run only as their owner

A later clarification narrows the scenarios, and that changes the
recommendation.

- **A shared venv is always created by a service account.** Today that's the
  account your deployment automation becomes, which you can `sudo -u` into.
- **Only people who can `sudo -u` that account should run its tools.** Everyone
  else shouldn't run them at all.
- **S2 (an owner nobody can become) isn't a shared-venv scenario.** A
  deploy-only account doesn't host tools for others.

Under that rule every run of a shared venv is an **owner run**, and every venv
(personal or shared) has **a single writer**. That removes the reason for
the inbox, the sweep and the binding table. It also removes the reason to drop
the index, because a single writer is exactly what SQLite wants.

We don't have to predetermine anyone. The check happens in sudo at run time:
- The console escalates every run in a shared venv to the venv's owner
  (`sudo -n -u <owner>`, owner detected from the venv root).
- An operator sudoers allows just runs.
- An operator it doesn't allow gets sudo's refusal, which is shown as "you
  aren't permitted to run tools in this venv".
- Who is allowed is an administrator's sudoers or group change, e.g.
  `%tools-app ALL=(svc) NOPASSWD: /opt/venvs/app/bin/*`.
- The console never needs a list of users.

Belt and braces, so direct runs can't bypass it:
- **Filesystem:** the venv's `bin/` is `0750`, owned by `svc`, so nobody else can
  even execute the tools.
- **runspec:** `enforce_run_as` refuses a run by the wrong account.
- Both need small additions: a per-venv owner setting in the console, and a
  `run_as` value meaning "the venv's owner" (a SPEC change).

**Per-user ACLs are the wrong lever.** Granting each operator `setfacl` on
`logs/` makes every one of them a writer: multi-writer SQLite, umask and
default-ACL-mask pitfalls, and every new person needs a host change. A group ACL
removes the per-person step but keeps the multi-writer problems. sudo to a
single owner removes both.

**Revised recommendation:**
- **Adopt B (run as owner)** for shared venvs, enforced by sudo and the
  filesystem, together with **D** (the console is the database people look at).
- **Keep the host index** — it's now always single-writer.
- **Freeze the inbox**: leave it in place, harmless, and retire it in a later
  release once shared venvs are owner-only.
- **Option C** (no index) stays a valid simplification for later. It's no
  longer needed to make shared venvs work.
- **S3a (root runs)** remains the one exception: root can write anywhere, and
  core already hands anything root creates back to the owner.

## 9. Testing it for real — a lab plan (sketch)

The five Debian testing machines, with your admin account used only to *set up*
each scenario. The console then connects as a **plain, non-admin** account
(`op1`) created on every machine, so it sees what a real operator sees.

| Machine | Scenario | Setup |
|---|---|---|
| 1 | S1 — personal venv | as `op1`: `python3 -m venv ~/venvs/tools` (home stays `0700`) |
| 2 | S2 — owner nobody can become | a `deploy` system account owns `/opt/venvs/app`; no sudoers entry for it; `op1`/`op2` run scripts as themselves |
| 3 | S3 — mixed sudo | `svc` owns `/opt/venvs/shared`; `op1` can `sudo -u svc`, `op2` can't |
| 4 | S3a — root runs | as 3, plus a `run_as = "root"` runnable and `op1`'s passwordless root grant |
| 5 | Scale + S4 | a venv with a generated 100,000-run history; the console also connects as `op2` to show what a less-privileged reader sees |

For each machine, run the same script for every scenario:
1. Run a runnable as each account; as `op1` via `sudo -u`; from each account's
   crontab; and one run killed mid-flight.
2. Record where each run's file landed, and what `ps` and the sudo log show
   (the 0.67 regression check).
3. Record what `runspec history list` shows as each account.
4. Record what the console shows when connected as `op1`, and as `op2`.
5. Time a history read at 1,000, 10,000 and 100,000 runs.

Run this against today's build (option A) first, to capture the baseline and
the pain points. Then run it against a prototype of C, and compare.

## 10. Questions for you

1. **Scale.** Roughly how many runs a day does your busiest shared venv see?
   This decides whether the file-scan cost in §7 matters at all.
2. **Host-side history.** Do people run `runspec history` on the hosts, or is
   the console the only place history is looked at? If the latter, host query
   speed barely matters.
3. **S2 in practice.** Should plain operators be able to run a deploy-owned
   venv at all, or is that itself something to forbid (option B as a policy)?
4. **Retention owner.** Is "each account prunes its own old runs" acceptable,
   or must one account control retention for audit reasons?
