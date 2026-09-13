"""
improve.py — the pure analysis core behind the console's **Improve** feature.

Improve mines the data the console already keeps on disk — saved chat
conversations (``conversations.py``) and the run-history audit records
(``bridge_reporting`` / run_summary logs) — and turns it into a structured
**effectiveness report**: how well the agent discovers tools, how often it gets a
runnable's arguments wrong, when a turn had to be stopped and given guidance, what
errored, and where the operator had to deny or edit a proposed action. The same
core backs two entry points (one engine, two doors):

* an **evaluation sweep** (manual or scheduled), and
* a **"report a problem"** action — the operator's free-text ``comment`` is carried
  through so the LLM-judge pass (in the bridge) can correlate the complaint against
  the evidence.

This module is **pure and dependency-free** (stdlib only): every function takes
already-parsed inputs (conversation dicts + history-record dicts) and returns plain
JSON-serialisable data, so it is unit-testable without a disk, a model, or the
bridge — the same discipline as ``memory.py`` and the ``*-core`` packages. The
bridge (``bridge_improve.py``) does the I/O: gathering the inputs, persisting the
report, running the optional LLM synthesis, and building the mailto.

Why text-pattern detection: the console records validation **bounces**, the
**"paused after N tool steps"** stop marker, and self-recovery only as prose — a
bounce is a tool result whose text starts with ``Error:``/``✗`` (there is no
structured failure counter), and the stop/recovery markers live inline in the
turn's assistant text. So the detectors here match those exact strings, which are
stable console output (see ``bridge_invoke``/``bridge_triggers``/``bridge_search``).
"""

from __future__ import annotations

import json
import re
from typing import Any

# ── marker strings emitted by the agent loop (kept in sync with the console) ──
# Stop marker: bridge_invoke.py ("say \"continue\"") and bridge_triggers.py
# ("re-run to continue"). We match the stable prefix common to both.
_STOP_RE = re.compile(r"paused after \d+ tool steps")
# Self-recovery reflection turn: bridge_invoke/bridge_triggers emit "↻ Trying to
# recover (n/max)…" before injecting the recovery prompt.
_RECOVERY_RE = re.compile(r"Trying to recover")
# Turn-level durable error surfaced into the assistant stream: "⚠ Error: …".
_DURABLE_ERROR_RE = re.compile(r"⚠ Error:")

# A tool result is a bounce/refusal when its text starts with one of these.
_ERROR_PREFIXES = ("Error:", "✗")

# Tool names that execute a runnable (so an Error: result is an argument/exec
# bounce, not a read-only tool's own error message). "run_runnable" is the
# search-mode executor; a dedup/per_host id carries "__"; direct built-in exec
# tools are handled separately (use_tool wraps a built-in).
_META_RUN = "run_runnable"


# ── incident categories (drives prioritisation + the UI table) ────────────────
CAT_STOP = "stop"
CAT_RECOVERY = "recovery"
CAT_DURABLE_ERROR = "durable_error"
CAT_ARG_BOUNCE = "arg_bounce"
CAT_EMPTY_SEARCH = "empty_search"
CAT_AUTONOMY_DENIED = "autonomy_denied"
CAT_AUTONOMY_EDITED = "autonomy_edited"
CAT_RUN_FAILURE = "run_failure"  # from run_summary history
CAT_CATALOG_DESC = "catalog_description"  # a weak first-sentence catalog entry

# Higher = surfaced first when the incident list is capped.
_CAT_WEIGHT = {
    CAT_DURABLE_ERROR: 100,
    CAT_STOP: 90,
    CAT_RUN_FAILURE: 80,
    CAT_ARG_BOUNCE: 70,
    CAT_RECOVERY: 60,
    CAT_EMPTY_SEARCH: 50,
    CAT_AUTONOMY_DENIED: 40,
    CAT_AUTONOMY_EDITED: 30,
    CAT_CATALOG_DESC: 20,
}

# A catalog-index entry (the first sentence of a runnable's description) shorter
# than this is too terse to tell the agent what the runnable is for. In search
# mode that one sentence is the *whole* entry the agent sees in the always-on menu.
_CATALOG_DESC_MIN_CHARS = 40


def _is_error_text(text: str) -> bool:
    stripped = str(text or "").lstrip()
    return stripped.startswith(_ERROR_PREFIXES)


def classify_bounce(text: str) -> str:
    """Bucket a runnable-exec error result by what the agent got wrong. The
    strings match the preflight bounces (``bridge_search._preflight_validate`` /
    ``_preflight_builtin_input``) and the core validator's remote bounces."""
    low = str(text or "").lower()
    if "unknown argument" in low or "unknown arg" in low or "valid arguments" in low:
        return "unknown_arg"
    if "missing required" in low or "required argument" in low or "is required" in low:
        return "missing_required"
    if (
        "not a valid choice" in low
        or "must be one of" in low
        or "valid options" in low
        or "choose from" in low
    ):
        return "bad_choice"
    if (
        "out of range" in low
        or "must be between" in low
        or "minimum" in low
        or "maximum" in low
        or ">= " in low
        or "<= " in low
    ):
        return "out_of_range"
    if "unknown command" in low or "requires a command" in low or "subcommand" in low:
        return "bad_command"
    return "other"


def _catalog_first_sentence(text: str, maxlen: int = 120) -> str:
    """First sentence of a description as a single trimmed line (capped). Mirrors
    ``bridge_search.SearchMixin._first_sentence`` — this is the exact string the
    search-mode catalog index shows for a runnable, so the quality check sees what
    the agent sees."""
    s = " ".join(str(text or "").split())
    if not s:
        return ""
    for sep in (". ", "! ", "? "):
        i = s.find(sep)
        if 0 <= i < maxlen:
            return s[: i + 1]
    return s if len(s) <= maxlen else s[: maxlen - 1].rstrip() + "…"


def catalog_description_issues(
    runnables: list[dict[str, Any]], *, min_chars: int = _CATALOG_DESC_MIN_CHARS
) -> list[dict[str, Any]]:
    """Flag runnables whose **catalog-index entry** — the first sentence of the
    description — is too weak to guide discovery.

    In search mode that single sentence is the *entire* entry the agent sees for a
    runnable in the always-on menu (``bridge_search._build_catalog_index``), so a
    missing, stub, or copy-pasted sentence quietly degrades every search. Two
    checks, both things a human reviewer would catch:

      * **empty / short** — no description, or a first sentence under ``min_chars``
        (~40) characters; and
      * **duplicate** — the same first sentence as another runnable, so the two are
        indistinguishable in the menu.

    ``runnables`` is a deduped list of ``{id?, name, group?, description}`` (one per
    runnable, not per host). Returns a list of ``{runnable, kind, first_sentence,
    shared_with?}`` in a deterministic order (empties, shorts, then duplicate groups
    by sentence). Pure/SDK-free — unit-testable and reusable by a docs lint."""
    rows: list[tuple[str, str]] = []
    by_sentence: dict[str, list[str]] = {}
    for r in runnables:
        name = str(r.get("name") or "")
        if not name:
            continue
        rid = str(r.get("id") or "")
        if not rid:
            group = str(r.get("group") or "")
            rid = f"{group}/{name}" if group else name
        sentence = _catalog_first_sentence(r.get("description") or "")
        rows.append((rid, sentence))
        key = sentence.strip().lower()
        if key:
            by_sentence.setdefault(key, []).append(rid)

    issues: list[dict[str, Any]] = []
    for rid, sentence in rows:
        stripped = sentence.strip()
        if not stripped:
            issues.append({"runnable": rid, "kind": "empty", "first_sentence": ""})
        elif len(stripped) < min_chars:
            issues.append(
                {"runnable": rid, "kind": "short", "first_sentence": sentence}
            )
    for _key, ids in sorted(by_sentence.items()):
        if len(ids) > 1:
            for rid in ids:
                issues.append(
                    {
                        "runnable": rid,
                        "kind": "duplicate",
                        "first_sentence": next(s for i, s in rows if i == rid),
                        "shared_with": [o for o in ids if o != rid],
                    }
                )
    return issues


def _is_run_attempt(name: str) -> bool:
    """True when a tool call executes a runnable (vs a read-only/meta tool)."""
    return name == _META_RUN or "__" in name


def _runnable_of(name: str, tool_input: Any) -> str:
    """Best-effort runnable identity for grouping. ``run_runnable`` carries the id
    in its input; a ``per_host`` tool name is ``host__runnable`` (as is a legacy
    group-qualified dedup name), and a dedup tool name is the runnable itself."""
    if name == _META_RUN and isinstance(tool_input, dict):
        return str(tool_input.get("id") or "run_runnable")
    if "__" in name:
        return name.split("__", 1)[1]
    return name


def _search_returned_nothing(output: str) -> bool:
    """A ``search_runnables`` result with no runnable and no runbook rows — a
    discovery miss. The result is JSON; fall back to a substring check."""
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        low = str(output or "").lower()
        return '"results": []' in low or "no matching" in low
    if not isinstance(data, dict):
        return False
    results = data.get("results")
    runbooks = data.get("runbooks")
    empty_results = not results if isinstance(results, list) else results in (None, "")
    empty_books = not runbooks if isinstance(runbooks, list) else runbooks in (None, "")
    return bool(empty_results and empty_books)


def _snippet(text: str, limit: int = 240) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _ts_of_turn(turn: dict[str, Any]) -> str:
    return str(turn.get("ts") or "")


# ── main entry point ─────────────────────────────────────────────────────────
def build_report(
    conversations: list[dict[str, Any]],
    history: list[dict[str, Any]],
    *,
    comment: str = "",
    since_iso: str | None = None,
    window_days: int | None = None,
    max_incidents: int = 40,
    product_name: str = "runspec-console",
    developer_emails: list[str] | None = None,
    runnables: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Analyse conversations + run-history into an Improve report (a plain dict).

    ``conversations`` are full conversation records (``{id, turns:[...]}``, the
    shape ``conversations.load`` returns); ``history`` are lean run-history records
    (``get_history_all`` shape: ``{runnable, exitCode, ts, host, ...}``).
    ``comment`` (a problem-report) is carried through verbatim for the synthesis
    pass. ``since_iso`` filters both inputs by timestamp when given (defensive —
    the bridge normally pre-filters). ``runnables`` (optional) is the deduped
    catalog; when given, weak catalog-index descriptions are flagged as incidents
    (see :func:`catalog_description_issues`). Incidents are prioritised and capped
    at ``max_incidents``.
    """
    incidents: list[dict[str, Any]] = []
    metrics = {
        "turns": 0,
        "conversations": 0,
        "searches": 0,
        "empty_searches": 0,
        "describes": 0,
        "runs": 0,
        "runs_after_search": 0,
        "run_attempts": 0,
        "arg_bounces": 0,
        "stops": 0,
        "recoveries": 0,
        "durable_errors": 0,
        "autonomy_denied": 0,
        "autonomy_edited": 0,
    }
    bounce_kinds: dict[str, int] = {}
    per_runnable: dict[str, dict[str, Any]] = {}
    autonomy_by_tool: dict[str, dict[str, int]] = {}

    def _pr(name: str) -> dict[str, Any]:
        return per_runnable.setdefault(
            name,
            {
                "runnable": name,
                "runs": 0,
                "arg_bounces": 0,
                "failures": 0,
                "total_runs_history": 0,
            },
        )

    for conv in conversations:
        conv_id = str(conv.get("id") or "")
        turns = conv.get("turns") or []
        counted_conv = False
        for idx, turn in enumerate(turns):
            # Only chat turns carry agent behaviour; operator_run records are the
            # operator's own manual launches (kind != "chat").
            if turn.get("kind") and turn.get("kind") != "chat":
                continue
            if since_iso and _ts_of_turn(turn) and _ts_of_turn(turn) < since_iso:
                continue
            metrics["turns"] += 1
            if not counted_conv:
                metrics["conversations"] += 1
                counted_conv = True

            assistant = str(turn.get("assistant") or "")
            if _STOP_RE.search(assistant):
                metrics["stops"] += 1
                incidents.append(
                    _incident(conv_id, idx, CAT_STOP, evidence=_stop_snippet(assistant))
                )
            if _RECOVERY_RE.search(assistant):
                metrics["recoveries"] += 1
                incidents.append(
                    _incident(conv_id, idx, CAT_RECOVERY, evidence="self-recovery turn")
                )
            if _DURABLE_ERROR_RE.search(assistant):
                metrics["durable_errors"] += 1
                incidents.append(
                    _incident(
                        conv_id,
                        idx,
                        CAT_DURABLE_ERROR,
                        evidence=_error_snippet(assistant),
                    )
                )

            tools = turn.get("tools") or []
            searched_this_turn = False
            for tool in tools:
                name = str(tool.get("name") or "")
                output = str(tool.get("output") or "")
                ti = tool.get("input")

                if name == "search_runnables":
                    metrics["searches"] += 1
                    searched_this_turn = True
                    if _search_returned_nothing(output):
                        metrics["empty_searches"] += 1
                        q = ""
                        if isinstance(ti, dict):
                            q = str(ti.get("query") or "")
                        incidents.append(
                            _incident(
                                conv_id,
                                idx,
                                CAT_EMPTY_SEARCH,
                                tool=name,
                                evidence=f"query {q!r} returned no runnables/runbooks",
                            )
                        )
                elif name == "describe_runnable":
                    metrics["describes"] += 1

                if _is_run_attempt(name):
                    metrics["run_attempts"] += 1
                    runnable = _runnable_of(name, ti)
                    pr = _pr(runnable)
                    pr["runs"] += 1
                    if name == _META_RUN:
                        metrics["runs"] += 1
                        if searched_this_turn:
                            metrics["runs_after_search"] += 1
                    if _is_error_text(output):
                        kind = classify_bounce(output)
                        metrics["arg_bounces"] += 1
                        pr["arg_bounces"] += 1
                        bounce_kinds[kind] = bounce_kinds.get(kind, 0) + 1
                        incidents.append(
                            _incident(
                                conv_id,
                                idx,
                                CAT_ARG_BOUNCE,
                                tool=name,
                                runnable=runnable,
                                bounce_kind=kind,
                                evidence=_snippet(output),
                            )
                        )

                decision = str(tool.get("operator_decision") or "")
                if decision in ("denied", "edited"):
                    bucket = autonomy_by_tool.setdefault(
                        name, {"denied": 0, "edited": 0}
                    )
                    bucket[decision] += 1
                    if decision == "denied":
                        metrics["autonomy_denied"] += 1
                        cat = CAT_AUTONOMY_DENIED
                    else:
                        metrics["autonomy_edited"] += 1
                        cat = CAT_AUTONOMY_EDITED
                    incidents.append(
                        _incident(
                            conv_id,
                            idx,
                            cat,
                            tool=name,
                            evidence=f"operator {decision} this call",
                        )
                    )

    # ── run-history (run_summary) failures ──────────────────────────────────
    hist_failures = 0
    hist_total = 0
    for rec in history:
        if since_iso and str(rec.get("ts") or "") and str(rec["ts"]) < since_iso:
            continue
        runnable = str(rec.get("runnable") or "")
        if not runnable:
            continue
        hist_total += 1
        pr = _pr(runnable)
        pr["total_runs_history"] += 1
        code = rec.get("exitCode")
        failed = code not in (0, None, 130)  # 130 = cancelled, not a failure
        if failed:
            hist_failures += 1
            pr["failures"] += 1
            incidents.append(
                _incident(
                    "",
                    -1,
                    CAT_RUN_FAILURE,
                    tool="",
                    runnable=runnable,
                    evidence=_history_failure_evidence(rec),
                    run_id=str(rec.get("runId") or ""),
                    host=str(rec.get("host") or ""),
                    ts=str(rec.get("ts") or ""),
                )
            )

    metrics["history_runs"] = hist_total
    metrics["history_failures"] = hist_failures

    # ── catalog description quality (optional; needs the runnable catalog) ────
    desc_issues = catalog_description_issues(runnables or [])
    metrics["catalog_desc_issues"] = len(desc_issues)
    for issue in desc_issues:
        kind = issue["kind"]
        if kind == "duplicate":
            shared = ", ".join(issue.get("shared_with") or [])
            evidence = (
                f"first description sentence duplicates: {shared} "
                f'("{issue["first_sentence"]}")'
            )
        elif kind == "empty":
            evidence = "no description — blank catalog-index entry"
        else:
            evidence = (
                f"first description sentence is only "
                f"{len(issue['first_sentence'].strip())} chars: "
                f'"{issue["first_sentence"]}"'
            )
        incidents.append(
            _incident(
                "",
                -1,
                CAT_CATALOG_DESC,
                runnable=issue["runnable"],
                evidence=evidence,
            )
        )

    # Prioritise (severity, then keep chronological within a category) and cap.
    incidents.sort(key=lambda i: -_CAT_WEIGHT.get(i.get("category", ""), 0))
    total_incidents = len(incidents)
    incidents = incidents[: max(0, int(max_incidents))]

    scores = _derive_scores(metrics)
    runnable_rows = _runnable_rows(per_runnable)

    return {
        "kind": "problem_report" if comment.strip() else "evaluation",
        "product_name": product_name,
        "developer_emails": list(developer_emails or []),
        "comment": comment,
        "window_days": window_days,
        "since_iso": since_iso or "",
        "metrics": metrics,
        "scores": scores,
        "bounce_kinds": bounce_kinds,
        "autonomy_by_tool": autonomy_by_tool,
        "catalog_description_issues": desc_issues,
        "runnables": runnable_rows,
        "incidents": incidents,
        "incident_total": total_incidents,
        "incident_shown": len(incidents),
    }


# ── helpers ──────────────────────────────────────────────────────────────────
def _incident(
    conv_id: str,
    turn_index: int,
    category: str,
    *,
    tool: str = "",
    runnable: str = "",
    bounce_kind: str = "",
    evidence: str = "",
    run_id: str = "",
    host: str = "",
    ts: str = "",
) -> dict[str, Any]:
    inc: dict[str, Any] = {
        "conversation_id": conv_id,
        "turn_index": turn_index,
        "category": category,
        "evidence": _snippet(evidence),
    }
    if tool:
        inc["tool"] = tool
    if runnable:
        inc["runnable"] = runnable
    if bounce_kind:
        inc["bounce_kind"] = bounce_kind
    if run_id:
        inc["run_id"] = run_id
    if host:
        inc["host"] = host
    if ts:
        inc["ts"] = ts
    return inc


def _stop_snippet(assistant: str) -> str:
    m = _STOP_RE.search(assistant)
    return _snippet(m.group(0) if m else "paused")


def _error_snippet(assistant: str) -> str:
    m = _DURABLE_ERROR_RE.search(assistant)
    if not m:
        return _snippet(assistant)
    return _snippet(assistant[m.start() :])


def _history_failure_evidence(rec: dict[str, Any]) -> str:
    code = rec.get("exitCode")
    exc = rec.get("exception")
    if isinstance(exc, dict) and (exc.get("type") or exc.get("message")):
        return _snippet(
            f"exit {code} — {exc.get('type', '')}: {exc.get('message', '')}"
        )
    return f"exit code {code}"


def _pct(num: int, den: int) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def _derive_scores(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "arg_bounce_rate_pct": _pct(m["arg_bounces"], m["run_attempts"]),
        "search_before_run_pct": _pct(m["runs_after_search"], m["runs"]),
        "empty_search_rate_pct": _pct(m["empty_searches"], m["searches"]),
        "stop_rate_pct": _pct(m["stops"], m["turns"]),
        "recovery_rate_pct": _pct(m["recoveries"], m["turns"]),
        "durable_error_rate_pct": _pct(m["durable_errors"], m["turns"]),
        "history_failure_rate_pct": _pct(
            m.get("history_failures", 0), m.get("history_runs", 0)
        ),
    }


def _runnable_rows(per_runnable: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in per_runnable.values():
        r = dict(r)
        r["arg_bounce_rate_pct"] = _pct(r["arg_bounces"], r["runs"])
        r["failure_rate_pct"] = _pct(r["failures"], r["total_runs_history"])
        rows.append(r)
    # Worst first: those with bounces or failures rise to the top.
    rows.sort(
        key=lambda r: (r["arg_bounces"] + r["failures"], r["runs"]),
        reverse=True,
    )
    return rows


# ── prompt + rendering (pure; the bridge feeds these to the adapter / mailto) ─
_SYNTH_CATEGORIES = (
    "agent_prompt",
    "runnable_description",
    "runnable_args",
    "tool_visibility",
    "tool_mode_config",
    "runnable_bug",
)


def build_synthesis_prompt(report: dict[str, Any]) -> str:
    """A deterministic prompt for the LLM-judge pass. Pure — the bridge sends it
    to ``adapter.chat`` and parses the JSON reply. Asks for ranked, actionable
    recommendations addressed to the developers named in the report, and (for a
    problem-report) a correlation of the operator's comment against the evidence."""
    devs = ", ".join(report.get("developer_emails") or []) or "the maintainers"
    product = report.get("product_name") or "runspec-console"
    comment = str(report.get("comment") or "").strip()
    # Trim the payload the model sees: metrics + worst runnables + top incidents.
    payload = {
        "metrics": report.get("metrics", {}),
        "scores": report.get("scores", {}),
        "bounce_kinds": report.get("bounce_kinds", {}),
        "autonomy_by_tool": report.get("autonomy_by_tool", {}),
        "worst_runnables": (report.get("runnables") or [])[:15],
        "incidents": (report.get("incidents") or [])[:30],
    }
    lines = [
        f"You are reviewing the effectiveness of {product}'s agent, its runnables,",
        f"and the app itself, on behalf of {devs} (and their coding agent). The data",
        "below was mined from real chat transcripts and run history: tool-search",
        "behaviour, argument-validation bounces, turns that had to be stopped and",
        "given guidance, errors, and operator denials/edits of proposed actions.",
        "",
    ]
    if comment:
        lines += [
            "A user filed this problem report — correlate it against the evidence:",
            f"  “{comment}”",
            "",
        ]
    lines += [
        "Return ONLY a JSON object of this shape (no prose outside it):",
        "{",
        '  "summary": "2-3 sentence health summary",',
        '  "correlation": "how the user comment maps to the data, or empty string",',
        '  "recommendations": [',
        '    {"category": "<one of: ' + " | ".join(_SYNTH_CATEGORIES) + '>",',
        '     "target": "the runnable / arg / config key affected",',
        '     "why": "the evidence, citing the numbers",',
        '     "fix": "a concrete change the developer or their code agent can make",',
        '     "confidence": "high|medium|low"}',
        "  ]",
        "}",
        "Rank recommendations most-impactful first. Be specific and terse.",
        "",
        "DATA:",
        json.dumps(payload, indent=2, default=str),
    ]
    return "\n".join(lines)


def parse_synthesis(text: str) -> dict[str, Any]:
    """Parse the model's JSON reply, tolerating code fences / surrounding prose.
    Returns ``{}`` when nothing parseable is found (the bridge then reports the
    deterministic findings alone — synthesis is best-effort)."""
    raw = str(text or "").strip()
    if not raw:
        return {}
    # Strip a ```json fence if present.
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def build_learnings_snippet(
    report: dict[str, Any], synthesis: dict[str, Any] | None = None
) -> str:
    """A portable ``[[learning]]`` TOML block the developer can drop into their
    Config Sync repo / config seed on their own terms (the console never writes it
    back). Built from the LLM recommendations when present, else from the top
    deterministic findings."""
    recs = (synthesis or {}).get("recommendations") or []
    blocks: list[str] = []
    ts = report.get("since_iso") or ""
    if recs:
        for r in recs[:10]:
            blocks.append(
                "[[learning]]\n"
                f"category = {json.dumps(str(r.get('category', '')))}\n"
                f"target = {json.dumps(str(r.get('target', '')))}\n"
                f"why = {json.dumps(str(r.get('why', '')))}\n"
                f"fix = {json.dumps(str(r.get('fix', '')))}\n"
                f"confidence = {json.dumps(str(r.get('confidence', '')))}\n"
                f"since = {json.dumps(ts)}\n"
            )
    else:
        for row in (report.get("runnables") or [])[:10]:
            if not (row.get("arg_bounces") or row.get("failures")):
                continue
            why = (
                f"{row['arg_bounces']} arg bounce(s) over {row['runs']} run(s); "
                f"{row['failures']} failure(s) over {row['total_runs_history']} "
                "history run(s)"
            )
            blocks.append(
                "[[learning]]\n"
                'category = "runnable_args"\n'
                f"target = {json.dumps(str(row.get('runnable', '')))}\n"
                f"why = {json.dumps(why)}\n"
                'fix = ""\n'
                'confidence = "low"\n'
                f"since = {json.dumps(ts)}\n"
            )
    return "\n".join(blocks)


def render_markdown(
    report: dict[str, Any], synthesis: dict[str, Any] | None = None
) -> str:
    """Human-readable report body — used for the workspace file and the mailto
    body. Pure string assembly."""
    m = report.get("metrics", {})
    s = report.get("scores", {})
    devs = (
        ", ".join(report.get("developer_emails") or []) or "(no developers configured)"
    )
    product = report.get("product_name") or "runspec-console"
    kind = "Problem report" if report.get("kind") == "problem_report" else "Evaluation"
    out = [
        f"# {product} — Improve {kind}",
        "",
        f"To: {devs}",
        f"Window: last {report.get('window_days')} day(s) (since {report.get('since_iso') or 'n/a'})",
        "",
    ]
    if report.get("comment"):
        out += ["## Reported problem", "", f"> {report['comment']}", ""]
    if synthesis and synthesis.get("summary"):
        out += ["## Summary", "", str(synthesis["summary"]), ""]
        if synthesis.get("correlation"):
            out += ["**Correlation:** " + str(synthesis["correlation"]), ""]
    out += [
        "## Headline metrics",
        "",
        f"- Turns analysed: {m.get('turns', 0)} across {m.get('conversations', 0)} conversation(s)",
        f"- Arg-bounce rate: {s.get('arg_bounce_rate_pct', 0)}% ({m.get('arg_bounces', 0)}/{m.get('run_attempts', 0)} run attempts)",
        f"- Search-before-run: {s.get('search_before_run_pct', 0)}% | empty searches: {s.get('empty_search_rate_pct', 0)}%",
        f"- Stops (paused): {m.get('stops', 0)} | recoveries: {m.get('recoveries', 0)} | durable errors: {m.get('durable_errors', 0)}",
        f"- Autonomy: {m.get('autonomy_denied', 0)} denied, {m.get('autonomy_edited', 0)} edited",
        f"- Run-history failure rate: {s.get('history_failure_rate_pct', 0)}% ({m.get('history_failures', 0)}/{m.get('history_runs', 0)})",
        f"- Weak catalog descriptions: {m.get('catalog_desc_issues', 0)}",
        "",
    ]
    recs = (synthesis or {}).get("recommendations") or []
    if recs:
        out += ["## Recommendations", ""]
        for i, r in enumerate(recs, 1):
            out.append(
                f"{i}. **[{r.get('category', '?')}] {r.get('target', '')}** "
                f"({r.get('confidence', '?')}) — {r.get('why', '')} → {r.get('fix', '')}"
            )
        out.append("")
    worst = [
        r
        for r in (report.get("runnables") or [])
        if r.get("arg_bounces") or r.get("failures")
    ]
    if worst:
        out += ["## Runnables needing attention", ""]
        for r in worst[:15]:
            out.append(
                f"- `{r.get('runnable')}` — {r.get('arg_bounce_rate_pct', 0)}% arg-bounce "
                f"({r.get('arg_bounces')}/{r.get('runs')}), "
                f"{r.get('failure_rate_pct', 0)}% failures "
                f"({r.get('failures')}/{r.get('total_runs_history')})"
            )
        out.append("")
    desc_issues = report.get("catalog_description_issues") or []
    if desc_issues:
        out += ["## Catalog descriptions needing attention", ""]
        for issue in desc_issues[:20]:
            if issue.get("kind") == "duplicate":
                detail = "duplicate of " + ", ".join(issue.get("shared_with") or [])
            elif issue.get("kind") == "empty":
                detail = "no description"
            else:
                detail = f"too short: “{issue.get('first_sentence', '')}”"
            out.append(f"- `{issue.get('runnable')}` — {detail}")
        out.append("")
    return "\n".join(out)


def mailto_url(report: dict[str, Any], body: str, max_body: int = 1800) -> str:
    """Build a ``mailto:`` URL to the configured developers with the report as the
    body. Bounded (mail clients cap URL length) — the full report lives in the
    workspace file the operator can attach. Pure."""
    from urllib.parse import quote

    to = ",".join(report.get("developer_emails") or [])
    product = report.get("product_name") or "runspec-console"
    kind = "problem report" if report.get("kind") == "problem_report" else "evaluation"
    subject = f"{product} — Improve {kind}"
    trimmed = body if len(body) <= max_body else body[: max_body - 1] + "…"
    return f"mailto:{quote(to)}?subject={quote(subject)}&body={quote(trimmed)}"
