# Console "AI off" switch — a non-AI edition of runspec-console

**Status:** deferred / idea capture. Not scheduled. Written down so a future
session can pick it up after more thought. See the companion note
`console-mcp-server.md` (routing an external coding agent *through* the console)
— that feature is being pursued first and is independent of this one.

---

## Goal

Ship runspec-console with the AI/agent features hideable and disableable, so the
**same binary** can run as a non-AI, GUI-only fleet tool. Two motives:

1. **Onboarding.** Give Claude Code / runspec users a non-AI way to get
   comfortable with the runspec concept (hosts, credentials, forms, fleet
   runnables, schedules) before adopting the full agentic console.
2. **Corporate robustness pitch.** Let a security/platform team vet the
   fleet-execution substrate (SSH transport, credentials/keychain, denylist,
   `enforce_run_as`, config-sync/seed) with **zero AI in the loop**, then enable
   the agent later as a seeded flag flip on an architecture they already ran.

The value proposition when AI is off is coherent on its own: **a GUI for
self-serve fleet runnables across local + remote venvs, with Forms, History,
schedules, and rule-mode triggers.**

---

## The three gating layers (apply cheapest-first, stop at any)

**Layer A — UI gate (hide the tabs).**
Hide: ConsoleView/chat, Improve, Telemetry, agent memory, Rooms, the MCP-*client*
tab; strip agent-mode options from the Schedules/Triggers editors. Keep: Forms,
Hosts sidebar, History, Runnables, Specs, Logs, Transfer, Settings
(hosts/credentials/config-sync/seed/local-venvs/denylist/sounds/theme), and
rule-mode schedules/triggers. This alone is the onboarding product.

**Layer B — runtime gate (don't bootstrap it).**
No adapter/LLM path can bootstrap; agent tools aren't registered; the agent loop
is unreachable. Triggers/schedules execute only **runnable/rule** actions —
agent-mode `prompt`/`runbook`/`pre_turn`/action-tool steps are *refused*, not
merely hidden. Telemetry traces agent turns, so with no turns it is inert anyway.

**Layer C — dependency gate (don't install it).**
Move the LLM adapters + agent-only modules behind an `[ai]` extra so a plain
`pip install runspec-console` does not ship the anthropic/bedrock/langserve SDKs
(nor sandbox/telemetry extras). The only expensive layer; treat as **optional
hardening**, not part of a first cut.

---

## Two-tier enforcement (maps onto existing controls)

- **Soft off** = `[console] ai = false` in `config.toml`, distributed via
  config-seed and pinned with a `preserve` entry. Central default; a determined
  operator could flip it locally.
- **Hard off** = don't install the `[ai]` extra (Layer C). The code isn't
  present, so it can't be turned on. Naturally locked.

---

## Strategic lever: AI-off is a config-seed setting

Because the switch lives in `config.toml`, it rides the existing config-seed /
config-sync machinery. Rollout becomes:

1. Corporate ships a seed → every dev's console comes up **AI-off, forms + fleet
   only**. Nothing to configure; nothing for security to object to.
2. Devs get comfortable with the whole fleet substrate, no AI.
3. AI app is approved → ship a seed with `ai = true` (+ the `[ai]` extra) → the
   *same binary already in production* lights up the agent.

Robustness argument made concrete: not "trust us the AI version is safe" but
"you already ran this architecture; we flipped one seeded flag."

Optionally pair AI-off with a default white-label `[console.theme] brand_name`
(e.g. "runspec Fleet") so it presents as its own tool rather than "the AI console
with things hidden." Reuses machinery already present.

---

## Open questions / things to check before committing

- **Bring-up eagerness (the load-bearing feasibility check).** Is the
  adapter/LLM constructed lazily (first chat turn) or eagerly (in
  `Bridge.__init__` / `app.py`, `[llm]` assumed present)? Lazy ⇒ Layers A+B are
  a UI gate plus guarding the trigger/schedule agent paths (cheap). Eager ⇒ a
  bring-up refactor threaded through the bridge mixins.
- **Lock vs default.** Is AI-off a governance lock (must not be toggled on) or
  just a shipped default? Soft/hard tiers above answer this, but the product
  stance needs deciding.
- **Dependency-gating cost.** Is the anthropic SDK a hard dep today, or already
  lazy/extra? Moving it to `[ai]` is the bulk of Layer C.
- **Naming/positioning.** Does the AI-off console present as "runspec-console"
  or as its own brand? (White-label `brand_name`.)
- **Interaction with the MCP-server feature.** Routing an external agent through
  the console (see `console-mcp-server.md`) is independent — but an AI-off
  console that *gates an external coding agent's* fleet calls is a strong
  non-AI-inside demo, and worth keeping coherent with this switch.

---

## Explicitly out of scope for this note

- A browser-accessible / headless / always-on web daemon. The operator confirmed
  they want a GUI, not always-on; the desktop console is the vehicle. (A web
  front-end over a shared fleet-core remains a possible *later* layer if
  browser/headless is ever required — not this.)
- The MCP-server-in-console feature — captured separately in
  `console-mcp-server.md` and pursued first.
