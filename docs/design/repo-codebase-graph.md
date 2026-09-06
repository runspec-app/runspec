# Codebase knowledge-graph for the Repos feature — evaluation + direction

**Status:** evaluation / discussion. No code yet. Captures a smoke-tested finding
and a recommended path so we can decide deliberately rather than re-derive it later.

## Why

The Repos feature (`docs/design/repo-tools.md`) gives the agent on-disk checkouts
and seven `repo_*` tools, but code exploration is a **stateless grep/read loop**:
`repo_grep` is a live `os.walk` + per-line regex over raw bytes on *every* call
(capped at 200 hits, then "narrow your search"), `repo_read` is a bounded file
read, `repo_list` walks one directory level at a time. There is **no persisted
index, symbol table, call graph, or cross-reference anywhere in the feature** — the
agent reconstructs structure by grepping, reading, and re-grepping. On a large repo
that's many tool round-trips and a lot of tokens to answer a structural question
("who calls X", "what's the blast radius of changing Y", "what does this service
look like").

A **code knowledge graph** targets exactly this: parse the repo once (tree-sitter),
resolve symbols, store functions/calls/imports/routes as a graph, and answer
structural questions from pre-computed edges. The response scales with the *answer*
size, not the codebase size — so the savings are proportional to the reading the
agent skips. Big repo, big win; small repo, marginal.

This is a **complementary** approach to the "AI wiki" north star
(`docs/design/repo-knowledge-wiki.md`): the graph gives cheap *structural
navigation* (mechanical, always-fresh-on-reindex); the wiki gives accumulated
*semantic knowledge with provenance + freshness*. One is "how is this code wired",
the other is "what have we already learned about it". They stack.

## The load-bearing fact: the console is already an MCP client

Since 0.222.0 the console connects to external MCP servers (`[mcp]` /
`[[mcp.servers]]`, `mcp_client.py`, `McpManager`) and folds their tools into the
agent's toolset as `mcp_<server>_<tool>`, through the **same autonomy/confirm
gate**, with per-server credential + proxy. So a code-graph server that speaks MCP
over stdio drops in as **config, not code** — its tools sit *alongside* the built-in
`repo_*` tools. That makes "reference it" nearly free and de-risks "bundle it".

## What we evaluated

The category is a cluster of 2025–2026 "local code knowledge graph over MCP" tools.
The attributes that matter *for us* are not stars — they are:

| Criterion | Why it matters here |
|---|---|
| **License MIT/Apache** | Required to bundle into a shipped package. |
| **Local, zero API keys for *code*** | Must match the console's no-egress posture. |
| **MCP stdio native** | Drops into `[[mcp.servers]]` with no wrapper. |
| **Persistent index + *file-change* watcher (not just git hooks)** | Survives re-opens; must catch **uncommitted** `repo_write` edits — Repos never commits. |

Two candidates were verified against their actual repos:

- **codebase-memory-mcp** (MIT, static C binary, stdio, SQLite index, file-change
  watcher) — front-runner. Smoke-tested below.
- **Graphify / `graphifyy`** (Apache-2.0 / MIT, Python, stdio, `graph.json`
  in-memory, git-hook + optional `watch`) — viable, but JSON-in-memory (not a
  persistent index) and freshness defaults to git hooks, which **never fire on an
  uncommitted `repo_write`**. Weaker fit.

Others surfaced only via comparison blogs with implausible popularity numbers
(CodeGraph, CodeGraphX/CGX, GitNexus, code-review-graph) and are **unverified**;
GitNexus reportedly has a commercial tier, so it needs license diligence before any
bundling. Treat these as leads, not candidates, until their repos are opened.

## Smoke test — codebase-memory-mcp 0.10.8 on this repo

Run against `/home/user/runspec` (1,023 tracked files; 740 py/ts/js). Installed via
`pip install codebase-memory-mcp` — a **thin downloader that fetched the platform
binary and verified its checksum** before running (the same fetch-on-first-use +
verify pattern the WASM sandbox already ships, 0.224.0).

| Metric | Result |
|---|---|
| Cold index time | **21 s** (includes spinning a temp daemon) |
| Graph size | 17,563 nodes / 77,356 edges |
| Coverage | 0 files skipped; 2 `.tsx` with a harmless line-level partial parse |
| Languages | 8, correctly mapped across all 15 packages (Python 616, TS 120, …) |
| Per-repo index | **62 MB** SQLite (`~/.cache/codebase-memory-mcp/<proj>.db`) |
| Query payloads | full architecture map **3.7 KB** · `sync_repo` call graph **567 B** |
| Freshness | `auto_watch = true` — a **file-change** watcher, not git hooks |

**The head-to-head that matters** — "who calls `sync_repo`?", the thing the agent
does today with a grep→read→grep→read loop:

1. It first **disambiguated three same-named symbols** across the monorepo: the real
   Python `repos.sync_repo` function, the `bridge/mock.ts` TS mock, and the
   `FleetMixin.sync_repo` bridge method. A flat `repo_grep` returns all of these
   mixed together with no kind/type distinction.
2. `trace_path inbound` on the real one returned its **actual call graph in 567
   bytes** with hop distances — `_run_repo_tool` (1, the agent dispatch) →
   `_run_tool_sync` (2) → `_gated_run_tool` / `_run_calls_parallel` (3). Correct,
   in one call. The grep loop needs many round-trips and still can't cleanly
   separate the three symbols.
3. Even its grep-equivalent (`search_code`) is richer than `repo_grep`: each hit
   carries qualified name, Function/Method label, line range, and in/out edge
   counts — not just `{path, line, text}`.

**Honest caveats from the run:**

- **The "sub-millisecond query" claim did not reproduce at the CLI level** — each
  warm `cli` call took **~2.1 s**. That is process-spawn + daemon-connect overhead
  *per invocation*, **not** the graph query (which is sub-ms). Over a **persistent
  MCP stdio session** — how the console would actually use it — that cost is paid
  once, so real per-tool latency should be far lower. **This is the single biggest
  unvalidated assumption** and must be measured in the real MCP path, not assumed.
- **62 MB index per repo.** Fine for a few; a fleet operator with ~20 registered
  repos is ~1 GB of cache. Manageable, but a real footprint to budget.
- The full cache was 342 MB, but most is the one-time binary + bundled embedding
  assets, shared across all repos — not per-repo.

## Smoke test — Graphify (`graphifyy` 0.9.55) contrast

Same repo, same questions. `pip install graphifyy`; built the graph offline with
`graphify extract /path --code-only --no-cluster` (the `--code-only` path needs no
LLM key — a key is required only for the 196 doc/PDF files, which we skipped).

| Metric | Graphify | codebase-memory-mcp |
|---|---|---|
| Build time | 25 s (AST, 4 workers) | 21 s |
| Graph size | 14,324 nodes / 34,014 edges | 17,563 / 77,356 |
| Store | **18 MB `graph.json`** (loaded into memory per query) | 62 MB SQLite (persistent) |
| Query latency (CLI) | **~670 ms** (reloads the JSON each call — no daemon) | ~2.1 s (spawn + daemon connect) |
| License | Apache-2.0 / MIT | MIT |

**Where Graphify came out weaker for our use:**

- **Call-graph precision.** For "who calls `sync_repo`", `affected` surfaced mostly
  **test** callers, and `query "who calls sync_repo"` did a fuzzy BFS that matched
  *three* start nodes (the Python `sync_repo()`, a TS `Repo` class, and a test
  `repo()`) and returned a 39-node / **~4,800-token** neighborhood mixing tests,
  error classes, and gitsync helpers — over its own 2,000-token budget. Neither
  isolated the production dispatch path (`_run_repo_tool` → `_gated_run_tool` →
  `_run_calls_parallel`) that codebase-memory-mcp's `trace_path inbound` returned
  exactly, in 567 bytes. Its own `benchmark` reports an **average query cost of
  ~16k tokens** — an order of magnitude above codebase-memory-mcp's sub-KB payloads.
  It *did* correctly distinguish the `.sync_repo()` method from the `sync_repo()`
  function, so symbol resolution is sound; it's the call-edge precision and query
  ergonomics that lag.
- **The MCP server serves a *static* pre-built file.** `python -m graphify.serve
  --graph graph.json --transport stdio` reads a graph.json produced by a *separate*
  `extract`/`update`/`watch` step. So freshness is fully external to the server
  (git hooks or `watch` rebuild the file); there is no index-on-demand. That is a
  worse fit for the console than codebase-memory-mcp's server, which owns indexing
  and a file-change watcher.
- **It writes into the repo working tree.** `extract` dropped an 18 MB
  `graphify-out/graph.json` **inside** the checkout by default. In the Repos model
  that is exactly the wrong place — it shows up in `repo_diff`, risks being
  committed, and a re-`repo_sync` would discard it. codebase-memory-mcp stores in
  `~/.cache`, outside the repo. (We moved the artifact out; the repo tree is clean.)

**Net:** both are real, fast, permissively licensed, and offline for code. Graphify
is the lighter install (pure Python, no binary) and its per-call latency is lower at
the CLI. But codebase-memory-mcp wins on the things that matter for this feature —
tighter, more precise structural answers, a persistent index, a self-owned
file-watcher, and out-of-repo storage. codebase-memory-mcp stays the front-runner;
Graphify is the fallback if bundling a third-party binary proves unacceptable and a
pure-Python dependency is preferred (accepting the noisier answers and the
external-build/in-repo-output wrinkles).

## Integration options

### 1. Reference it — ~zero code, do this first

One `[[mcp.servers]]` entry; the server's tools appear next to `repo_*`:

```toml
[mcp]
enabled = true

[[mcp.servers]]
name      = "codebase-graph"
transport = "stdio"
command   = "codebase-memory-mcp"
autonomy  = "autonomous"   # its query tools are reads; safe to auto-run
enabled   = true
```

`[mcp]` already rides Config Sync / the config seed, so one entry distributes the
capability fleet-wide from the versioned source. Costs nothing to trial and proves
the value on real repos before any deeper investment.

**Ready-to-paste trial** (operator steps, no code change):

1. `pip install runspec-console[mcp]` (the MCP client SDK is behind the `mcp` extra),
   and put the server on `PATH` once: `pip install codebase-memory-mcp` (or drop the
   static binary somewhere on `PATH`).
2. Add the block above to `config.toml` (or Settings → MCP → add server: transport
   `stdio`, command `codebase-memory-mcp`, autonomy `autonomous`). Save →
   `mcp_reload` reconnects; its tools appear as `mcp_codebase_graph_*` next to
   `repo_*`.
3. In chat, tell the agent to `index_repository` the checkout once (the path is
   `{app_dir}/repos/<name>`; `repo_status` prints the registered repos), then ask a
   structural question and watch it use `mcp_codebase_graph_trace_path` /
   `_get_architecture` instead of the grep loop.
4. **Measure** (the go/no-go): per-tool latency over the persistent session
   (not the ~2.1 s CLI number), and whether the grep/read round-trips actually drop.

If Microsoft Graph / egress is a concern: this server is fully local and needs no
network — leave it stdio, no `credential`, no `proxy`.

**Limitations of the pure-reference path:**
- **Autonomy is per-server, not per-tool.** All ~15 tools share one `autonomy`
  setting, but `index_repository` / `delete_project` are writes while the query
  tools are reads. You either confirm-gate everything (annoying) or auto-approve the
  indexers too (coarse). The built-in `repo_*` tools have a proper read/write split
  (`_REPO_READ_TOOLS` autonomous, `_REPO_WRITE_TOOLS` confirm) that this loses.
- **Two disconnected worlds.** It keeps its own index and needs to be pointed at a
  path *the agent supplies*; it knows nothing about `checkout_dir(name)`, the
  registry, or that a `repo_sync`/`repo_write` just changed the tree. The agent has
  to drive indexing itself.
- **Trust.** A third-party binary that reads all the code. It is local-only,
  no-telemetry, SLSA-3 + cosign-signed — which fits the posture — but it is an
  opt-in supply-chain decision the operator makes per install.

### 2. Incorporate it — moderate work, real product win (only if #1 proves out)

Make it invisible plumbing behind the Repos feature rather than a bolt-on:
- Auto-run `index_repository` against `checkout_dir(name)` at the end of `sync_repo`,
  and re-index after `repo_write` (the `auto_watch` file watcher covers most of
  this; owning the trigger makes it deterministic).
- Add graph-backed tools (`repo_trace`, `repo_architecture`) or back `repo_grep`
  with graph queries, **falling back to the current `os.walk`** when the graph is
  cold or the binary is absent.
- Keep the read/write autonomy split; keep the registry as the allowlist.
- **Distribution:** ship it the WASM-sandbox way — fetch-on-first-use, SHA-256
  verified, an explicit-path / air-gapped override, and **fail closed** (drop the
  tools when the binary is unavailable — never silently fall back to something
  unsanctioned). MIT means bundling is legally clean.

### 3. Copy / port it — rejected

Porting the C core means reimplementing 162 vendored tree-sitter grammars plus a
custom hybrid-LSP type resolver for 12 languages — the entire product. It also
breaks the codebase's own layering instinct (pure cores are stdlib-only). No reason
to: MIT + a prebuilt static binary means referencing or bundling gets all of it free.

## Freshness & the `repo_write` fit

This was the main integration worry (console edits are local-only, never committed).
codebase-memory-mcp's `auto_watch` is a **file-change** watcher, so a `repo_write`
edit *would* trigger re-indexing without a commit — a genuine advantage over
Graphify's git-hook default. Still worth an explicit re-index trigger on
`repo_sync`/`repo_write` in the incorporate path so freshness isn't left to a
best-effort watcher's timing.

## Risks / open questions

- **Persistent-session latency (blocking).** The ~2.1 s CLI number must be re-run
  over a live `[[mcp.servers]]` stdio session in the running console before we trust
  the feature. The smoke test could not launch pywebview.
- **Index footprint** at fleet scale (~62 MB/repo). Cap? Evict on repo delete
  (`delete_checkout` should also `delete_project`)?
- **Per-tool autonomy** is lost via the reference path; only the incorporate path
  restores the read/write split.
- **Supply chain.** Pin a version, verify the checksum ourselves, decide whether we
  bundle the binary or fetch it. Re-confirm license before shipping.
- **Relationship to the AI-wiki doc.** If both land, the graph is the mechanical
  navigation layer and the wiki is the semantic-knowledge layer; make sure they
  don't grow overlapping "what does this code do" surfaces.

## Recommendation

Start with **option 1** as an experiment in the running console: add the
`[[mcp.servers]]` entry, point it at a large real repo, and measure (a) actual
per-tool latency over the persistent session and (b) whether the graph tools cut the
grep/read churn on *our* repos. If it proves out, invest in **option 2** for the
per-tool autonomy split, auto-index-on-sync, and fail-closed bundling that make it
feel native. Skip **option 3** entirely.

**Revisit trigger:** pick this up when someone next touches the Repos feature or
reports the agent's code exploration being slow/expensive on a big repo — re-run the
persistent-session latency test first; that number decides go / no-go.

## Appendix — reproduce the smoke test

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install codebase-memory-mcp                      # thin downloader, checksum-verified binary
codebase-memory-mcp daemon start                     # warm daemon (removes per-call startup)
codebase-memory-mcp cli --json index_repository --repo-path /path/to/repo
codebase-memory-mcp cli --json get_architecture --project <proj>
codebase-memory-mcp cli --json trace_path --project <proj> \
  --function-name '<qualified.name>' --direction inbound --depth 3
codebase-memory-mcp daemon stop
```

Index lands at `~/.cache/codebase-memory-mcp/<proj>.db`; `config list` shows
`auto_watch`. Tools: `index_repository`, `search_graph`, `query_graph`,
`trace_path`, `get_code_snippet`, `get_graph_schema`, `get_architecture`,
`search_code`, `list_projects`, `delete_project`, `index_status`,
`check_index_coverage`, `detect_changes`, `manage_adr`, `ingest_traces`.

Graphify contrast (writes `graphify-out/` into the target dir — run from a scratch
dir, not the checkout):

```bash
pip install graphifyy
graphify extract /path/to/repo --code-only --no-cluster   # offline AST, no key
graphify affected "sync_repo()"                            # reverse traversal
graphify query "who calls sync_repo"                       # fuzzy BFS
python -m graphify.serve --graph graphify-out/graph.json --transport stdio
```
