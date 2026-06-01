# Handoff — runspec-console autonomy confirm prompt (0.1.14)

_Last updated: 2026-06-01 (late). Written as a session handoff so a fresh
session can pick up with full context._

## TL;DR / where we are

- **runspec-console 0.1.14 is merged to `main` (PR #77) and released to PyPI**
  via the Console Release workflow (workflow_dispatch — see "Environment
  facts" for why not a tag push). The Console Release run completed green.
- **NOT YET VERIFIED in the real app.** The fix is based on strong evidence
  (logs + reasoning), passes tests, typechecks and builds — but the user went
  to bed before visually confirming the approval card actually renders in the
  pywebview/WebView2 GUI. **First task next session: confirm the behaviour.**

## The problem we were fixing

Asking the LLM (chat) to run a runnable whose `autonomy` is `confirm` or
`supervised` (e.g. `flush-dns`) showed a spinner for ~5 minutes and then
reported "you declined". The Approve/Deny prompt never appeared.

## Root cause (confirmed via logs)

Logging added to the gate showed:

```
autonomy gate: tool=local__flush-dns decision=confirm
autonomy gate: prompting for local__flush-dns (autonomy=confirm) request_id=…
WARNING: autonomy gate: request_id=… timed out after 300s — treating as deny
```

No `evaluate_js` error → the `runspec:tool_confirm` event reached the browser,
but the frontend used antd's **`Modal.confirm`** static method, whose detached
portal does **not paint inside the WebView2 host**. Nothing answered → the 300s
timeout fired → treated as deny. Tokens stream fine over the same dispatch path,
which is what proved the event was arriving and pointed at the Modal portal.

Also learned: `tool_start` is dispatched **before** the gate waits, so the
chat block's tool segment sits at "running…" (spinner) for the whole wait —
that spinner *was* the symptom.

## What changed (all on `main` as of 0.1.14)

Frontend (`packages/console-ui/src/`):
- **`components/OutputPanel.tsx`** — removed `Modal.confirm`. The approval now
  renders **inline on the tool segment** that's spinning, via normal React
  state (`pendingConfirm` in `OutputPanel`, threaded to `BlockCard` →
  `ToolCallBlock`). The segment switches from "running…" to an amber
  **"needs approval"** state with **Approve & run** / **Deny** buttons.
  Match is exact: confirm carries chat `id` + full `tool_name`. Approve
  resolves the gate → real run; Deny = cancel-before-run.
- **`bridge/mock.ts`** — mock `send_chat` now drives the real confirm
  round-trip (`runspec:tool_confirm` → `resolve_tool_confirmation`) so the
  flow is exercisable in a plain browser / Vite dev instance.

Backend (`packages/python/runspec-console/runspec_console/`):
- **`bridge.py`** — prompt is now dispatched from the chat **event-loop
  thread** (the proven token path); only the blocking wait is offloaded to a
  worker thread (`_gated_run_tool_async`). Sync `_gated_run_tool` kept for
  unit tests. Shared helpers: `_confirm_decision` / `_register_confirm` /
  `_finish_confirm`. **Gate logging** end-to-end via
  `logging.getLogger(__name__)`. `_dispatch` no longer swallows `evaluate_js`
  failures — it logs them.
- **`tests/test_autonomy_gate.py`** — 13 cases incl. 4 async-gate
  (autonomous/manual/approve/deny). Full console suite: 68 passed.
- Version → **0.1.14** in `pyproject.toml`; CHANGELOG entry added; 0.1.13's
  entry restored to as-shipped text with a forward pointer.

## Verified vs NOT verified

- ✅ Python tests (68 passed), UI typecheck, UI production build.
- ✅ Logs proved the root cause (Modal portal, not threading).
- ❌ **Real-app GUI**: nobody has yet seen the inline "needs approval" card
  render in the running pywebview app. This is the one open confirmation.

## Next session — do this first

1. **Verify the fix in the real app** (the only thing that truly proves it):
   ```
   git pull                      # main has 0.1.14
   cd packages/console-ui && npm run build
   cd ../python/runspec-console && pip install -e .
   runspec-console
   ```
   Ask it to flush DNS → the tool block should switch to amber **"needs
   approval"** with Approve/Deny within a second (no 5-min spinner). Approve
   runs it; Deny cancels. Gate steps print to the launching terminal
   (`autonomy gate: …`).
   - If it works: done. (Optionally note it in CHANGELOG/here.)
   - If it still doesn't render: the inline card is in the normal React tree
     that already renders tokens, so a failure would be surprising — capture
     the terminal `autonomy gate` lines and the browser console (see harness
     below) before changing anything.

2. **Build the testing harness we agreed on** (the "easier way to work"):
   - **Browser-console → Python log forwarding**: a small JS shim
     (`console.log/warn/error` + `window.onerror`/`unhandledrejection`) that
     calls a new `Bridge.log_js(level, msg)` which logs via
     `logging.getLogger(__name__)`. Result: frontend `[runspec] …` logs and
     React errors land in the SAME terminal as Python logs — **no DevTools
     needed** (prod webview has DevTools disabled). Suggest forwarding only
     `[runspec]`-prefixed logs + everything at error level to avoid noise.
   - **`runspec-console --diagnose`**: after the window loads, fire a
     synthetic `tool_confirm` itself (no chat / Anthropic / network). One
     command reproduces the gate in 5s and logs the full round-trip. If the
     card appears → gate UI is fine; if not → isolated to the gate with full
     logs from both sides.
   - **`scripts/dev-install.ps1`**: build UI → `pip install -e .` into the
     active venv → print resolved version + module path, so "did it install
     to the right venv?" is never a question again.

## Deferred (explicitly out of scope of 0.1.14)

- **Cancel a *running* chat tool.** The chat tool path (`_run_tool_sync`)
  doesn't register in `_in_flight` and has no kill switch, so the green
  in-flight strip + its cancel only cover manual `/runnable` launches. In the
  gate flow, **Deny is the pre-run cancel** (the only stuck state). Real
  mid-run cancellation for chat tools is a separate, larger change.

## Environment facts (matter for tooling)

- **Tag pushes are blocked** from the Claude Code remote env (403). Releases
  go via the Console Release workflow's **`workflow_dispatch`** input (tag
  name), not `git push --tags`. Release tags: `console-v*`.
- **Windows dev friction** is real for the user: PowerShell (`&&` invalid),
  port-in-use on 5173, prod webview has **DevTools disabled** (use
  `runspec-console --dev` + Vite for DevTools, or the console-forwarding
  harness above). This friction caused two unverified releases tonight — hence
  the harness is high priority.
- **Logs**: console runnable logging is auto-wired (the app calls
  `runspec.parse(...)`). INFO → the launching terminal's stdout; the runspec
  log file is under `{project_root}/logs/runspec-console.log` (or
  `~/logs/runspec-console.log`).

## Branch hygiene note

- `claude/console-autonomy-fix` was the clean branch merged via PR #77 — safe
  to delete now.
- `claude/console-autonomy-gate` is an **old, diverged** branch (pre-squash
  history + a duplicate of the 0.1.13 gate). Do **not** build on it; delete it.
