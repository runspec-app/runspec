# Hosts & fleet

The console runs runnables on remote machines over plain SSH. There are no
agents or daemons to install on the hosts — a host needs only an SSH login and
the `runspec` core library installed in one or more Python environments. The
console keeps one pooled, keepalive'd SSH connection per host and discovers
each host's runnables by running `runspec local --format json` over it.

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

Once saved, the host shows in the sidebar; the background refresh probes
connectivity and inventories its runnables. Ask the agent:

> What runnables do you see on app-01?

### Bootstrapping a fresh host

If a host doesn't have runspec yet, [`runspec-linux`](https://pypi.org/project/runspec-linux/)
(installed on any already-connected host, or locally) provides agent-drivable
bootstrap runnables — `which` (find an interpreter), `create-venv`,
`configure-pip` (private index), and `install-into-venv`:

> Create a venv at /opt/venvs/ops on app-01 owned by the svc-ops account, configure it for our internal PyPI mirror, and install runspec and runspec-linux into it.

Each step is confirm-gated. Then add the new venv's
`/opt/venvs/ops/bin/runspec` to the host's `runspec_paths`.

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
filters under **Settings → Filters**; they're stored in
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
`grep -E -- {{arg}}` filter above, supplying `12345|Jason` as the value
matches lines containing either — the `|` is regex alternation *inside* the
pattern, safely quoted on its way there. (With `grep -F` the value is matched
literally, `|` included — use `-E`/`egrep` semantics when you want
alternation.)

Ask the agent:

> Download /var/log/app/audit.log from app-01 using the id-lines filter with the value 12345|Jason.

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

[refresh]
interval = 30          # seconds between background refresh cycles
jitter   = 3
workers  = 16          # bounded worker pool for the refresh fan-out
```

The connection pool exists to avoid hammering bastions with handshake bursts
(which trips `sshd MaxStartups` and surfaces as *"Error reading SSH protocol
banner"*): a warm connection serves new commands over fresh channels with no
new handshake, and the refresh loop's thread count stays constant regardless
of fleet size. For very large fleets, raise `max_concurrent`/`workers`
gradually rather than disabling the pool.

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

- **Settings → Denylist** — pick hosts, disable/re-enable all, add rules,
  set enforce/warn/off;
- the agent's `manage_denylist` tool:

> Disable reboot-host on all production hosts. …later… What's currently denylisted across the fleet?

Listing is autonomous; changes are confirm-gated. The mechanism runs the core
`runspec deny` command over SSH, and the control path is exempt from its own
denylist — the console can always re-enable.
