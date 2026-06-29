# Host profiles — a scoped, switchable working set of hosts

## Why

A runspec-console operator often wears several hats: an application-support
engineer who *also* assists DevOps and occasionally works another region's
servers. Today every configured host is probed and kept connected on every
refresh cycle, all the time — so the sidebar is a wall of hosts and the console
holds standing SSH connections to machines you aren't using right now.

**Host profiles** let you flip between named host-scopes. The mental model is
"each profile is like a fresh instance of the app, differing only in which remote
hosts are in scope and connected." You are *in* one profile at a time; switching
tears down the previous scope's standing connections and brings up the new one.
Same config, credentials, runbooks, and agent — the **only** thing a profile
changes is the live host set.

## What a profile is (and isn't)

- A **profile** is a named *working set* — "which hosts are loaded right now."
  Exactly one is active at a time (plus the built-in **All hosts**).
- A **group** (existing feature) is a *targeting unit* — "which hosts do I act on
  together," one per host, used for fan-out. Profiles and groups are orthogonal:
  inside a loaded profile you still see and target your groups.
- Membership is **many-to-many**: a host can belong to several profiles (a shared
  bastion may be in both *App Support* and *DevOps*). One profile is active, but a
  host can be reachable from more than one.

Profiles are a **convenience/connection-scoping** concept, **not** a security or
reachability boundary (the denylist is the real "can't run here").

## Scope boundary — what a profile actually gates

The operator's chosen rule (deliberate): a profile scopes the **interactive
working set and standing connections only**. Automation spans profiles.

**Scoped to the active profile** (the sidebar/standing surface):
- The refresh/probe cycle — only the active profile's hosts are probed and kept
  warm. This is what removes the "connected to everything, always" cost.
- `get_hosts` (the left sidebar) and runnable **discovery** — the catalog you
  browse reflects your active working set.
- The chat agent's *visible* catalog/host-awareness (it folds discovery).

**Not scoped** (reaches any configured host on demand):
- Explicit single-host lookups (`_host_entry`) — a manual run, a schedule with
  `host = …`, an agent tool call naming a host.
- The group resolvers (`_group_members` / `_groups_map` / `_all_host_entries`) —
  group fan-out, trigger `expand_targets`, schedule `group = …`.
- Reactive triggers and scheduled jobs — they connect to their target on demand
  regardless of which profile is loaded, so a 2am DevOps schedule still fires
  while you're in the App-Support profile.

Mechanically this falls out of one split: scope the two interactive call sites
(`_refresh_cycle`, `get_hosts`/discovery) to a new `_active_host_entries()`, and
leave `_host_entry` / `_all_host_entries` full-scope. On-demand connections to
out-of-profile hosts are created by the pool as needed and idle-reaped afterward,
so they don't become standing connections.

## Storage — definitions sync, the active choice is local

This is the heart of the "I want to git-sync this" requirement, and it mirrors
the established *synced-registry vs machine-local-state* split (e.g.
`runspec_groups.toml` syncs; `runspec_repo_paths.toml` / `runspec_rota.toml` do
not).

**Profile definitions sync** (the team shares identical sets):
- Membership is a **`profiles` array on each host entry** in the already-synced
  `runspec_hosts.toml` — e.g. `profiles = ["app-support", "devops"]`. No new sync
  identity surface; it rides the existing host merge (by `name`), like the way
  trigger targeting lives inside existing string fields.
- A **`runspec_profiles.toml`** name/label registry — the controlled vocabulary —
  mirroring `runspec_groups.toml` exactly:
  ```toml
  [[profile]]
  name        = "app-support"
  description = "App-support fleet"   # optional
  ```
  Added to Config Sync `KNOWN_FILES` (gitsync) and `COLLECTION_FILES`
  (configmerge, merged by `name`).

**The active profile is machine-local and never synced** — which profile you have
loaded is personal, moment-to-moment state, like the rota or repo checkout paths:
- `runspec_active_profile.toml` — `{ active = "app-support" }` — **not** in
  `KNOWN_FILES`. Empty / absent / `active = ""` means **All hosts** (today's
  behavior).

## Switching profiles (the "reboot into a scope")

`set_active_profile(name)`:
1. Snapshot the currently-in-scope SSH hosts.
2. Persist the new active profile (machine-local file).
3. Recompute the in-scope set; for hosts **leaving** scope, evict their pooled
   transport (`SSHConnectionPool.drop`) and clear their connected-cache entry so
   the sidebar dot goes grey immediately.
4. Dispatch `runspec:hosts_updated` and kick one refresh cycle so the newly
   in-scope hosts probe + discover.

In-app and fast — no process relaunch; it just *behaves* like a fresh instance.

## Backward compatibility / opt-in

- **All hosts** is a built-in baseline = exactly today's behavior (every host in
  scope). It is the startup default and the home for untagged / newly-synced
  hosts, so a console that never adopts profiles sees no change.
- A host tagged into no custom profile appears only under **All hosts** until you
  tag it — a freshly synced host is visible there, never silently lost.
- Empty active selection → no filter, so the console can never end up blank.

## Bridge surface (mirrors groups)

Registry CRUD, self-healing like groups:
- `get_profiles()` — registry `[{name, description}]`, absorbing any in-use
  profile tag not yet registered (and persisting it).
- `save_profiles(profiles)` — replace the registry; on a delete, strip the now-
  illegal tag from every host so membership never dangles.
- `rename_profile(old, new)` — rename in the registry **and** re-point every
  host's `profiles` tag.

Active selection:
- `get_active_profile()` → `""` (All hosts) or a profile name.
- `set_active_profile(name)` → performs the switch above; returns `{active}`.

Resolvers (`hosts.py`, pure): `profile_members(hosts, profile)`,
`profiles_map(hosts)`, `host_profiles(host)`.

## UI

- **Left sidebar:** a profile switcher at the top (a `Select` of All hosts +
  registered profiles). Rows still group by `group` underneath; only the active
  set renders. Switching calls `set_active_profile`.
- **Settings → Jump Hosts:** a per-host **profiles multi-select** (like the group
  dropdown, but multiple) writing the host's `profiles` array, plus a **Manage
  profiles…** dialog cloned from `GroupsManager` (add / rename / delete /
  describe).

## Bounds / risks

- **On-demand reconnect erodes "never connected to all" slightly** — by the
  operator's choice, automation may briefly connect to out-of-profile hosts. The
  *standing* reduction still holds (the refresh loop won't keep them warm; idle
  transports are reaped).
- **Scoped discovery hides out-of-profile runnables from the chat catalog** — a
  deliberate consequence; explicitly named or configured targets still execute.
- **Name collision** with a group name is harmless — profiles and groups are
  separate namespaces resolved by separate fields.
- **Single active profile** — no concurrent multi-profile view in v1 (that would
  reintroduce "connected to everything"); the union model was explicitly rejected
  in favor of instance-like switching.
