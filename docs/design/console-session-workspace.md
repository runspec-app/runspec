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
- **Trigger / schedule turn** — `trigger-{chat_id}` for a reactive trigger and
  `schedule-{chat_id}` for a schedule-sourced turn (`_trigger_run_agent_turn`'s
  `workspace_prefix`, passed as `"schedule"` by `bridge_schedules`), where
  `chat_id` is the per-turn uuid the isolated-agent loop already uses.
  Deliberately prefixed so the orphan sweep can tell headless from chat, and the
  two headless kinds apart.

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
  shape (log format, error banner) so trivial cases don't need a follow-up
  call.

### 3.1 JSON legibility (0.172.0)

Large tool output is very often **minified JSON** (a runnable with `output =
"json"`, an Outlook COM dump, an MCP result). For those, a raw 4 KB byte prefix
is a mangled, near-useless fragment of one giant line. So when the spilled
output parses as JSON (`workspaces._parse_json_bounded`, bounded at 25 M chars so
a pathological dump can't wedge the spill), the envelope carries a **structural
shape summary** instead of the byte slice:

```
[Output spilled to workspace — 4.2 MB written to tool-results/local__list-processes-abc123.json]
JSON array — shape: array[8000] of {pid: int, name: str, cpu: float, rss: int, user: str}
[A line-per-record view of the 8000 array items was written to tool-results/local__list-processes-abc123.jsonl — use workspace_grep / workspace_read / workspace_head on it to filter or page records.]
[… 4400001 bytes total; use workspace_read / … on 'tool-results/local__list-processes-abc123.json' to see the raw output.]
```

- `_json_shape` renders a compact, depth-bounded (`_JSON_SHAPE_MAX_DEPTH = 2`)
  descriptor: an object as `{key: type, …}` (capped at 40 keys), an array as
  `array[len] of <element-shape>` where an array of records shows the **union**
  of the sampled records' fields — the field set the model needs to know which
  columns it can grep / query.
- **JSONL companion.** For a top-level JSON *array*, a line-per-record
  `{stem}.jsonl` is written alongside the raw file, so the **autonomous** line
  tools (`workspace_grep`/`read`/`head`/`tail`) can filter or page records
  without reaching for a confirm-gated `run_python`. The raw `.json` stays the
  verbatim record of what the runnable emitted; the `.jsonl` is a derived view.
  Writing it is best-effort — a failure just omits the companion, the raw file
  is untouched.

Non-JSON output (logs, plain text) is unchanged: it keeps the raw head-slice
preview and gets no companion.

The 1 MB `_AGENT_TOOL_OUTPUT_CAP` stays as a hard belt-and-braces on the
envelope, but it's no longer the primary shaping mechanism. A spill failure
(disk full, jailed path resolution failed) logs and falls through to the old
cap-and-truncate behaviour, so a hiccup can never fail a tool call.

## 4. The workspace agent tools

All autonomous (registered in `_WORKSPACE_TOOL_NAMES`; hit the "read-only or
scoped-to-my-own-sandbox" branch of `_effective_tool_autonomy`), all
path-jailed under the current session's workspace root.

| Tool               | Shape                                             |
|--------------------|---------------------------------------------------|
| `workspace_info`   | Report this session's id + the **absolute** workspace root and the console documents dir — so the agent can tell the operator where a file is, and knows the roots `open_file` / `reveal_in_folder` take paths relative to (previously it had to `ls` and be told the path) |
| `workspace_ls`     | List entries (name/type/size/mtime) under a path; `sort="modified"` puts the newest file first (grab the latest spill). The response also carries the absolute `root` |
| `workspace_read`   | Read `limit` lines starting at `offset` — streams the whole file so a window deep in a multi-MB spill is reachable; accurate `total_lines` (+ `total_lines_exact`) |
| `workspace_grep`   | Search, `{path, line, text}` + `context`; regex by default or `fixed` literal, `ignore_case`, `glob` file filter, `truncated` signal at the cap |
| `workspace_head`   | First N lines                                     |
| `workspace_tail`   | Last N lines (seeks near EOF for huge files)      |
| `workspace_write`  | Write agent-authored notes (refuses `tool-results/`) |

`workspace_write` deliberately refuses to touch `tool-results/` — that dir
holds the audit-quality record of what a runnable actually emitted, and the
model shouldn't be able to overwrite it. Notes go under `notes/` (or any
non-reserved path).

Caps mirror the repo tools (`repos.py`):

- Reads return at most 512 KB of `content` / 2000 lines per call, and the
  per-call line scan is bounded at 64 MB (beyond which `total_lines` is a lower
  bound flagged by `total_lines_exact = false`).
- Grep capped at 200 hits, per-file byte budget 5 MB.
- Head/tail capped at 500 lines.

`tool-results/` is system-owned: `workspace_write` refuses it — checked against
the **resolved** target's first segment, case-insensitively, so a `..` re-entry
(`notes/../tool-results/x`) or a case variant (`Tool-Results/x`) can't slip past.
Spilled files are written atomically (temp + `os.replace`) so an interrupted
write never leaves a truncated provenance record.

Path + line grammar (uniform across all six tools): `jail` treats a backslash as
a separator (normalised to `/`), so a Windows-minded `notes\summary.md` means the
same nested path in `read`/`grep`/`ls` as in `write` — a leading separator or
drive letter is still refused as absolute. Line-oriented tools split on `\n`
only (normalising CRLF), not `str.splitlines()`'s wider set (`\f`, `\x85`, …), so
line counts and grep line numbers agree with newline-based tools even when a
value contains an exotic control char.

## 4.1 Opening / revealing a workspace file on the desktop

Two **confirm-gated action tools** (in `_ACTION_TOOL_NAMES`, so allowlistable per
trigger and listed by `list_action_tools`) let the agent surface a workspace file
to the operator on the console machine. Both take a `path` **relative** to a
`location` root — `workspace` (this session's sandbox, default) or `documents`
(where `create_document` writes locally) — resolved through the same
`workspaces.jail` used by the read tools (`_resolve_jailed_path`), so neither can
touch anything outside those two console-owned roots.

| Tool               | Effect                                            |
|--------------------|---------------------------------------------------|
| `open_file`        | Open the file in its OS default app (`_launch_open`). **Restricted to a passive-viewer extension allowlist** (`_OPENABLE_EXTS`: pdf/txt/md/csv/log/json + common images) so "open" can't become code execution |
| `reveal_in_folder` | Show the file/folder in the OS file manager, highlighted where supported (Explorer `/select`, Finder `-R`; `_launch_reveal`). **Any file type** — it only reveals, never opens/executes |

The split is deliberate and is the security model. "Open by default app"
delegates to the OS shell, where the **extension** picks the handler — and many
extensions execute (`.exe/.bat/.ps1/.js/.lnk/.hta/.reg`, Office macro formats,
`.html`/`.svg` that run script in a browser). Since the console's agent routinely
acts on **untrusted external input** (emails, room/PR messages, saved
attachments), a generic "open any file" tool would be an arbitrary-code-execution
path gated only by a confirm the operator might rubber-stamp. So `open_file` is
held to passive viewers; for anything else the agent uses `reveal_in_folder` and
the **human** makes the final open/execute decision with full context. The
confirm gate is a speed bump, not the boundary — the allowlist + jail are.

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

## 10. Sandboxed code execution in the workspace (`run_python_sandboxed` / `run_sql_sandboxed`, 0.166.0; WASM sandbox 0.224.0)

Two agent tools let the agent *process* what the workspace holds — read a spilled
`tool-results/…` file, reshape or join data, query a SQLite file — instead of
doing heavy data-wrangling token-by-token. Execution happens inside a **WASM/WASI
capability sandbox** (`sandbox.py`); the pure classification/normalisation helpers
live in `codeexec.py` (no runtime, so the autonomy gate can import them without
`wasmtime`). The dispatcher is `bridge_agent_tools._run_exec_tool`, the schemas
are `_exec_tools()`, and the killable subprocess is `_sandbox_worker.py`.

- **`run_python_sandboxed`** runs an inline snippet (`code`, a string *or* a list
  of lines) or a workspace-relative `.py` file (`file`) inside a WASI CPython
  interpreter under `wasmtime`. The workspace is preopened as the guest `/ws` and
  set as the cwd, so relative `open("tool-results/x.json")` still works.
  Interpreter output (`stdout`/`stderr`/`exit_code`/`timed_out`) is returned, and
  a large result spills to `tool-results/` like any big tool output.
- **`run_sql_sandboxed`** runs one SQL statement against a workspace-relative
  SQLite file *in the same sandbox* (the WASI CPython build embeds `sqlite3`).
  Read-only statements open the database `mode=ro` in-guest.

**Sandbox posture (0.224.0).** The sandbox is a **real security boundary**, not a
relabel. Enforced by the runtime, not by policy the code could bypass:

- **No network** — WASI preview1 exposes no sockets.
- **No host filesystem** beyond the single preopened workspace — `open("/etc/passwd")`
  fails with *no such file*; the guest physically cannot see anything else
  (stronger than the old path-jail, which only bounded the file *argument*).
- **No subprocess / host process access; no host environment or secrets.**
- **Bounded** — a memory cap (`Store` limits) and a **hard wall-clock timeout**
  (`[workspace] exec_timeout`, default 30s). The run happens in a *killable worker
  subprocess*, so even a guest blocked in a host syscall (`time.sleep`, a blocking
  read) is stopped at the deadline, and the operator's Cancel works mid-run — a
  guarantee in-process epoch interruption alone can't give.

The confirm gate remains **on top** as operator governance: `run_python_sandboxed`
is always `confirm`, exactly like `create_document` / `repo_write` / `outlook_send`
— it is no longer the *only* control. `run_sql_sandboxed` classifies the statement
(`codeexec.classify_sql`): a read (`SELECT`/`EXPLAIN`/read `PRAGMA`/`VALUES`/`WITH …
SELECT`) runs **autonomously** and can't mutate (enforced by the `mode=ro`
connection *and* the sandbox); anything else confirm-gates. The Python stdlib is
available inside the sandbox; the console's own installed packages are **not** (it
is isolated) — enough for json/csv/re/statistics/sqlite reshaping, which is what
these tools are for.

**Runtime & distribution.** The heavy pieces are optional: the `[sandbox]` extra
pulls `wasmtime`, and the ~20 MB WASI CPython `.wasm` is resolved from
`[sandbox] wasm_path` / `$RUNSPEC_SANDBOX_WASM` / an app-data cache (downloaded on
first use, SHA-256 verified; pre-placed for air-gapped installs). The compiled
module is serialized to disk (`.cwasm`), so a worker subprocess deserializes it in
~0.04 s (warm calls ~0.1–0.6 s). **Fail-closed:** if the runtime or binary can't be
made ready, `_exec_tools()` returns `[]` and the tools never reach the model —
there is **never** a fallback to un-sandboxed host execution. `[workspace]
code_exec = false` (or `[sandbox] enabled = false`) drops them on top. See
`docs/console-sandbox.md`.
