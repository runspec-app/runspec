# Design: Improve — evaluation + problem-report system

Status: implemented in runspec-console 0.235.0.
Package: `runspec-console` (`packages/python/runspec-console/`).

## 1. Motivation

The console's agent discovers and runs fleet runnables (tool search, arg
discovery, autonomy-gated execution), but there was **no way to measure whether
the agent, the runnables, or the app are effective**. Every signal — a search that
found nothing, an arg the agent kept getting wrong, a turn that had to be stopped
with "continue", a runnable that always errors, a tool call the operator denied —
was buried in transcripts and audit logs and never looked at again. And when a
user noticed the agent misbehaving, there was no structured way to report it.

**Improve** is one engine with two entry points: an **evaluation sweep** (manual
or scheduled) and a **"report a problem"** action (a free-text comment analysed
alongside the data). Both produce one developer-facing report.

## 2. Data sources (all local, already on disk)

- **Conversations** — `conversations_dir()/*.json` via `conversations.load` /
  `list_meta`. Per-turn `{user, assistant, tools:[{name, input, output,
  operator_decision, proposed_input}], usage, model}`. The authoritative record of
  tool-search funnels, arg values, validation bounces (in tool `output`), the
  stop/recovery/error markers (inline in `assistant`), and denied/edited decisions.
- **run_summary audit** — via `bridge_reporting.get_history_all` (lean records:
  `runnable`, `exitCode`, `ts`, `host`, `exception`, …).

OTel (`observability.py`) is **not** a source — it exports to an external backend
with no local sink.

**Key constraint:** the console records validation bounces, the stop marker, and
self-recovery only as **prose** (there is no structured failure counter), so the
core detects them by matching the stable console strings:

- tool `output` starting with `Error:` / `✗` → a bounce/refusal (classified by
  the preflight text: unknown-arg / missing-required / bad-choice / out-of-range).
- `assistant` containing `paused after N tool steps` → a stop (chat
  `bridge_invoke.py`, trigger `bridge_triggers.py`).
- `assistant` containing `↻ Trying to recover` / `⚠ Error:` → self-recovery /
  durable error.

## 3. Architecture

Four layers, smallest blast radius first:

1. **Pure core `improve.py`** (stdlib only, no bridge/model dep). `build_report(
   conversations, history, *, comment, since_iso, window_days, max_incidents,
   product_name, developer_emails)` → a plain dict: metrics, derived scores,
   per-runnable rows, prioritised+capped **flagged incidents**, bounce-kind and
   autonomy tallies. Plus `build_synthesis_prompt`, `parse_synthesis`,
   `build_learnings_snippet`, `render_markdown`, `mailto_url`. Pure ⇒ unit-tested
   without disk/model (`test_improve.py`), mirroring `memory.py`/`*-core`.

2. **Bridge mixin `bridge_improve.py`** (`ImproveMixin`, added to the
   `Bridge(*Mixins)` composition). Does the I/O the core avoids: gather inputs
   (`_gather_improve_conversations` + `get_history_all`), persist a dated report to
   the machine-local store `config.improve_reports_dir()` (+ a sidecar `.md`), run
   the optional synthesis via `self._get_adapter()`, and build/open the mailto.

3. **LLM-judge synthesis.** `synthesize_improve_report(id)` feeds the deterministic
   findings + worst incidents (+ the problem comment) to the console's own adapter
   and parses ranked `{category, target, why, fix, confidence}` recommendations.
   Best-effort: returns the report with a `synthesis_error` note when no provider
   is configured or the call fails — never raises, never blocks the deterministic
   report.

4. **Agent tool `run_improve_report`.** Read-only → `autonomous`; a **deferred**
   built-in (not hot) so it stays behind `describe_tool`/`use_tool` in search mode.
   Follows the four-part contract: `_IMPROVE_TOOL_NAMES` (`bridge_const.py`,
   unioned into `_BUILTIN_TOOL_NAMES`), `_improve_tools()`/`_run_improve_tool()`
   (`bridge_agent_tools.py`, wired into both `_custom_agent_tools` branches), and
   the autonomy + dispatch branches (`bridge_autonomy.py`).

## 4. Durability — why not local memory or a git write-side

An earlier design considered writing learnings to agent memory (rejected: local,
so it wouldn't reach other installs) and giving the console a **git write-side** to
push results back to the Config Sync repo (rejected: Config Sync is deliberately
**pull-only**, and giving the single-user, trust-critical console write access to
the shared source of truth is a trust risk).

Instead: the **report is the deliverable**, addressed to the configured
`developer_emails`, handed off via a `mailto:` (`improve_mailto`, opens the
operator's default client — no Outlook/mail-server dependency). The developer, or
their coding agent, folds any durable learning into the Config Sync repo / config
seed **on their own terms** — the report ships a ready-to-commit `[[learning]]`
snippet to make that easy. Reports are machine-local and never synced.

`[improve] developer_emails` + `product_name` live in `config.toml`, so they ride
Config Sync / the config seed automatically (a white-label deploy bundles who its
developers are) and feed both the synthesis prompt and the rendered report.

## 5. UI

A dedicated **Improve** top-level tab (`console-ui/src/views/ImproveView.tsx`,
wired in `App.tsx`; typed methods on `BridgeApi` in `bridge/index.ts`): a
"report a problem" comment box + a "run evaluation" control on one screen, then
metric cards, a flagged-incident table, a **Synthesize** button, the portable
learnings snippet (copyable), and **Email developers**.

## 6. Scheduling

No schedule-engine change: an ordinary `action = "prompt"` schedule ("run the
Improve report for the last 7 days") calls `run_improve_report` on a headless
agent turn. The agent can also run it in normal chat.

## 7. Files

- New: `runspec_console/improve.py`, `runspec_console/bridge_improve.py`,
  `tests/test_improve.py`, `tests/test_improve_tool.py`,
  `console-ui/src/views/ImproveView.tsx`, `docs/console-improve.md`.
- Changed: `bridge.py` (mixin), `bridge_const.py`, `bridge_agent_tools.py`,
  `bridge_autonomy.py`, `config.py` (`read_improve` + `improve_reports_dir`),
  `tests/test_bridge_api_surface.py` (+6 methods) and the three custom-agent-tool
  stub tests (bind `_improve_tools`), `console-ui/src/App.tsx`,
  `console-ui/src/bridge/index.ts` + `mock.ts`, `mkdocs.yml`,
  `scripts/check_docs.py` (exempt `[improve]`), `CHANGELOG.md`, `pyproject.toml`.

## 8. Non-goals / future

- No console-side git push (Config Sync stays pull-only).
- Folding accepted synced learnings into the agent system prompt (like pinned
  memory) is a deliberate future step — it changes agent behaviour fleet-wide and
  deserves its own review.
- A first-class `action = "evaluation"` schedule (vs. the `prompt` reuse) if the
  prompt phrasing proves unreliable.
