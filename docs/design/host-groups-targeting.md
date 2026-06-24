# Host groups as a first-class targeting concept

## Why

In runspec-console a host's `group` (in `runspec_hosts.toml`) was only a **sidebar
label** — hosts with the same `group` rendered under one header. Nothing in
execution knew about it: the agent's `run_runnable`, triggers (`host__runnable`),
schedules (`host`), and skills (free-text) all targeted exactly **one** host. To
run the same runnable across a fleet you cloned the trigger/schedule per host.

This change makes the group name a real targeting unit: "run this runnable on
**every host in group X**" from the agent, skills, triggers, and schedules — and
makes the agent aware that groups exist. It deliberately reuses the existing
fan-out and grouped-confirm machinery rather than adding a workflow engine
(see `console-triggers.md` §1).

## The group registry (`runspec_groups.toml`)

Group **names** are a controlled vocabulary, not free-form, because they are
referenced by shared runbooks/skills and synced across consoles — a typo or a
divergent name ("Prod" vs "Production") would make the same runbook behave
differently per operator.

`groups.py` reads/writes `runspec_groups.toml`, an array-of-tables registry:

```toml
[[group]]
name        = "Production"
description = "Prod fleet"   # optional
```

It is in Config Sync `KNOWN_FILES` (`gitsync.py`) and `COLLECTION_FILES`
(`configmerge.py`, merged by `name`), so the vocabulary is shared like
hosts/skills. The registry pins **names only** — host→group **membership** stays
on the host entry's `group` field (in the already-synced `runspec_hosts.toml`).

Bridge CRUD (`get_groups` / `save_groups` / `rename_group`):
- `get_groups` is **self-healing** — it absorbs any group a host currently
  references but that isn't registered yet (and persists it), so pre-registry
  free-form groups migrate automatically and the dropdown always offers every
  in-use group.
- `save_groups` writes the registry and **reconciles membership**: a host whose
  group was deleted is reset to Ungrouped, so membership never dangles.
- `rename_group` renames in the registry **and** re-points every member host
  (a plain delete+add would orphan membership).

The host editor (Settings → Jump Hosts) presents a **required group dropdown**
sourced from the registry, with an explicit "Ungrouped" option — you pick from
the shared list rather than typing a name. A "Manage groups" dialog adds/renames/
deletes registry entries.

## The resolver (single source of truth)

`hosts.py`:
- `group_members(hosts, group) -> [host names]` — exact, **case-insensitive**,
  config order. Empty/blank group → `[]` (the ungrouped "Hosts" section is not a
  targetable group).
- `groups_map(hosts) -> {group: [host names]}` — preserves the operator's declared
  casing for display.

`bridge.py` wrappers over the live host set: `_all_host_entries()` (local + remotes,
the one definition shared by `get_hosts` and the resolvers), `_group_members`,
`_groups_map`, and `_host_has_runnable(host, name)`.

## Fan-out + grouped confirm

A group run is a **flat fan-out of one runnable behind one confirm**:

- `_grouped_confirm_run_sync` / `_grouped_confirm_run_async` take a list of
  `(tool_name, input)` calls, compute the **most-restrictive autonomy** across
  them (`_group_calls_level`; a `manual` member refuses the whole group), raise a
  single `run_group` confirm (operator may edit calls before approving), then run
  the authorised calls via `_run_calls_parallel` (bounded `ThreadPoolExecutor`,
  ≤8 workers — each host reuses its own pooled SSH transport, so the cap just
  bounds concurrent channels, not handshakes). The async variant registers the
  confirm on the event-loop thread (dispatch from a worker thread is unreliable)
  and offloads only the wait + runs.
- `_run_rule_group` (trigger parallel actions) is now a thin wrapper that sets
  rule provenance (`agent=False`) around `_grouped_confirm_run_sync`. The agent
  path reuses the same helpers with the turn's own provenance (`agent=True`).
- **Denylist** is enforced per host automatically — each call shells out to that
  host's own `runspec` binary, which reads its venv denylist on every run; a
  denied host fails as that host's result row.

Per-host outputs are joined by `_format_group_output` with a `── <host> ──`
header per section.

## Where it plugs in

**Agent** — `run_runnable`'s `host` may be a group name. `_group_fanout_calls`
detects this (and returns `None` when `host` is a real host — **host name wins on
collision**), expands to `(host__name, args)` per member that carries the runnable
(heterogeneous groups: members lacking it are skipped), and the gates
(`_gated_run_tool` / `_gated_run_tool_async`) route it through the grouped-confirm
helpers before the normal single-host decision. The agent is made group-aware by a
bounded "Host groups" block in `_console_context()` (group → connected/total +
member names, ≤8 groups/≤8 names).

**Triggers** — `triggers.parse_target(run)` classifies `group:<group>__<runnable>`;
`bridge.expand_targets(run)` resolves it to concrete `host__runnable` names. The
runner (`trigger_runner.make_handler`) expands a single `run` (→ `run_rule_group`)
and each `actions[].run` (flattened, then run sequentially or as one grouped
confirm). Targeting lives **inside the existing string fields**, so the synced
`runspec_triggers.toml` gains no new identity/preserve surface for Config Sync.

**Schedules** — `ScheduleJob` gains a `group` field, mutually exclusive with `host`
(`parse_schedule_entry` enforces exactly one). `_invoke_scheduled(..., group=...)`
fans out across members (one autonomous run card per host — scheduling *is* the
authorization, so no confirm). `runspec_schedules.toml` is **not** in Config Sync,
so the new field has no sync concern.

**Skills** — no structural change. A runbook step naming a group (e.g. "run
restart-service on the Production group") works once the agent is group-aware,
because the agent passes the group name as `run_runnable`'s `host`.

## Sidebar (UI)

`App.tsx` group headers are collapsible (chevron, persisted in `localStorage` key
`runspec.sidebar.collapsedGroups`). Collapsed, a header shows a green connected
count (`#52c41a`) and a grey disconnected count (`#595959`, matching the existing
host-row dot). Counts are live (they read `host.connected`, already re-rendered on
`runspec:hosts_updated`). The Schedules editor adds a Host/Group toggle; the
Triggers editor `run` inputs document the `group:` form.

## Bounds / risks

- **Confirm UX for large groups** — one grouped confirm lists members; the card
  view caps the displayed list.
- **Partial failure** — each host is its own result row; the batch never swallows
  a failure.
- **Matrix explosion** — single-`run` group fan-out is uncapped (that's the
  feature, bounded by concurrency); the declarative `actions` list keeps its
  10-step cap and the post-expansion total is bounded by `_MAX_FANOUT_CALLS`.
- **Name collision** — a host named the same as a group: the host wins (documented
  in the tool/field help).
