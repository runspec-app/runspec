# Console tool visibility — hiding runnables from the agent

## Problem

`runspec-console` discovers every runnable installed in the fleet's venvs and
turns each into an agent tool. Some of those runnables are unusable in a given
environment — the classic case is the Microsoft 365 / Graph runnables when
Microsoft Graph is blocked by tenant policy. They add dead weight to the agent: every
schema is spent context, and the agent will try tools that can only fail.

The existing **denylist** does *not* solve this. The denylist is a run-time
safety gate enforced inside the core `parse()` — a denylisted runnable is still
discovered, still schematized, and still shown to the model; it only throws
`RunnableDenied` when actually invoked. So it never reduces the agent's tool
footprint. `discoverable = false` doesn't help either: it is filtered by
`runspec serve`, but not by `runspec local --format json`, which is the path the
console uses to build its cache.

## Design

A console-side **curation** filter — distinct from the denylist — that removes
runnables from the surfaces where they're noise, without breaking their ability
to run directly.

Config lives in `config.toml` (so it rides Config Sync and the config seed with
no extra wiring — `config.toml` deep-overlays):

```toml
[tool_visibility]
hidden_from_agent = ["outlook-*", "teams-*"]
hidden_from_all   = ["calendar-*", "onedrive-*", "graph-*", "next-meeting"]
```

Two independent glob lists, so scope is chosen per runnable, not globally. Each
pattern is a shell glob matched against the runnable **name**; a `group:<glob>` /
`host:<glob>` prefix hides a whole group/host. Empty/missing ⇒ hide nothing.

Semantics:

- **Agent surfaces** (chat/trigger/schedule toolset, the `tool_mode = "search"`
  search rows / catalog index / run-by-id path) filter by the **union** of both
  lists.
- **Forms tab + room/self-service catalog** filter by **`hidden_from_all` only**.

Hidden ≠ denylisted: a hidden runnable stays installed and directly runnable
(Forms, `runspec run`). Curation, not a safety boundary.

## Implementation

- `config.read_tool_visibility()` — reads/cleans the two lists.
- `bridge_utils.runnable_hidden(name, group, host, patterns)` — the pure
  `fnmatch` matcher (mirrors the core denylist's `_matches_any`, but empty ⇒ hide
  nothing). A TS mirror lives in `console-ui/src/toolVisibility.ts`.
- `Bridge._visible_runnables(surface)` — the single chokepoint. `surface="agent"`
  drops the union; `surface="forms"` drops `hidden_from_all` only. It replaces the
  five `list(self._runnables_cache)` reads that feed the agent (`bridge_agent_tools`
  `_runnables_to_tools`; `bridge_search` `_runnables_for_id` / `_search_runnables`
  / `_build_catalog_index`) and the catalog publish (`bridge_triggers._publish_catalog`).
- `Bridge.get_tool_visibility()` / `save_tool_visibility()` — the Settings tab
  read/validate/write, added to the frozen `test_bridge_api_surface` set.
- Frontend: a **Settings → Agent tools** tab (per-runnable Visible / Hide from
  agent / Hide everywhere, plus advanced glob lists); the Forms view filters
  `hidden_from_all` client-side and refreshes on `runspec:tool_visibility_changed`.

## Config seed

Because the section lives in `config.toml`, a white-label / corporate config seed
can ship a baseline `[tool_visibility]` to hide capabilities a whole fleet can't
use — e.g. `hidden_from_all = ["outlook-*", "teams-*", "calendar-*", "onedrive-*",
"graph-*", "next-meeting"]` on a deployment with no Graph access — from one
versioned source, with local edits and Config-Sync layers overriding as usual.
