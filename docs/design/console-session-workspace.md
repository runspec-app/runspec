# Console session workspace — per-session sandbox for large tool outputs

**Status:** Built in `runspec-console` 0.160.0. Chat + trigger + schedule turns.

## 1. Problem

`runspec-console`'s agent hands runnable output back to the model as a single
string capped at 1 MB (`_AGENT_TOOL_OUTPUT_CAP` in `bridge_const.py`). Beyond
the cap the tail is sliced with a visible truncation marker. That works, but
the design forces two expensive habits:

- **Runnable authors hand-shrink every output.** A native tool that would
  naturally emit a few thousand rows has to add `--top`, `--since`, `--format
  digest`, etc. so the JSON stays inside a tool result. Each shrink is a
  design + implementation + doc round-trip, and the shrink is done blind (you
  don't know which fields the agent will actually want next time).
- **Third-party tools we don't own can't be tamed.** A vendor CLI, an MCP
  server, an Outlook COM dump — their output shape isn't ours to change. When
  their result exceeds the cap the agent silently loses the tail.

The fix is to stop treating tool output as something that must fit in one
message. Give the agent a **workspace on disk** and a set of **file tools** to
interrogate whatever the runnable emitted.

## 2. Shape

A workspace is a directory keyed by session id:

```
{app_dir}/workspaces/{session_id}/
  tool-results/{tool_name}-{inv_id}.{json|txt}    ← system-written (spill)
  notes/                                           ← model-authored (workspace_write)
```

Session ids:

- **Chat conversation** — the persistent `_conversation_id` (uuid4 hex,
  minted lazily on the first turn, resets on `clear_chat`, same one that keys
  `conversations/{id}.json`).
- **Trigger / schedule turn** — `trigger-{chat_id}` where `chat_id` is the
  per-turn uuid the isolated-agent loop already uses. Deliberately prefixed
  so the orphan sweep can tell headless from chat.

**Nothing new to configure by default.** The workspace is created on first
spill (or first `workspace_write`) — an unused session leaves no directory
behind.

## 3. The spill rule

`_run_tool_sync` (the shared executor path for every runnable-backed tool
call, in both search-mode and dedup/per_host layouts) joins the runnable's
stdout lines and then decides:

- **Below `[workspace] spill_threshold` (default 32 KB).** Return the raw
  string as the tool result. Below this size an inline dump is cheaper than a
  disk round-trip, and small results are exactly where you want the model to
  see the full thing.
- **Above the threshold.** Write the *full* raw output to
  `tool-results/{tool_name}-{inv_id}.{json|txt}` (extension picked by
  whether the last non-empty line is valid JSON) and return a short
  envelope:

  ```
  [Output spilled to workspace — 4.2 MB written to tool-results/local__list-processes-abc123deadbe.json]
  <first 4 KB of output>
  [… 4400001 bytes total; use workspace_read / workspace_grep / workspace_head / workspace_tail on 'tool-results/local__list-processes-abc123deadbe.json' to see the rest.]
  ```

  The head chunk (`_SPILL_ENVELOPE_HEAD_BYTES`, 4 KB) gives the model the
  shape (JSON keys, log format, error banner) so trivial cases don't need a
  follow-up call.

The 1 MB `_AGENT_TOOL_OUTPUT_CAP` stays as a hard belt-and-braces on the
envelope, but it's no longer the primary shaping mechanism. A spill failure
(disk full, jailed path resolution failed) logs and falls through to the old
cap-and-truncate behaviour, so a hiccup can never fail a tool call.

## 4. The six agent tools

All autonomous (registered in `_WORKSPACE_TOOL_NAMES`; hit the "read-only or
scoped-to-my-own-sandbox" branch of `_effective_tool_autonomy`), all
path-jailed under the current session's workspace root.

| Tool               | Shape                                             |
|--------------------|---------------------------------------------------|
| `workspace_ls`     | List entries (name/type/size/mtime) under a path  |
| `workspace_read`   | Read `limit` lines starting at `offset` (paged)   |
| `workspace_grep`   | Regex search, `{path, line, text}` + `context`    |
| `workspace_head`   | First N lines                                     |
| `workspace_tail`   | Last N lines (seeks near EOF for huge files)      |
| `workspace_write`  | Write agent-authored notes (refuses `tool-results/`) |

`workspace_write` deliberately refuses to touch `tool-results/` — that dir
holds the audit-quality record of what a runnable actually emitted, and the
model shouldn't be able to overwrite it. Notes go under `notes/` (or any
non-reserved path).

Caps mirror the repo tools (`repos.py`):

- Reads capped at 512 KB / 2000 lines per call.
- Grep capped at 200 hits, per-file byte budget 5 MB.
- Head/tail capped at 500 lines.

## 5. Retention

**Chat workspaces** live and die with the conversation:

- `conversations.prune()` gained an optional `workspaces_root=` — after
  deleting `{id}.json`, it also `shutil.rmtree`'s the sibling workspace dir.
- `conversations.delete()` (called from `Bridge.delete_conversation`) does
  the same.
- `clear_chat` intentionally leaves the previous conversation on disk (that's
  today's behaviour), and the workspace lingers with it so reopening from
  History still shows spilled outputs referenced in the transcript. It's
  reaped when `[chat_history]` retention rolls the conversation over.

**Headless workspaces** (`trigger-…` / `schedule-…`) don't have an owning
conversation — they're pruned by mtime against `[workspace]
headless_retention_days` (default 7).

**Startup orphan sweep** (`Bridge._sweep_workspaces`, called from
`__init__` right after `seed_apply`) picks up any drift: chat workspaces
whose conv id no longer exists in `conversations/`, and headless workspaces
older than the retention window. Robust to a missing root and per-entry
filesystem errors.

## 6. Threading + concurrency

The workspace id for the current turn is looked up per-call, not carried on a
long-lived Bridge attribute. Trigger/schedule turns register
`self._workspace_by_chat[chat_id] = "trigger-<chat_id>"` under `self._lock`
before starting the isolated turn (`_trigger_run_agent_turn`) and pop it in
the `finally` — same pattern the `_run_origin` machinery already uses. Chat
turns don't register anything; `_run_tool_sync` and `_run_workspace_tool`
fall back to `self._conversation_id`. This makes concurrent chat + trigger
runs hermetic: each turn only ever sees its own workspace.

## 7. Prompt / system-message impact

- **No workspace id in the cached tool list.** The tool descriptions are
  written to be self-explanatory ("the workspace is your session sandbox
  where large tool outputs get spilled — start with `workspace_ls`") and
  don't name a per-conversation path — so the prompt cache stays warm.
- **One short line in `_console_context()`** points at the tools by name.
  Applies to chat, triggers, and schedules (they all fold this block into
  the per-turn `system_override`).
- **The envelope names the file path each time.** The path is right there in
  the tool result — the model doesn't need it in the system prompt to reach
  for a file it just saw.

## 8. Files touched

- `runspec_console/config.py` — `workspace_dir(session_id)`,
  `workspaces_root()`, `read_workspace()`.
- `runspec_console/bridge_const.py` — `_WORKSPACE_TOOL_NAMES`,
  `_TOOL_OUTPUT_SPILL_THRESHOLD`, `_SPILL_ENVELOPE_HEAD_BYTES`.
- `runspec_console/workspaces.py` — new pure module: `jail`,
  `spill_output`, `list_dir`, `read_file`, `grep`, `head`, `tail`,
  `write_file`, `sweep_orphans`, `delete_workspace`.
- `runspec_console/bridge_agent_tools.py` — `_workspace_tools()` schemas,
  `_run_workspace_tool` handler, spliced into `_custom_agent_tools`.
- `runspec_console/bridge_autonomy.py` — autonomy branch,
  `_run_tool_sync` dispatch, the spill logic on the shared join site.
- `runspec_console/bridge_internals.py` — one line in `_console_context()`.
- `runspec_console/bridge_triggers.py` — register / unregister
  `_workspace_by_chat[chat_id]` around `_run_isolated_agent_turn`.
- `runspec_console/bridge.py` — `_workspace_by_chat` init,
  `_sweep_workspaces()`, startup sweep.
- `runspec_console/conversations.py` — `prune()` / `delete()` accept
  `workspaces_root=` and cascade the cleanup.
- `runspec_console/bridge_invoke.py` — `_persist_conversation_turn` +
  `delete_conversation` pass the workspaces root through.

## 9. Non-goals

- **No cross-conversation workspaces.** Each session is hermetic.
  Cross-run knowledge lives in `runspec_memory.toml` (facts) and
  `runspec_runbooks.toml` (procedures).
- **No network / no shell out from the `workspace_*` tools.** The six file
  tools are pure Python file I/O over a jailed path. (Code *execution* in the
  workspace is a separate, confirm-gated tool family — see §10.)
- **No new persistence contract for the file tools.** The `workspace_*` tools
  treat the workspace as a folder; the filesystem is the truth. (`run_sql` in
  §10 reads/writes a SQLite file the agent itself created there — that's a
  data file inside the folder, not a workspace-level index.)
- **Not a workflow engine.** The agent decides what to write and read; the
  console never orchestrates "step A → B" over workspace files.
- **Not the same as `documents/` or `repos/`.** `create_document` writes
  operator-facing markdown (may upload to a fleet host); repo tools operate
  over long-lived git checkouts. Workspaces are ephemeral, per-session, and
  strictly agent-internal.

## 10. Code execution in the workspace (`run_python` / `run_sql`, 0.166.0)

Two **confirm-gated** agent tools let the agent *process* what the workspace
holds — read a spilled `tool-results/…` file, reshape or join data, query a
SQLite file — instead of doing heavy data-wrangling token-by-token. They live in
the pure `codeexec.py` module (kept out of `workspaces.py`, whose contract stays
"file I/O, no subprocess"); the dispatcher is `bridge_agent_tools._run_exec_tool`
and the schemas are `_exec_tools()`.

- **`run_python`** runs an inline snippet (fed on stdin) or a workspace-relative
  `.py` file with the console's own `sys.executable`, `cwd` set to the workspace.
  Interpreter output (`stdout`/`stderr`/`exit_code`/`timed_out`) is returned, and
  a large result spills to `tool-results/` like any big tool output.
- **`run_sql`** runs one SQL statement against a workspace-relative SQLite file
  using stdlib `sqlite3` in-process (no code execution). Read-only statements
  open the database `mode=ro`.

**Trust posture (deliberate).** The console is single-user and runs *as the
operator*. `run_python` executes arbitrary code with the operator's full
privileges — the path-jail only bounds *which file the tool is pointed at* and
the subprocess cwd; it does **not** sandbox what the code can touch. So **the
confirm gate is the security control here, not the jail** — `run_python` is
always `confirm`, exactly like `create_document` / `repo_write` / `outlook_send`.
`run_sql` classifies the statement (`codeexec.classify_sql`): a read
(`SELECT`/`EXPLAIN`/read `PRAGMA`/`VALUES`/`WITH … SELECT`) runs **autonomously**
and can't mutate (enforced by the `mode=ro` connection, not just the classifier);
anything else confirm-gates. We add a wall-clock timeout
(`[workspace] exec_timeout`, default 30s) and output caps/spill as honest,
proportionate rails, but deliberately do **not** attempt OS sandboxing / network
isolation / hard rlimits — that would be security theater on a Windows-first
desktop console whose operator already has shell-equivalent power over the whole
fleet via SSH runnables. A locked-down deployment drops both tools with
`[workspace] code_exec = false` (defense-in-depth beyond the confirm gate);
`_exec_tools()` then returns `[]` so they never reach the model.
