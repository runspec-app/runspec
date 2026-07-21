# Team config — sync & rooms

One console is personal tooling; a team of consoles needs shared
configuration and shared channels. Two features cover this: **Config Sync**
(every console pulls the same versioned config from git) and **rooms**
(chat rooms and helpdesk queues served by a `runspec-room` server on one of
your hosts).

---

## Config Sync

Config Sync pulls the console's config files from an external git repository
**at a tag**, so a fresh install is fully configured from one versioned
source and updates are deliberate (bump the tag, everyone pulls).

```
pip install "runspec-console[git-sync]"
```

Configure under **Settings → Config Sync** (repo URL, tag, auth), stored as:

```toml
[config_sync]
repo = "https://git.corp.example/ops/console-config.git"
tag  = "v42"
```

What syncs — the known config files: `config.toml`, `runspec_hosts.toml`,
`runspec_groups.toml`, `runspec_profiles.toml`, `runspec_triggers.toml`,
`runspec_filters.toml`, `runspec_credentials.toml` (metadata only — secrets
stay in each operator's keychain), `runspec_runbooks.toml`,
`runspec_repos.toml`, `runspec_self_service.toml`.

How it merges — **non-destructively**:

- Collection files (hosts, triggers, credentials, runbooks, …) merge by
  identity (`name`/`id`): repo entries are added or updated; the operator's
  own local entries are kept.
- `config.toml` deep-overlays — local-only keys survive.
- A **`preserve`** list pins machine-local / operator-owned values so a pull
  (or a config-seed re-assert) never overwrites them. Each entry is a path,
  `"<file>:<entry>.<field>"`, where `<entry>` is a specific `name`/`id` or `*`
  for **every entry in that file**; for `config.toml` (not a list of entries)
  it's just the dotted key. The whole list lives under `[config_sync]` — it is
  **not** a field on the entries it protects (you never edit a `[[credential]]`
  or `[[host]]` table):

  ```toml
  [config_sync]
  repo = "https://git.corp.example/ops/console-config.git"
  tag  = "v42"
  preserve = [
    "runspec_credentials.toml:*.username",   # every credential's username stays local
    "runspec_hosts.toml:*.room_user",        # every host's room_user (also a built-in default)
    "config.toml:llm.api_key_command",       # a single machine-local config.toml key
  ]
  ```

  A preserve only keeps a value that is **already in the local file** — it
  restores the operator's current value, it can't resurrect one an earlier pull
  already cleared. So set the preserve *before* the pull that would overwrite
  the value (and make sure the value is present locally first).
- Every fetched file is validated before writing, each local file is backed
  up first, and the result hot-reloads with no restart.

Mechanics worth knowing: git is pure-Python (dulwich — no system `git`, and
it ignores `~/.gitconfig`); SSH auth uses the bundled client with a
Credentials-tab SSH key; HTTPS verifies against a configurable `ca_bundle`
(for private-CA / TLS-proxy networks) and routes through the `[ssh] proxy`.

Auth reuses an entry from **Settings → Credentials** (HTTPS token or SSH
key), so no second secret store.

!!! tip "Shipping config *inside* a package instead"
    A white-label wrapper package can bundle a baseline config as package
    data — the same files, applied on startup with no git host reachable at
    all, and Config Sync layering on top. See the
    [white-label walkthrough](white-label.md).

---

## Rooms & helpdesk

A **room** is a lightweight chat server —
[`runspec-room`](https://pypi.org/project/runspec-room/) on PyPI — that runs
on one of your fleet hosts and gives the console two shared channels:

- **team chat rooms** the console joins over the host's existing SSH
  connection (no new ports exposed; an exec-channel relay covers networks
  that block port-forwarding), where room messages can also fire console
  triggers;
- **helpdesk queues**: private per-visitor conversations, where visitors sign
  in to a browser app with a verified company email and operators staff the
  queue from their consoles — an inbox with claim/resolve controls, and
  agent turns that only spend model budget on claimed threads.

A helpdesk queue can publish a **self-service catalogue**: a curated set of
runnables (and, for operators, runbooks) rendered as web forms in the room,
generated from the same `runspec.toml` schemas as the Forms tab — date
pickers, collapsed defaults and all. A visitor submits a request; an
operator's console enacts it under the usual autonomy gates. The room itself
never executes anything.

Console-side setup is on the host entry (**Settings → Jump Hosts**): the
room/queue fields on the host that runs the server, and the visible catalogue
under **Settings → Self-Service**. When several hosts run the same queue for
redundancy, the console connects to exactly one at a time — the **Connected
room** selector in Settings → Self-Service switches live.

Server-side, `runspec-room` is a small self-contained deployment on the host
(typically a systemd user service): it serves the browser app over TLS,
stores transcripts in SQLite, and reads its settings from a
`console_room.toml` (ports, public URL, queue mailbox, session lifetime,
allowed email domains, operator allowlist). Sign-in links are emailed to
visitors **by a connected console as the queue mailbox** — the room never
sends mail itself. The full server setup lives in the
[runspec-room README](https://pypi.org/project/runspec-room/); this page's
scope is the console side.
