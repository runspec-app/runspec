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
# follow a saved runbook, create a schedule, or emit an event into the trigger
# engine. All four are side-effecting, so the autonomy gate prompts before each
# (treated as "confirm" in _effective_tool_autonomy). Names are "__"-free so they
# never collide with the host__runnable scheme.
_ACTION_TOOL_NAMES = frozenset(
    {"create_document", "run_runbook", "create_schedule", "emit_event"}
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
        "workspace_ls",
        "workspace_read",
        "workspace_grep",
        "workspace_head",
        "workspace_tail",
        "workspace_write",
    }
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

PUTTY_PROXY_SESSION = "runspec-console-proxy"

_EVENT_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
