# Hosts & fleet

The console runs runnables on remote machines over plain SSH. There are no
agents or daemons to install on the hosts — a host needs only an SSH login and
the `runspec` core library installed in one or more Python environments. The
console keeps one pooled, keepalive'd SSH connection per host and discovers
each host's runnables over it — by scanning the venv's site-packages for
`runspec.toml` files with one `find`+`cat` command (no Python starts on the
host) and normalising them with runspec's own loader, which yields exactly
what `runspec local --format json` would; that CLI is the per-venv fallback
when the scan can't be trusted (a Windows remote, no `runspec.toml` found, or
an editable install).

Add hosts either in **Settings → Jump Hosts** or by asking the agent — its
`manage_hosts` tool edits the same config behind a confirmation card. Once a
host is connected, everything on it — running, file transfer, log analysis,
denylist control — is agent-drivable too.

---

## Add a host

The fastest way is to tell the agent:

> Add a host called app-01: deploy@app-01.internal, runspec at /opt/venvs/ops/bin/runspec.

Approve the confirmation card (editing any detail first) and the fleet
reconnects and re-discovers. The same entry can be made in **Settings → Jump
Hosts**; either way it lands in `runspec_hosts.toml`:

```toml
[[host]]
name          = "app-01"                          # display name, used everywhere
ssh           = "deploy@app-01.internal"          # SSH target; omit for the local machine
runspec_paths = ["/opt/venvs/ops/bin/runspec"]    # one per venv to expose
group         = "web"                             # optional targeting group
profiles      = ["production"]                    # optional working-set tags
identityFile  = "C:/Users/me/.ssh/app-01_ed25519" # optional per-host key
```

- **`runspec_paths`** — each path is a `runspec` binary inside a venv on the
  host; every listed venv's runnables appear in the console. A host can expose
  several venvs.
- Authentication uses your SSH keys (per-host `identityFile`, the console's
  own key, or your `~/.ssh/config`). The console honours `ProxyCommand` from
  `~/.ssh/config` when `[ssh] use_ssh_config = true`.

On a large fleet the **Jump Hosts** tab opens on a **group overview** — one
row per group with its host count, how many are connected right now, and the
profiles its hosts belong to. Click a group to drill into its hosts (reorder,
test, copy key, edit, delete); the search box (name / hostname / user / group)
and the profile filter flatten the list across groups. The sidebar's title
row has a **collapse / expand all** toggle for the host groups (remembered
across restarts, and a group that appears later follows your last choice).
The **tool count** at the bottom of the hosts panel (*N tools · M venvs*) is
also the refresh control: click it to probe every host in the profile and
re-discover its runnables now — which is exactly what that number reflects
(hover shows the per-host breakdown). The sidebar's connection dots update
host-by-host as the sweep runs (coalesced to a few refetches), and a profile
switch or refresh takes over from a sweep already in progress instead of
waiting behind it.

Once saved, the host shows in the sidebar; the background refresh probes
connectivity and inventories its runnables. Ask the agent:

> What runnables do you see on app-01?

### Provision a venv from the console

A host with no runspec venv yet (or one that needs another venv) can get one
straight from **Settings → Jump Hosts**: **Provision venv…** in the toolbar
(pick any hosts, or **+ Group** / **+ Profile** to tick a whole set), the
cloud icon on a host row, or **Provision new venv…** under a saved host's
runspec paths. Nothing needs to be on the host except Python and SSH.

You choose:

- **Location**: a parent folder (browse the host, like finding an existing
  `bin/runspec`) plus a venv name, e.g. `/opt/venvs` + `ops`.
- **Python**: an interpreter the console found on the host (with its version),
  one you browse to in a non-standard location, or **Same version on each
  host** (e.g. `3.12`) so each host uses its own 3.12 wherever it lives.
  runspec needs Python 3.10 or newer, so an older interpreter is flagged and
  blocked (on RHEL 9 pick 3.12, not the system 3.9).
- **Run as**: the account that will own the venv, e.g. a service account
  (reached with `sudo -n -u` or `su`). Blank means your SSH user. Every step
  that runs as that account runs the chosen Python (or, later, the venv's own
  `bin/python` and `bin/runspec`), never a shell, so a sudoers rule that lets
  your login act as the account but refuses shells (`NOPASSWD: ALL, !/bin/sh,
  …`) is enough. Deprovision runs the venv's own `bin/python`.
- **Packages** (runspec is always installed) and the **package index** (a
  Marketplace repo, or the host's own pip config).

**Check hosts** runs a preflight on every host that changes nothing: the
interpreter runs and has `venv`, the account works without a password, the
location doesn't already exist and is writable. **Provision** then runs on the
ready hosts in parallel, each with its own log: create the venv (`--symlinks`,
`u=rwX,go=rX` permissions), upgrade pip, install the packages,
set up the [logs inbox](#set-up-the-logs-inbox-for-runs-by-other-accounts)
(`runspec logs share --inbox`), and check `bin/runspec` runs. Each host that
succeeds gets the new path added to its runspec paths; **Retry failed** re-runs
only the failures. Linux/macOS hosts only for now.

A host that fails after the venv folder was made (a misspelt package name, a
pip error, a cancel) has its partial venv undone, so a retry at the same path
isn't refused as "not empty": a folder the provision created is removed, a
folder that was already there empty is emptied again, and a folder that held a
kept `logs/` keeps it. The failure message says it was undone, or that it
couldn't be and you need to remove it yourself.

If the run-as account can't write the parent folder (say `/opt/venvs` belongs
to root, with each service account owning only its own venv inside it), Check
hosts asks whether your SSH login can `sudo` without a password. If it can, or
you log in as root, the host stays ready with a warning: Provision creates the
empty venv folder as root, gives it to the run-as account, and builds the rest
as that account. Without passwordless sudo it stays blocked; create the folder
and `chown` it to the account yourself, or pick a location the account can
write.

### Deprovision a venv

**Deprovision…** (toolbar) or the delete icon beside a saved runspec path
removes a venv from one or many hosts. Only a venv a host already lists can be
picked. The dialog shows each host's path, owner, size, the runnables in it,
and a warning if your triggers, schedules, saved runs or runbooks name them.
Type the venv's folder name to enable **Delete**.

- **Keep logs** (on by default) deletes everything except `logs/`, so the
  folder stays with its run history. Provisioning a new venv at the same path
  later builds around it and the history carries on.
- The host re-checks that the folder is a real venv (`pyvenv.cfg` +
  `bin/runspec`, not a symlink, not a system or home folder) in the same
  command as the delete, which runs as the venv's owner.
- The path is then removed from the host's runspec paths.

Provision and deprovision each write audit events (`venv.provision.*`,
`venv.deprovision.*`, `venv.logs.retained`) to the console's audit log. They're
also recorded on the host itself, as [audit events](audit.md):

- After a provision, the console runs `runspec audit record venv.provision`
  in the new venv as its owner. The venv's ledger (`logs/audit.jsonl`) starts
  with how the venv was made (who asked, the Python, the packages), and a copy
  goes to the host's syslog.
- Just before a deprovision deletes anything, it records
  `venv.deprovision confirmed` the same way. With **Keep logs** the ledger stays
  with `logs/`. Without it, the syslog copy is what remains. A venv whose
  runspec is older than 0.71.0 can't record that, so the console writes one
  `logger` line to syslog instead.

### Env files (`.runspec_env`)

Every runspec venv can hold a `.runspec_env` file in its root: plain
`KEY=value` lines that runspec loads into the environment before each run in
that venv (a variable already set in the environment wins). It's the usual
place for an instance URL, a region, or a token a runnable reads from
`RUNSPEC_<RUNNABLE>_ARG_<ARG>` or one of its declared `env` names. The
**Env files** button at the bottom of the hosts panel manages these files for
the local venvs and every venv on the hosts that are connected right now (in
the active profile).

**One venv** shows one file as a table:

- **Read by** lists the runnables in that venv that read each variable (the
  automatic `RUNSPEC_<RUNNABLE>_ARG_<ARG>` name or a declared `env` alias), so a
  stale variable stands out. **Add a variable** suggests the names this venv's
  runnables read that the file doesn't set yet.
- Values whose name looks like a secret (`…_PASSWORD`, `…_TOKEN`, `…_PAT`,
  `…_SECRET`, `…_KEY`, …) or that feed a `password` arg arrive masked. The
  console only fetches one when you press **Reveal**, and **Replace** sets a
  new value without ever showing the old one.
- **Save** sends only the keys you changed. The file is patched key by key, so
  comments, blank lines and the lines you didn't touch stay as they were. If
  someone changed the file after you opened it, the save is refused and the
  table reloads.
- Warnings point out a file that holds secrets but is readable by other
  accounts, a variable set twice (the last one wins), lines runspec ignores,
  and runnables that name their own env file in `runspec.toml` and so don't
  read this one.

**Across venvs** shows every venv's file at once as a grid: one row per
variable, one column per venv (host and venv name), with the variable column
pinned while you scroll sideways.

- Each cell shows that venv's value, or `–` when it doesn't set the variable.
  A variable whose venvs disagree is tagged **N values** (the odd ones out are
  highlighted), and **set in N of M** marks one that only some venvs set.
  **Only differences** hides the variables that are the same everywhere.
- Secret values stay hidden in the grid. The console still tells you whether
  they match: hidden values are compared by a fingerprint computed on the
  console (never sent anywhere, and useless outside the running console), and
  when they differ each cell gets a letter, so cells with the same letter hold
  the same secret. The eye icon reveals one cell.
- Click a cell to edit it (Enter keeps the edit, Esc drops it); the bin icon
  removes the variable from that venv. On a row, the pencil sets one value in
  every venv shown and the bin removes it from all of them. **Add a variable
  row** suggests names the venvs' runnables read. Edited cells are coloured
  (green added, amber changed, red removed) until you save or undo them.
- Venvs with the same name and identical files share one column, headed
  with the venv name and how many hosts it covers (for example `ops · 37 hosts`; hover the
  header for the list). Editing, undoing, removing or revealing a cell there
  does it in each of those venvs, so a column splits off as soon as one of its
  venvs differs. Venvs with different names never share a column, even when
  their files match. Turn **Group identical venvs** off to get one column per venv.
- Long values wrap rather than being cut off.
- Click a variable name to see every value it has across the venvs shown,
  with the venvs holding each value and those that don't set it. **Use
  everywhere** copies a value to every venv shown (as an unsaved edit).
- The venv picker narrows the columns to a venv name, a group, a profile or
  one host; type to search it. Venvs that can only be read show a lock in
  their header and can't be edited.
- **Save** lists the changes per venv first, then writes each file with just
  its own changed variables. Each file is only written if it hasn't changed
  since the grid loaded it, so a venv someone edited meanwhile is refused and
  reloaded (its edits stay, for you to check and save again) while the others
  are saved.

How the files are written:

- A remote file is read and written by the venv's own `bin/python` over the
  host's SSH connection. When the SSH login can't read or write it, the
  console retries as the venv's owner (`sudo -n -u <owner>`), then as root,
  the same way the [denylist](denylist.md) does. A scoped NOPASSWD rule on the
  venv's `bin/` covers it. If neither is allowed, the file opens read-only with
  the reason.
- The new text goes over the command's standard input, never its command line,
  and is renamed over the old file in one step. The file keeps its mode; a new
  file is created `0600`, owned by the venv's owner.
- Each change is recorded as `venv.env.changed` in the console's audit log and,
  with runspec 0.71.0 or newer, in the venv's own [audit ledger](audit.md) as
  the account that wrote it. Records carry variable names, never values. A
  **Reveal** is recorded as `venv.env.revealed`.
- Only the default file in the venv root is managed. Windows remote hosts
  aren't supported yet; local Windows venvs are.

### Bootstrapping a fresh host

If a host doesn't have runspec yet, use
[Provision a venv from the console](#provision-a-venv-from-the-console): it
creates the venv, installs runspec and your packages, sets up the logs inbox and
adds the new `bin/runspec` to the host's `runspec_paths`. Without the console,
the same steps by hand as the venv's owner are `python3 -m venv --symlinks`,
`pip install runspec …` and `runspec logs share --inbox`. (runspec-linux's
`create-venv` / `install-into-venv` runnables were removed in 0.20.0.)

#### Who runs a shared venv's tools

Tools run as the account that owns their venv (`run_as = "@owner"`, the default
since runspec 0.68.0). Discovery records each venv's owner. When that isn't the
account you log in as, the console runs the tool with `sudo -n -u <owner>`, so
the host's sudoers decides who may run it — typically a Unix group granted
`(svc-ops) NOPASSWD: ALL`. A venv you own runs with no sudo, and a root-owned
venv never escalates to root. Forms shows the account next to the runnable
("as svc-ops"). A package that must run as the operator sets
`[config] run_as = ""`.

Tools that need root (`runspec-linux-root`: package upgrades, `reboot-host`,
`set-sysctl`) declare `run_as = "root"` and belong in a **root-owned** venv, so
only root can change code that root runs. The console runs them with
`sudo -n -u root`, which needs a passwordless rule for the admin; runspec-linux
itself never runs anything as root.

#### Set up the logs inbox (for runs by other accounts)

A shared venv is owned by one account, but some runs are made by others — root
(`run_as = "root"`), packages that opt out with `run_as = ""`, other accounts'
crontabs. **Set up its logs inbox** so every one of those runs lands in the
venv's history (runspec >= 0.66.1):

```bash
sudo -u <owner> /opt/venvs/ops/bin/runspec logs share --inbox --owner-only
```

This creates `logs/inbox` (sticky `01777`, owned by the venv's owner): every
other account drops its per-run log file there, and the owner's runs index it.
No group, ACL or extra sudo rule is needed. Until it's set up, other accounts'
runs fall back to their own `~/logs`, where History can't see them.

- A venv built by hand works too. As the owner, with `umask 022`:
  `python3 -m venv`, `pip install "runspec>=0.66.1" …`, then the command above.
- The console's **Provision venv…** sets up the inbox for you.
- For a venv that already exists, run the command above, or the console's
  **Logs → Make logs shared** (leave the group blank).
- The owner's own runs also create it automatically, as a safety net.

Finished runs waiting in the inbox show in History with an **inbox** tag. The
owner's runs sweep them into the runs index, or use **Logs → Runs index… →
Sweep inbox**. The full walkthrough — how it works, checking it, and
troubleshooting — is [Shared venvs — the logs inbox](logs-inbox.md).

The older alternative is still there: `runspec logs share --group consoles` (or
**Logs → Make logs shared** with a group) makes `logs/` group-shared (`2775` +
setgid), so members of that group write it directly.

### Copy your SSH key to the fleet

**Settings → SSH → Copy key to all hosts…** authorises this machine on every
selected host over a one-time password connection (the password is never
stored) — useful right after a [Config Sync](console-team.md) pull hands you a
pre-configured fleet.

---

## Local venvs

The local machine (the synthetic `local` host) always discovers runnables in the
venv the console itself runs in. To discover **more than one local venv** — the
same multi-venv support remote hosts have — configure glob patterns in
**Settings → Local venvs**. Every directory a glob matches is scanned for a venv
(probed for `Scripts\runspec.exe` on Windows, `bin/runspec` on POSIX) and its
runnables appear under the `local` host, tagged with the venv's directory name
just like a remote host's venvs.

The globs live in `config.toml` as a `[local]` section:

```toml
[local]
venv_globs = ["C:\\venvs\\*"]
```

Because they live in `config.toml`, the globs ride [Config Sync](console-team.md)
and the config seed. That's the point: unlike per-machine absolute paths, one
versioned *convention* (e.g. `C:\venvs\*`) works across a fleet of identical
machines — each console scans its own filesystem and resolves the venvs it
actually has. The Settings tab shows a live preview of what the globs resolve to
on **this** machine (the console's own venv is always first). With no `[local]`
config, only the console's own venv is discovered — the default behaviour.

---

## File transfer & filters

The console moves files between hosts over its existing SSH connections. The
**Transfer files** dialog handles every direction from one place: **Source**
and **Destination** can each be *this computer*, a *configured host*, or an
**ad-hoc host** — an SSH machine not in your fleet, reached through a
configured host as a jump (password auth can reference a saved credential by
id). One local side transfers directly; two remote sides relay through the
console. You can also **rename the file on the target** instead of keeping
the source's name.

The agent has the same reach: `download_file` (remote → local, including
ad-hoc sources via `source_via` + `source_target`) and `relay_file`
(host → host), both with an optional `dest_name` to rename on target.

A **filter** is a named, reusable stream transform applied to the file **on
the source host** during a transfer, so only the matching output crosses the
network — the way to pull a few relevant lines out of a gigabyte log. Define
filters with **Manage filters…** in the Transfer files dialog; they're stored in
`runspec_filters.toml` (which rides [Config Sync](console-team.md), so a team
shares one set):

```toml
[[filter]]
name        = "id-lines"
pipeline    = "grep -E -- {{arg}} | cut -d, -f1,4"
description = "Lines matching a pattern, keep cols 1 & 4"
param_label = "id or regex"
```

How a filter runs:

- The source file is fed to the `pipeline` **on stdin**, through the source
  host's POSIX shell (`sh -c '<pipeline>' < '<file>'`). The pipeline is
  ordinary shell you author — pipes (`|`), `grep`, `sed`, `awk`, `cut`,
  multi-stage chains are all fine. For a download it runs on the remote host;
  for an upload or a relay's source side, wherever the source file lives (a
  local source needs `sh` on PATH — effectively remote-source on Windows).
- **One runtime value, not multiple.** The literal token `{{arg}}` marks
  where the operator-supplied value goes; `param_label` labels that input in
  the Transfer dialog (and tells the agent what `filter_value` means). The
  token may appear several times, but every occurrence gets the *same single*
  value — there is no `{{arg2}}`.
- The value is **shell-quoted on substitution**: it always arrives as one
  inert token, so it can't add pipeline stages or escape the command — only
  the pipeline you authored is interpreted shell. A filter whose pipeline has
  `{{arg}}` refuses to run without a value, and a value passed to a filter
  with no `{{arg}}` is an error (never silently dropped).

Because the quoted value lands inside your command as a single argument,
**regex alternation is how you match "multiple values"**: with the
`grep -E -- {{arg}}` filter above, supplying `12345|alice` as the value
matches lines containing either — the `|` is regex alternation *inside* the
pattern, safely quoted on its way there. (With `grep -F` the value is matched
literally, `|` included — use `-E`/`egrep` semantics when you want
alternation.)

Ask the agent:

> Download /var/log/app/audit.log from app-01 using the id-lines filter with the value 12345|alice.

### Saved transfers

A **saved transfer** is a pull you build once in the Transfer files dialog and
run again later, by hand or by the agent. Use it for the files you keep
fetching while you investigate, such as yesterday's app log from every web
host.

**Build one:** pick a remote source, choose the files, set *This computer* as
the destination and click **Save as scenario…**. The picked paths become the
scenario's path patterns, which you can edit:

- **Wildcards** `*`, `?` and `[..]` match on the source host, a folder at a
  time, like a shell glob (a leading `.` is only matched on purpose).
- **Placeholders** `{{name}}` take a *parameter's* value when the scenario
  runs. `{{name:FORMAT}}` reads the value as a date and formats it with
  strftime, so `{{day:%Y%m%d}}` becomes `20261028`. A date value can be
  `today`, `yesterday`, `-3d`, `+1d`, `-12h`, `-2w` or an ISO date. Built-ins
  that need no parameter: `{{today}}`, `{{yesterday}}`, `{{now}}` and
  `{{host}}` (the source host). A parameter's value always matches literally,
  even if it contains `*`.
- If a picked file name contains a date, **Turn 2026-10-28 into a date
  parameter** swaps it for `{{day:…}}` in the same format and adds a `day`
  parameter (default `yesterday`). **Also match rotated files** adds a `*`
  before the extension, so `app-2026-10-28.log` also matches
  `app-2026-10-28.1.log`.

The source can be one host, **every host in a group** (each host's files land
in a folder named after it), or an **ad-hoc host** (jump, user and a saved
credential are kept; a typed password never is). Files go to the chat's
workspace unless you pick a folder, with an optional subfolder that can use
placeholders too. **Newest** keeps only the newest N matches per path, and
**Max files** (default 50 per host) refuses a run that would fetch more,
rather than cutting the list short. If two matches share a file name, each
keeps its folder below the common parent so nothing is overwritten.

**Run one:** the **Saved** row at the top of the dialog lists them. **Run…**
asks for the parameters; **Preview** lists the matching files, sizes and
dates without copying anything.

Scenarios live in `runspec_saved_transfers.toml`, which rides
[Config Sync](console-team.md) (merged by `id`), so a team can share one set:

```toml
[[saved_transfer]]
id          = "app-logs"
title       = "App logs for a day"
group       = "web"                     # or host = "web-01", or adhoc = "db7.corp"
paths       = ["/var/log/app/app-{{day:%Y-%m-%d}}*.log"]
subdir      = "logs/{{day:%Y-%m-%d}}"
latest      = 0                         # 0 = every match, N = newest N per path
max_files   = 50
filter      = "id-lines"                # optional, with filter_arg = "{{txn}}"
autonomy    = "autonomous"              # what the agent may do: confirm (default) / autonomous / manual

[saved_transfer.params]
day = { default = "yesterday", description = "today, yesterday, -3d or a date" }
```

The agent runs them with `run_saved_transfer` (`id`, `values`, and
`preview = true` to list first). A run asks you first unless the scenario's
`autonomy` is `autonomous`; a preview never asks. The tool reports where each
file landed, so the agent can read them with the workspace tools straight
away. The console's MCP server offers the same tool, and a trigger can be
allowed to use it through its action allowlist.

> Use the app-logs saved transfer for last Tuesday, then find the first error after 14:00.

**Schedule one:** in the Schedules tab, pick **Saved transfer** as the action,
choose the scenario and fill its parameters. A date parameter set to
`yesterday` or `-1d` picks its own day each time the schedule fires. A
schedule has no chat, so a scenario that saves to the chat workspace saves to
`documents/transfers/<id>/` in the console's data folder instead, unless you
set **Save files to** on the schedule (an absolute folder, placeholders
allowed). Each run shows a card with the file count and folder, or what went
wrong. It runs without asking, like any schedule.

```toml
[[schedule]]
id             = "gather-app-logs"
action         = "saved_transfer"
saved_transfer = "app-logs"
values         = { day = "yesterday" }
transfer_dest  = "D:/collect/{{today:%Y-%m-%d}}"   # optional
when           = { cron = "15 6 * * mon-fri" }
```

---

## Groups

A host's `group` is a **targeting unit**: naming a group runs a runnable on
every member at once, behind a single grouped confirmation.

> Run apt-upgrade-check on the whole `web` group and summarise which hosts have pending security updates.

Group names are a controlled vocabulary kept in a registry
(`runspec_groups.toml`) that syncs across consoles, so a shared runbook
targeting `web` means the same thing everywhere. Manage them from the host
editor's group picker ("Manage groups") or via the agent (`manage_groups` —
"create a `db` group and put nas-01 and nas-02 in it"). Groups also work in
schedules and runbooks.

## Profiles

A **profile** is a working set: activate one and only its hosts are probed and
hold standing SSH connections — useful when your fleet spans environments you
don't want warm at once. The sidebar's profile switcher selects the active
profile (machine-local; definitions sync). Profiles scope only the
*interactive* view — schedules and explicitly-targeted runs still reach any
configured host.

---

## SSH tuning

The defaults suit most fleets. All knobs live in `config.toml` under `[ssh]`
and `[refresh]`:

```toml
[ssh]
pool           = true  # one reused connection per host (recommended)
keepalive      = 30    # seconds; SSH-layer keepalive
max_sessions   = 8     # concurrent channels per connection (< sshd MaxSessions)
idle_ttl       = 300   # close unused connections after this many seconds
max_concurrent = 5     # simultaneous NEW handshakes across the fleet
backoff_base   = 5     # per-host retry backoff after a failure (seconds)
backoff_max    = 300

connect_timeout = 10   # raise these behind slow proxies / VPNs
banner_timeout  = 30
auth_timeout    = 30

jump_max_concurrent = 4  # ssh connections at once through one jump host
jump_retries        = 2  # retries when a jump host refuses a connection

[refresh]
interval = 30          # seconds between background connectivity probes
jitter   = 3
workers  = 16          # bounded worker pool for every fleet fan-out
discovery_interval = 300  # seconds between per-host runnable re-discovery
```

`interval` is the **connectivity** cadence: every cycle probes each in-scope
host over its warm pooled transport (cheap). `discovery_interval` is the
**runnable re-discovery** cadence: `runspec local --format json` (one SSH exec
per venv) re-runs on a host only when it first connects, reconnects after a
drop, its `runspec_paths` / identity change, or this many seconds have passed
since its last discovery — plus on any explicit trigger (a host save, profile
switch, host import, copy-key, or clicking the tool count at the bottom of the
hosts panel). `0` re-runs
discovery every cycle. `workers` also bounds the other fleet-wide sweeps
(the hosts-panel Denylist modal / `runspec deny`).

The connection pool exists to avoid hammering bastions with handshake bursts
(which trips `sshd MaxStartups` and surfaces as *"Error reading SSH protocol
banner"*): a warm connection serves new commands over fresh channels with no
new handshake, and the refresh loop's thread count stays constant regardless
of fleet size. For very large fleets, raise `max_concurrent`/`workers`
gradually rather than disabling the pool.

### Hosts behind a jump host

A host with a **Jump host (ProxyJump)** set connects through the system `ssh`
rather than the connection pool, so every check and discovery opens a new
connection through the jump. The jump's sshd only lets a few unfinished logins
in at once (`MaxStartups`, default 10) and closes the rest before key exchange,
which shows up as *`kex_exchange_identification: Connection closed by remote
host`* and hosts that go green and then drop. So the console sends at most
`jump_max_concurrent` connections through any one jump host at a time and
retries a refused one `jump_retries` times after a short pause. Lower
`jump_max_concurrent` if hosts behind a busy jump still fail; raise it if the
jump allows more and many hosts sit behind it.

New host keys are accepted automatically for both the jump and the host behind
it, into the console's own known_hosts. PuTTY keeps a separate host-key cache,
so a PuTTY terminal may still ask about a key the console already accepted.

!!! tip "Behind a corporate proxy / middlebox"
    If a connection error mentions *"the remote sent non-SSH data on
    connect"*, a proxy or TLS middlebox is intercepting the SSH port. Point
    the console at an HTTP CONNECT proxy with
    `[ssh] proxy = "http://proxy.corp:8080"`, or set `use_ssh_config = true`
    to honour a `ProxyCommand` from `~/.ssh/config`.

---

## Fleet kill switch: the denylist

Each venv on each host has a [denylist](denylist.md) — a kill switch that
makes `runspec` refuse matched runnables at parse time, no matter how they're
invoked. The console drives it fleet-wide two ways:

- the **Denylist** button at the bottom of the hosts panel — a modal scoped to
  the hosts *connected right now* in the active profile (greyed out until the
  fleet sweep has settled, so its scope is the green dots you can see): pick a
  subset or act on all of them, disable/re-enable all, add rules, set
  enforce/warn/off. A disconnected host is never queried or touched — reopen
  once it reconnects;
- the agent's `manage_denylist` tool:

> Disable reboot-host on all production hosts. …later… What's currently denylisted across the fleet?

Listing is autonomous; changes are confirm-gated. The mechanism runs the core
`runspec deny` command over SSH — across hosts in parallel, bounded by
`[refresh] workers` — and the control path is exempt from its own denylist —
the console can always re-enable. A venv created as a service account
(**Provision venv…** with a run-as account) isn't writable by the login user, so a write that
comes back *permission denied* is re-run once escalated — as the venv owner via
passwordless `sudo -n -u`, else as root — and the result line says which
(`(as svc)`); see [Denylist → service-account venvs](denylist.md#venvs-owned-by-a-service-account).
