"""bridge_const.py — shared logger + module-level constants for the Bridge.

Extracted from bridge.py so the Bridge mixin modules can import them without a
circular dependency (bridge.py imports the mixins; the mixins import this).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_CANCEL_KEY = "__cancel_event__"  # key in _in_flight dicts, not surfaced to JS

# Autonomy escalation order — higher = more restrictive (must check before run).
_AUTONOMY_RANK = {"autonomous": 0, "confirm": 1, "supervised": 2, "manual": 3}

# Built-in (not runnable-backed) agent tools. Names are intentionally free of
# "__" so they never collide with the host__runnable scheme. download_file /
# relay_file are side-effecting file moves; manage_denylist writes a host's venv
# denylist (a kill switch) — all three prompt before running (the autonomy gate
# treats every _CUSTOM_TOOL_NAMES entry as "confirm").
_CUSTOM_TOOL_NAMES = frozenset({"download_file", "relay_file", "manage_denylist"})

# Read-only discovery meta-tools used by search mode (tool_mode = "search"). They
# have no side effects, so the autonomy gate lets them run without a prompt; the
# side-effecting executor in that mode is ``run_runnable``, gated by the *inner*
# runnable it resolves to.
_READONLY_META = frozenset({"search_runnables", "describe_runnable"})

# Persistent-memory / runbook tools. Backed by local TOML collections (see
# memory.py) — local and reversible, so the autonomy gate lets them run without a
# prompt (like the read-only meta-tools), keeping the model spend on actual work.
_MEMORY_TOOL_NAMES = frozenset(
    {"remember", "search_memory", "save_runbook", "load_runbook"}
)

# Read-only status tool: report the live working-hours rota + configured
# triggers/schedules on demand (the same data folded into each turn's context,
# but queryable mid-session after the clock may have crossed a rota boundary). No
# side effects, so the autonomy gate runs it without a prompt.
_STATUS_TOOL_NAMES = frozenset({"console_status"})

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
        "create_schedule",
        "emit_event",
        "open_url",
        "open_file",
        "reveal_in_folder",
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
    }
)

# Code-execution tools rooted in the current session's workspace (cwd jailed to
# it). ``run_python`` executes an arbitrary snippet/file with the console's own
# interpreter and is ALWAYS confirm-gated — it runs as the operator with full
# privileges, so the confirm gate (not the path-jail) is the security control.
# ``run_sql`` runs SQL against a sqlite file in the workspace; read-only queries
# (SELECT/EXPLAIN/read PRAGMA) run autonomously via a read-only connection,
# writes/DDL confirm-gate. Both dropped entirely when ``[workspace] code_exec``
# is false. See _effective_tool_autonomy / _run_exec_tool / codeexec.py.
_EXEC_TOOL_NAMES = frozenset({"run_python", "run_sql"})

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
    {"manage_hosts", "manage_groups", "manage_triggers", "manage_rota"}
)

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
    | _ACTION_TOOL_NAMES
    | _WORKSPACE_TOOL_NAMES
    | _EXEC_TOOL_NAMES
    | _CONFIG_TOOL_NAMES
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

# One budget for a single chat turn's model round-trips: tool-calling iterations
# AND auto-continuations after an output-ceiling cut-off (stop_reason ==
# "max_tokens") both draw from it, so a cut-off-heavy turn can't starve tool use
# or loop unbounded. Was a bare ``range(10)``.
_MAX_AGENT_ITERATIONS = 10

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

_EVENT_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
