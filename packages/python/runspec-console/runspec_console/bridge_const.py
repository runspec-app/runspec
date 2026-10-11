"""bridge_const.py — shared logger + module-level constants for the Bridge.

Extracted from bridge.py so the Bridge mixin modules can import them without a
circular dependency (bridge.py imports the mixins; the mixins import this).
"""

from __future__ import annotations

import contextvars
import logging

logger = logging.getLogger(__name__)

# The model's tool-call id for the call currently being executed. Set per call by
# the agent loop (bridge_turn._exec_call) and read by _register_confirm, so the
# confirm event names the exact card it belongs to. Matching by tool name alone
# put the gate on every running card with that name — in search mode every
# runnable call is `run_runnable`, so a gate could land on an earlier card.
CURRENT_CALL_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "runspec_current_call_id", default=""
)

_CANCEL_KEY = "__cancel_event__"  # key in _in_flight dicts, not surfaced to JS

# The built-in base system prompt for every agent turn (chat / trigger /
# schedule) when ``[llm] system`` is unset. Exposed to the Settings → Model / API
# tab via ``Bridge.default_system_prompt()`` so the field is pre-filled with what
# actually runs; a saved value equal to this is dropped from config.toml so the
# install keeps tracking future changes to the default (see save_config).
DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant embedded in runspec-console. You can run "
    "runspec runnables on local and remote hosts via tools. Use tools "
    "when they help, and confirm before anything destructive.\n\n"
    "When a required argument is missing or ambiguous — and getting it "
    "wrong would matter (scheduling, sending mail, targeting a host, or "
    "anything destructive) — ask the operator one brief clarifying question "
    "instead of guessing. Prefer a quick question over a wrong assumption. "
    "For low-stakes or clearly-implied values, go ahead; don't over-ask.\n\n"
    "Saved runbooks are the operator's standing procedures. Prefer following "
    "an existing one over improvising. When a correction you were just given "
    "would apply again next time, offer to capture it with save_runbook — "
    "the operator approves every save, so propose rather than ask permission "
    "first.\n\n"
    "The knowledge wiki holds what this team has learned about its fleet: "
    "where things live, who owns what, and each host's quirks. Check "
    "search_wiki before asking the operator or guessing such a detail, and "
    "when you learn something durable that isn't there, record it with "
    "propose_fact (it goes to the operator's review inbox)."
)

# Standing session-workspace guidance, folded into every agent turn's system
# prompt by Bridge._workspace_guidance (chat via _spawn_chat_turn, headless via
# _trigger_run_agent_turn) and into the console_status snapshot. It lives OUTSIDE
# DEFAULT_SYSTEM_PROMPT on purpose: a configured ``[llm] system`` replaces that
# prompt wholesale, but the workspace_* tools are offered regardless of persona,
# so the model must always be told how to read a spilled result back — and that
# the read-only reshaping tools come before the confirm-gated sandbox. Static
# text (no ids, no dates) so the model-side prompt cache still hits.
WORKSPACE_GUIDANCE = (
    "## Session workspace\n"
    "A runnable that emits more than a small page of output is **spilled** to "
    "disk instead of inlined — its tool result is a short envelope with a "
    "shape/head preview and a `tool-results/…` path. Use workspace_ls to see "
    "what's there, workspace_read / workspace_head / workspace_tail to page "
    "through a text file, workspace_grep to search one, and workspace_write to "
    "save a distilled note. All autonomous; scoped to this session only.\n"
    "To read a spilled **JSON** file, use workspace_json_get (one field by "
    "dotted path / JSON Pointer, paged) — a minified JSON object is one line, "
    "so the line tools won't help there. To summarise a spilled **text log**, "
    "use workspace_materialize (as='digest'). Both are read-only and run "
    "without asking; reach for run_python_sandboxed only when you need to "
    "compute, join or transform the data, not merely to read a field of it.\n"
    "Choose a runnable call's `deliver` from the task: `inline` to read a "
    "result you expect to be small, `extract` to read the main text field of "
    "a JSON document, `digest` to see why something failed (clusters a log); "
    "otherwise leave it `auto`."
)

# Autonomy escalation order — higher = more restrictive (must check before run).
_AUTONOMY_RANK = {"autonomous": 0, "confirm": 1, "supervised": 2, "manual": 3}

# Built-in (not runnable-backed) agent tools. Names are intentionally free of
# "__" so they never collide with the host__runnable scheme. download_file /
# relay_file are side-effecting file moves; manage_denylist writes a host's venv
# denylist (a kill switch) — all three prompt before running (the autonomy gate
# treats every _CUSTOM_TOOL_NAMES entry as "confirm").
_CUSTOM_TOOL_NAMES = frozenset({"download_file", "relay_file", "manage_denylist"})

# Prefix on every external-MCP tool's display name (mcp_client.py). External MCP
# tool names are dynamic, so — unlike the built-in name-sets — MCP membership is
# decided by the live registry (Bridge._is_mcp_tool). This prefix is kept free of
# "__" (the host__runnable separator) so an MCP name is never mistaken for a
# runnable, and lets self-free contexts (the _filter_headless_tools staticmethod)
# recognise an MCP tool by name alone.
_MCP_PREFIX = "mcp_"

# Read-only discovery meta-tools used by search mode (tool_mode = "search"). They
# have no side effects, so the autonomy gate lets them run without a prompt; the
# side-effecting executor in that mode is ``run_runnable``, gated by the *inner*
# runnable it resolves to.
_READONLY_META = frozenset({"search_runnables", "describe_runnable"})

# Built-in tools that only read, so several from one model response may run at
# once (``Bridge._parallel_safe_call``): status, run history, file listing, web
# fetch, workspace reads, wiki reads, repo reads and the deferred-tool schema
# lookup. Left out on purpose: anything that writes (memory, wiki proposals,
# runbooks, workspace_write/materialize, repo_sync/write), Outlook (COM is
# apartment-bound) and search_marketplace (a cold read rewrites its cache file).
_PARALLEL_READONLY_BUILTINS = frozenset(
    {
        "console_status",
        "search_history",
        "list_files",
        "fetch_url",
        "workspace_info",
        "workspace_ls",
        "workspace_read",
        "workspace_head",
        "workspace_tail",
        "workspace_grep",
        "workspace_json_get",
        "search_wiki",
        "wiki_page",
        "wiki_graph",
        "list_wiki_inbox",
        "repo_list",
        "repo_read",
        "repo_grep",
        "repo_status",
        "repo_diff",
        "describe_tool",
    }
)

# Built-in deferral meta-tools (search mode, chat turns only): the long tail of
# built-in tools leaves the per-turn tool list and is reached via describe_tool
# (read-only schema lookup — autonomous) + use_tool (a thin wrapper the autonomy
# gate unwraps to the INNER tool before deciding anything, so the inner tool's
# own confirm rules, Console card, and audit record apply unchanged). Headless
# (trigger/schedule) turns never see these — they keep inline built-ins so
# ``action_allow`` narrowing keeps working — and ``_filter_headless_tools``
# strips them defensively.
_BUILTIN_META_NAMES = frozenset({"describe_tool", "use_tool"})

# The runbook store's write tool (memory.py). The older fact memory (remember /
# search_memory / list_memories / forget_memory) was folded into the team knowledge
# wiki in 0.332.0 — facts are propose_fact / search_wiki, pins are pin_wiki_fact,
# and prune is retire_wiki_fact (see _KNOWLEDGE_TOOL_NAMES). save_runbook is
# confirm-gated in _effective_tool_autonomy: a runbook is a procedure that gets
# followed later. This frozenset is the dispatch key (_run_memory_tool) and the
# search-mode hot-kernel key.
_MEMORY_TOOL_NAMES = frozenset({"save_runbook"})

# load_runbook — read a runbook's *raw definition* (title/symptom/tags/steps
# array/args table) as data, for inspecting or editing it. Distinct from
# run_runbook, which renders the steps to "follow now"; this returns the editable
# source and never executes. It is **chat-only**, the same guarantee the config
# tools carry: stripped from every headless (trigger/schedule) turn by
# ``_filter_headless_tools`` (layer 1) and refused by an origin guard in its
# handler (layer 2). That is what closes the old governance gap — the removed
# load_runbook (0.202.0) returned the *same followable steps* as run_runbook and,
# sitting outside the action_allow vocabulary, let a trigger follow a runbook its
# allowlist excluded. Confined to interactive chat, that bypass cannot exist:
# reading-to-edit is an operator activity, and headless runs still go through
# run_runbook under action_allow.
_RUNBOOK_READ_TOOL_NAMES = frozenset({"load_runbook"})

# Read-only status tool: report the live working-hours rota + configured
# triggers/schedules on demand (the same data folded into each turn's context,
# but queryable mid-session after the clock may have crossed a rota boundary). No
# side effects, so the autonomy gate runs it without a prompt.
_STATUS_TOOL_NAMES = frozenset({"console_status"})

# Read-only run-history search: query the same audit records the History tab
# shows (when a runnable last ran, on which host, started by whom, exit code,
# duration, args). No side effects, so the autonomy gate runs it without a
# prompt — like the status tool, it only reads what the operator already sees.
_HISTORY_TOOL_NAMES = frozenset({"search_history"})

# Read-only directory listing: local, a fleet host, or an ad-hoc SSH host —
# the browse-before-transfer companion to download_file/relay_file, over the
# same machinery the Forms path-picker and the Transfer dialog's ad-hoc
# browser use. No side effects, so the autonomy gate runs it without a prompt.
_FILES_TOOL_NAMES = frozenset({"list_files"})

# Read-only web-page fetch: GET an http(s) URL on the console machine's network
# and hand back its readable text (HTML stripped to headings/lists/paragraphs),
# so the agent can read an online doc — e.g. a GitLab Pages / wiki page — and
# turn it into a runbook. A GET has no side effects, so the autonomy gate runs
# it without a prompt (like list_files / repo_read); the capability is offered
# only when [web] enabled (default true). Offered on any console (no fleet host
# needed). Dropped entirely by [web] enabled = false.
_WEB_TOOL_NAMES = frozenset({"fetch_url"})

# Draw a Mermaid diagram to a PNG in the session workspace, using the console's
# own web view (bridge_diagram.py). Autonomous: it only writes in the workspace.
_DIAGRAM_TOOL_NAMES = frozenset({"render_diagram"})

# Improve: generate an effectiveness / problem report by mining this install's own
# saved conversations + run history (tool-search funnels, argument bounces, stops,
# errors, autonomy denials). Read-only analysis — no side effects, so the autonomy
# gate runs it without a prompt. Rare and heavy (it reads across the whole history),
# so it is NOT a hot built-in — fine deferred behind describe_tool/use_tool in
# search mode. Offered only when [improve] enabled (default true).
_IMPROVE_TOOL_NAMES = frozenset({"run_improve_report"})

# Read-only marketplace discovery: search the package indexes' catalogue (the
# same entries the Marketplace tab shows) for runnable packages that are
# INSTALLABLE but not necessarily installed — the "is there a runnable for X I
# could add?" companion to search_runnables (which only sees what's installed).
# Discovery only: it reads the catalogue and recommends; installing stays an
# operator action in the Marketplace tab. Honours [marketplace] hide (via
# marketplace_catalog) so a curated-out public package is never recommended. No
# side effects, so the autonomy gate runs it without a prompt. Rare/niche and it
# reads the whole catalogue, so it is NOT a hot built-in — fine deferred behind
# describe_tool/use_tool in search mode. Offered only when [marketplace] enabled
# (default true); dropped entirely by [marketplace] enabled = false.
_MARKETPLACE_TOOL_NAMES = frozenset({"search_marketplace"})

# Team knowledge wiki (knowledge.py / bridge_knowledge.py). Reads + propose are
# autonomous (a proposal only lands in the review inbox — the inbox is the gate);
# review / edit / retire change what every console will eventually read, so they
# are confirm-gated in _effective_tool_autonomy. Not AI-gated: with [console]
# ai = false an MCP client (Claude Code) can still read and curate the wiki.
_KNOWLEDGE_TOOL_NAMES = frozenset(
    {
        "search_wiki",
        "wiki_page",
        "wiki_graph",
        "propose_fact",
        "list_wiki_inbox",
        "review_wiki_fact",
        "edit_wiki_fact",
        "retire_wiki_fact",
        "pin_wiki_fact",
    }
)
_KNOWLEDGE_WRITE_TOOL_NAMES = frozenset(
    {"review_wiki_fact", "edit_wiki_fact", "retire_wiki_fact"}
)
# The wiki tools a turn reaches for constantly stay inline in search mode.
_KNOWLEDGE_HOT_NAMES = frozenset({"search_wiki", "wiki_page", "propose_fact"})

# Outlook Classic (desktop COM) agent tools. Reads are autonomous (no side
# effects — local COM reads on the operator's box); draft/send are write actions
# and prompt before running. Only offered when Outlook COM is available (Windows
# + pywin32). These ride the desktop's signed-in Outlook session, so they work in
# locked-down environments that block the Graph device-code flow.
_OUTLOOK_READ_TOOLS = frozenset(
    {
        "outlook_inbox",
        "outlook_search",
        "outlook_get_message",
        "outlook_calendar",
        "outlook_reminders",
        "outlook_resolve_recipient",
        "outlook_event_join_url",
    }
)

# draft/send mail, save an attachment to disk, create a calendar event, respond to
# an invite, and triage actions (mark read / categorize / move) — all
# side-effecting, so they prompt before running.
_OUTLOOK_WRITE_TOOLS = frozenset(
    {
        "outlook_draft",
        "outlook_send",
        "outlook_save_attachment",
        "outlook_create_event",
        "outlook_respond_invite",
        "outlook_mark_read",
        "outlook_categorize",
        "outlook_move",
    }
)

_OUTLOOK_TOOL_NAMES = _OUTLOOK_READ_TOOLS | _OUTLOOK_WRITE_TOOLS

# Git-repo (local checkout) agent tools. Reads are autonomous (read-only over a
# local checkout); repo_sync (clones/fetches + writes a working tree, discarding
# local edits) and repo_write (writes a file) are side-effecting and prompt first.
# Only offered when the git engine (dulwich) is available and at least one repo is
# registered in runspec_repos.toml. No commit/push — edits are local only.
_REPO_READ_TOOLS = frozenset(
    {"repo_status", "repo_list", "repo_grep", "repo_read", "repo_diff"}
)

_REPO_WRITE_TOOLS = frozenset({"repo_sync", "repo_write"})

_REPO_TOOL_NAMES = _REPO_READ_TOOLS | _REPO_WRITE_TOOLS

# Built-in "action" tools beyond running a runnable — write a markdown document,
# follow a saved runbook, create a schedule, emit an event into the trigger
# engine, or open a URL in the operator's browser. All are side-effecting, so the
# autonomy gate prompts before each (treated as "confirm" in
# _effective_tool_autonomy). Names are "__"-free so they never collide with the
# host__runnable scheme.
_ACTION_TOOL_NAMES = frozenset(
    {
        "create_document",
        "run_runbook",
        "run_saved_run",
        "create_schedule",
        "emit_event",
        "open_url",
        "open_file",
        "reveal_in_folder",
        "play_sound",
    }
)

# Per-session workspace file tools. When a runnable's stdout exceeds
# ``_TOOL_OUTPUT_SPILL_THRESHOLD`` it is written to the current session's
# ``workspaces/{id}/tool-results/`` folder instead of inlined, and the model
# reaches for these tools to interrogate the file — list / read a window /
# grep / head / tail / write a note. All are read-only or scoped to writing
# inside the current session's workspace (path-jailed), so the autonomy gate
# lets them run without a prompt (like the memory + repo-read tools).
_WORKSPACE_TOOL_NAMES = frozenset(
    {
        "workspace_info",
        "workspace_ls",
        "workspace_read",
        "workspace_grep",
        "workspace_head",
        "workspace_tail",
        "workspace_write",
        # After-the-fact reshaping of a spilled file — read-only, so autonomous
        # like the rest, and hot in search mode alongside them.
        "workspace_json_get",
        "workspace_materialize",
    }
)

# Code-execution tools that run inside a WASM/WASI capability sandbox rooted at the
# current session's workspace (see sandbox.py). ``run_python_sandboxed`` executes
# an arbitrary snippet/file inside the sandbox — no network, no host filesystem
# beyond the workspace, no subprocess — and is ALWAYS confirm-gated (operator
# governance on top of the boundary). ``run_sql_sandboxed`` runs SQL against a
# sqlite file in the workspace *in the same sandbox*; read-only queries
# (SELECT/EXPLAIN/read PRAGMA) run autonomously via a read-only connection,
# writes/DDL confirm-gate. Both dropped entirely when the sandbox runtime is
# unavailable (fail-closed) or ``[workspace] code_exec`` / ``[sandbox] enabled``
# is false. See _effective_tool_autonomy / _run_exec_tool / codeexec.py / sandbox.py.
_EXEC_TOOL_NAMES = frozenset({"run_python_sandboxed", "run_sql_sandboxed"})

# Console-configuration tools — edit the fleet (runspec_hosts.toml), the group
# registry (runspec_groups.toml), triggers (runspec_triggers.toml), and the
# working-hours rota. Read actions (list/get) are autonomous; every mutation
# confirm-gates. These are CHAT-ONLY: a trigger- or schedule-originated agent
# turn must never see them (a trigger turn that can edit triggers could widen
# its own ``action_allow`` or mint new triggers — automation must not rewrite
# itself). Enforced in two layers: stripped unconditionally from the isolated
# turn's toolset (``_filter_headless_tools``) and refused at dispatch when the
# run originates from a trigger (``_run_config_tool`` origin guard) — the
# latter covers a hand-written rule trigger naming one of these as its ``run``.
# Deliberately NOT part of the ``action_allow`` vocabulary (list_action_tools).
_CONFIG_TOOL_NAMES = frozenset(
    {
        "manage_hosts",
        "manage_groups",
        "manage_triggers",
        "manage_rota",
        "manage_saved_runs",
    }
)

# The verb shown on the confirm card — the Approve button ("Approve & <verb>")
# and the header ("the assistant wants to <verb> …"). Keyed by the *resolved*
# (display) tool name; anything absent falls back to ``_CONFIRM_VERB_DEFAULT``.
# The default "run" fits a runnable or an executing tool, but a confirm-gated
# tool that stores/creates/opens rather than executes reads wrong as "run" —
# most sharply ``save_runbook``, whose card used to say "run save_runbook",
# implying it would *execute* the runbook instead of storing it for later.
_CONFIRM_VERB_DEFAULT = "run"
_CONFIRM_VERBS = {
    "save_runbook": "save",
    "retire_wiki_fact": "retire",
    "review_wiki_fact": "review",
    "edit_wiki_fact": "edit",
    "create_document": "create",
    "create_schedule": "create",
    "open_url": "open",
    "open_file": "open",
    "reveal_in_folder": "reveal",
}


# Every built-in (non-runnable-backed) agent tool. ``_gated_run_tool_async``
# wraps each of these — once past the autonomy gate — with a live Console
# in-flight card, cooperative cancellation, progress streaming, and a
# History/Analytics audit record, so the agent's own work is as visible and
# stoppable as a runnable run. Deliberately EXCLUDES ``run_runnable`` and the
# search meta-tools (``search_runnables``/``describe_runnable``): the former
# resolves to a real runnable execution that already registers its own
# ``_in_flight`` card via ``invoke_runnable`` (wrapping here would
# double-register), the latter are pure read-only discovery.
_BUILTIN_TOOL_NAMES = (
    _CUSTOM_TOOL_NAMES
    | _OUTLOOK_TOOL_NAMES
    | _REPO_TOOL_NAMES
    | _MEMORY_TOOL_NAMES
    | _STATUS_TOOL_NAMES
    | _HISTORY_TOOL_NAMES
    | _FILES_TOOL_NAMES
    | _WEB_TOOL_NAMES
    | _DIAGRAM_TOOL_NAMES
    | _IMPROVE_TOOL_NAMES
    | _MARKETPLACE_TOOL_NAMES
    | _ACTION_TOOL_NAMES
    | _WORKSPACE_TOOL_NAMES
    | _EXEC_TOOL_NAMES
    | _CONFIG_TOOL_NAMES
    | _KNOWLEDGE_TOOL_NAMES
)

# The hot kernel: built-ins that stay INLINE even when search mode defers the
# rest behind describe_tool/use_tool. These are the tools a turn reaches for
# constantly or that other machinery references directly — status + history
# (small, read-only), runbooks + the wiki's search/page/propose (the system
# prompt's pinned-facts block points at search_wiki), and the workspace tools (spilled tool results tell the model to read
# them back via workspace_*; a discovery hop there would break that flow), and
# list_files (read-only browsing — the entry point of most file tasks, kept
# zero-hop so the agent finds exact paths before reaching for the deferred
# transfer tools). Everything else in _BUILTIN_TOOL_NAMES is the deferred
# long tail.
_HOT_BUILTIN_NAMES = (
    _STATUS_TOOL_NAMES
    | _HISTORY_TOOL_NAMES
    | _FILES_TOOL_NAMES
    # fetch_url: read-only web read, the entry point of "make a runbook from this
    # doc" tasks — kept zero-hop so the agent reads the page without a discovery
    # detour through describe_tool/use_tool.
    | _WEB_TOOL_NAMES
    | _MEMORY_TOOL_NAMES
    | _RUNBOOK_READ_TOOL_NAMES
    | _WORKSPACE_TOOL_NAMES
    # run_runbook: the single path to a runbook's *rendered* steps. Its symptom
    # index is folded into every turn, so deferring the tool that acts on that menu
    # meant two discovery hops before the agent could follow a procedure it had
    # already been told about. load_runbook (the raw-definition reader) is hot for
    # the same reason — inspecting/editing a runbook shouldn't need a discovery hop.
    | {"run_runbook"}
    | _KNOWLEDGE_HOT_NAMES
)


# Built-in agent tools the console's OWN MCP server may expose to an external
# coding agent (Claude Code, …) when ``[mcp_server] expose_builtins`` is on. The
# console-SPECIFIC ops/fleet built-ins a coding agent can't do itself — status,
# run history, runbooks, the team knowledge wiki, host→local file
# transfer, Outlook, the action tools, and render_diagram (the console's Mermaid
# renderer — a coding agent usually has no browser engine to draw with).
# Deliberately EXCLUDES:
#   • config tools + search meta-tools — never (automation must not rewrite
#     itself; the meta-tools are internal indirection a direct client doesn't use),
#   • workspace / exec / web / files / repo / improve — a coding agent already has
#     its own file/bash/web tools, and the workspace/exec ones are chat-session
#     bound,
#   • manage_denylist — fleet governance stays operator-only.
# Filtered against the LIVE ``_custom_agent_tools()`` at publish time, so a tool
# unavailable on this install (e.g. Outlook off-Windows) simply isn't offered.
_MCP_SERVER_BUILTIN_NAMES = (
    _STATUS_TOOL_NAMES
    | _HISTORY_TOOL_NAMES
    | _MEMORY_TOOL_NAMES
    | _RUNBOOK_READ_TOOL_NAMES
    | _OUTLOOK_TOOL_NAMES
    | _ACTION_TOOL_NAMES
    | _KNOWLEDGE_TOOL_NAMES
    | _DIAGRAM_TOOL_NAMES
    | frozenset({"download_file", "relay_file"})
)


class ToolCancelled(Exception):
    """Raised inside a built-in agent tool when its cancel Event is set — the
    operator pressed the chat Stop button or the tool's Console-card Cancel.
    Caught by ``_gated_run_tool_async``, which turns it into a "⏹ stopped" tool
    result so the turn ends (Stop) or continues (per-tool Cancel) cleanly."""


# Cap on a single runnable's output handed to the model as a tool result. The
# old 16 KB cap sliced data-heavy runnables (e.g. list-processes / list-containers
# emitting one compact JSON line) *mid-JSON*, so the model received invalid,
# partial data. 1 MB comfortably fits any normal structured result inside a modern
# context window while still bounding a pathological dump; when we do have to cut,
# the marker says so explicitly so the model knows the JSON is incomplete rather
# than silently trusting a corrupt tail.
_AGENT_TOOL_OUTPUT_CAP = 1_000_000

# Char threshold above which a runnable's joined stdout is spilled to the
# session workspace (``workspaces/{session_id}/tool-results/``) instead of
# handed to the model inline. Below the threshold the tool result is the raw
# string, unchanged from the pre-workspace behaviour. 32 KB is small enough
# that a normal structured JSON dump still inlines but a chatty log / dumped
# process list spills — the operator can raise it via ``[workspace]
# spill_threshold`` (0 disables spill, restoring the pre-workspace
# cap-and-truncate behaviour of ``_AGENT_TOOL_OUTPUT_CAP``).
_TOOL_OUTPUT_SPILL_THRESHOLD = 32_768

# How much of the spilled output is quoted in the envelope handed to the model,
# so it sees the shape (JSON keys, log format, error banner) without a
# follow-up file read for trivial cases. Kept small: ~1 K tokens.
_SPILL_ENVELOPE_HEAD_BYTES = 4_096

# Default backstop on the number of tool-execution rounds in a single agent turn
# — a runaway guard, NOT a per-task dial. Sized generously so normal multi-step
# work never reaches it; the real safety controls are the confirm-gate (mutating
# tools pause for the operator) and the Stop button. Overridable via
# ``[llm] max_tool_rounds`` (see Bridge._max_tool_rounds). Output-ceiling
# auto-continuations (stop_reason == "max_tokens") are bounded SEPARATELY by
# ``[llm] max_continuations`` and do NOT draw from this budget, so continuing a
# long reply can never starve tool use. (Was a shared ``range(10)`` for both.)
_MAX_AGENT_ITERATIONS = 25

# Parallel-safe tool calls from one model response (independent runnable runs,
# read-only discovery) run concurrently up to this many at once — ``[llm]
# max_parallel_tools`` overrides (1 = sequential). The SSH pool's own per-host
# ``[ssh] max_concurrent`` still bounds connections underneath.
_DEFAULT_PARALLEL_TOOLS = 8

# Default wall-clock ceiling (seconds) for a ``run_python`` subprocess and the
# default row cap for a ``run_sql`` result. Timeout is operator-overridable via
# ``[workspace] exec_timeout``; the row cap via the tool's ``max_rows`` arg.
_EXEC_TIMEOUT_S = 30
_EXEC_MAX_ROWS = 1_000

# Backstop on how many stdout/stderr lines a *direct* Console run streams to the
# UI card, and the total bytes across them. A runnable that dumps a huge blob
# (e.g. 10k lines of pretty-printed JSON) otherwise floods the WebView with one
# ``runspec:output`` event per line — O(n) IPC round-trips plus O(n²) React
# reconciliation of a growing <pre> — hanging the card for minutes. Past the cap
# the bridge stops dispatching and emits a single sentinel line; the full result
# is unaffected in the runnable's own audit log. Operator-overridable via
# ``[console] output_line_cap`` / ``output_byte_cap`` (0 disables that limit).
# This is a UI-flood guard only — the agent tool-result path has its own
# ``_AGENT_TOOL_OUTPUT_CAP`` and is untouched.
_UI_OUTPUT_LINE_CAP = 5_000
_UI_OUTPUT_BYTE_CAP = 2_000_000

# Max characters of a manual runnable run's output stored in the chat-history
# conversation record (operator-only, for review on reopen). Generous — this is
# for the human to read results, not a model-context cap — but bounded so a
# runaway blob can't bloat the conversation JSON. The full result is always in
# the runnable's own audit log.
_OPERATOR_RUN_OUTPUT_CAP = 20_000

# Cadence (seconds) of the "still running" heartbeat emitted while a built-in
# agent tool executes (see ``_run_builtin_tool_tracked``). It bounds the silent
# stretch when a tool produces no output of its own — a ``repo_read`` of a big
# file, or the gaps between ``repo_grep``'s sparse every-200-files progress —
# by emitting one INFO log line per interval (so the log explains a multi-minute
# gap instead of looking dead) and one ``runspec:tool_progress`` event (so the
# chat card's status line keeps ticking). 12s is long enough that fast tools
# finish before the first tick — adding zero noise for the common case — yet
# short enough that a slow tool (~5 lines/min) reads as alive without bloating
# the log.
_TOOL_HEARTBEAT_SECS = 12

PUTTY_PROXY_SESSION = "runspec-console-proxy"
# Saved session a host with no proxy or jump loads for the [terminal] look.
PUTTY_LOOK_SESSION = "runspec-console-terminal"
# Prefix of the PuTTY saved sessions that open a terminal through a jump host
# (one per jump target — see bridge_utils._ensure_putty_jump_session).
PUTTY_JUMP_SESSION_PREFIX = "runspec-console-jump-"

_EVENT_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# The one user-facing sentence every AI surface returns when `[console] ai =
# false` — chat, runbooks, agent-mode triggers, prompt/runbook/eval schedules,
# evals, improve synthesis. Distinct from "no model provider configured" (an
# unset [llm] provider): this one means the operator/seed switched AI off.
# Deliberately neutral — it does not name the `[console] ai` switch or how to
# flip it, so a locked-down console never advertises a way back into AI.
AI_OFF_MESSAGE = "AI features are not available in this console."
