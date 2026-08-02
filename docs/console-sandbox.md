# Sandboxed code execution

The console's agent can run code to process what a chat session's workspace holds
— reshape a spilled `tool-results/…` file, join data, query a SQLite file —
instead of doing heavy data-wrangling token-by-token. This runs inside a
**WASM/WASI capability sandbox**, so it is safe to offer even where "an agent can
run Python" would normally be a non-starter.

This page is the one to hand a security reviewer.

## What the sandbox guarantees

Code runs inside a [WASI](https://wasi.dev/) CPython interpreter under
[`wasmtime`](https://wasmtime.dev/). A WebAssembly guest has **no ambient
authority**: it can only do what the host explicitly hands it. The console hands
it exactly one directory — the current chat session's workspace — and nothing
else. The following are enforced by the runtime, not by a policy the code could
talk its way around:

| Property | How it's enforced |
|---|---|
| **No network** | WASI preview1 exposes no sockets — `socket`/`urllib` have nothing to open |
| **No host filesystem** beyond the session workspace | Only the workspace is *preopened* (as the guest `/ws`); `open("/etc/passwd")` fails with *no such file* |
| **No subprocess / host process access** | WASI exposes no process-spawn capability |
| **No host environment or secrets** | The guest gets no host environment |
| **Bounded memory** | A `wasmtime` `Store` memory-size limit |
| **Bounded wall-clock, and a working Cancel** | The run happens in a **killable worker subprocess**, so even a guest blocked in a host syscall (`time.sleep`, a blocking read) is stopped at the deadline |

Because the workspace *is* preopened, the intended use — read a spilled file,
reshape it, write a result back — keeps working, and is now *enforced* rather than
merely conventional.

**What's not available inside the sandbox:** network, host access, and the
console's own installed (especially native) packages. The Python **standard
library is available** — including `sqlite3` — which covers
json/csv/re/statistics/sqlite-style reshaping, which is what these tools are for.

The confirm gate stays **on top** of the sandbox as operator governance
(`run_python_sandboxed` always asks first); it is no longer the *only* control.

## The tools

- **`run_python_sandboxed`** — run a Python snippet (`code`, a string *or* a list
  of lines) or a workspace `.py` file (`file`), with the workspace as the working
  directory. Always confirm-gated.
- **`run_sql_sandboxed`** — run one SQL statement against a workspace SQLite file,
  in the same sandbox. Read-only statements (`SELECT`/`EXPLAIN`/read `PRAGMA`) run
  autonomously and open the database read-only; writes/DDL confirm-gate.

## Installing & configuring

The sandbox is an optional extra:

```bash
pip install runspec-console[sandbox]
```

That pulls `wasmtime`. A ~20 MB WASI CPython binary is also needed; by default it
is fetched on first use into the app-data cache and SHA-256 verified. For an
air-gapped install, place the binary yourself and point at it:

```toml
[sandbox]
wasm_path = "/opt/runspec/python.wasm"
auto_download = false
```

Other knobs (all optional):

```toml
[sandbox]
enabled = true      # kill switch (with [workspace] code_exec); either false drops the tools
mem_mb = 512        # guest memory ceiling
# fuel = 5000000000 # optional wasmtime CPU-instruction cap (unset ⇒ wall-clock is the guard)
```

The wall-clock timeout is `[workspace] exec_timeout` (default 30s).

## Fail-closed

If the sandbox runtime or binary can't be made ready, the tools are **dropped
from the agent's toolset entirely** — the agent never sees them, and there is
**never** a fallback to un-sandboxed host execution. The `[workspace] code_exec =
false` (or `[sandbox] enabled = false`) kill switch drops them on top.

## Performance

The compiled module is cached to disk in serialized form (`.cwasm`), so a worker
subprocess deserializes it in ~0.04 s. The first run in a fresh install compiles
+ (optionally) downloads once (a few seconds); warm runs start in ~0.1–0.6 s.

## Implementation

- `runspec_console/sandbox.py` — availability, binary/serialized-module resolution, the subprocess orchestrator.
- `runspec_console/_sandbox_worker.py` — the standalone killable worker that runs the guest.
- `runspec_console/codeexec.py` — pure SQL classification + `code` normalisation (no runtime).
- `runspec_console/bridge_agent_tools.py` — the `_exec_tools()` schemas and `_run_exec_tool()` dispatcher.

See also [the design note](design/console-session-workspace.md#10-sandboxed-code-execution-in-the-workspace-run_python_sandboxed--run_sql_sandboxed-0166-0-wasm-sandbox-0224-0).
