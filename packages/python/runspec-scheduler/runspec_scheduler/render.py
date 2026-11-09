"""render.py — readable text for a person at a terminal.

Every ``runspec-scheduler`` verb prints JSON for the console, scripts and pipes.
When a person runs one in an interactive terminal it prints text instead: what
happened in a sentence or two, and the schedules as a table. ``--json`` keeps
JSON at a terminal too.

Each function takes the verb's JSON payload (unchanged) and returns the text.
They are pure — ``now`` is a parameter — so tests can pin the clock.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

#: Longest cell in a table; longer text is cut with "…".
MAX_CELL = 48

#: Lines of a failing schedule's last output shown under "Needs attention".
OUTPUT_LINES = 6


# ── small helpers ─────────────────────────────────────────────────────────────


def _now(now: _dt.datetime | None) -> _dt.datetime:
    return now if now is not None else _dt.datetime.now().astimezone()


def parse_ts(value: Any) -> _dt.datetime | None:
    """An ISO timestamp as an aware datetime (naive ones are taken as local)."""
    if not value:
        return None
    try:
        moment = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.astimezone()


def span(seconds: float) -> str:
    """A length of time in at most two units: ``45s``, ``3m 20s``, ``6h``, ``2d 4h``."""
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total}s"
    parts: list[str] = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if total >= size:
            amount, total = divmod(total, size)
            parts.append(f"{amount}{unit}")
        if len(parts) == 2:
            break
        if parts and total == 0:
            break
    return " ".join(parts)


def duration(seconds: Any, *, zero: str = "no limit") -> str:
    """A configured duration in seconds (``0`` = ``zero``)."""
    if seconds is None:
        return "-"
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    return zero if value == 0 else span(value)


def run_time(ms: Any) -> str:
    """How long a run took, from milliseconds."""
    if ms is None:
        return ""
    try:
        value = float(ms) / 1000.0
    except (TypeError, ValueError):
        return ""
    if value < 10:
        return f"{value:.1f}s"
    return span(value)


def ago(value: Any, now: _dt.datetime | None = None) -> str:
    moment = parse_ts(value)
    if moment is None:
        return ""
    delta = (_now(now) - moment).total_seconds()
    if delta < 5:
        return "just now"
    return f"{span(delta)} ago"


def when_next(value: Any, now: _dt.datetime | None = None) -> str:
    """``14:30 (in 25m)``, ``Tue 02:00 (in 1d 3h)``, in the host's local time."""
    moment = parse_ts(value)
    if moment is None:
        return "-"
    current = _now(now)
    local = moment.astimezone(current.tzinfo)
    delta = (moment - current).total_seconds()
    if delta < 0:
        return local.strftime("%H:%M") + " (due)"
    if local.date() == current.date():
        day = local.strftime("%H:%M")
    elif local.date() == current.date() + _dt.timedelta(days=1):
        day = local.strftime("tomorrow %H:%M")
    else:
        day = local.strftime("%a %d %b %H:%M")
    return f"{day} (in {span(delta)})"


def schedule_when(when: dict[str, Any] | None) -> str:
    when = when or {}
    text = str(when.get("cron") or "")
    if when.get("every"):
        text = f"every {when['every']}"
    zone = str(when.get("timezone") or "")
    if zone and zone.lower() != "local":
        text += f" ({zone})"
    return text or "-"


def rec_when(rec: dict[str, Any]) -> str:
    """A schedule's timing for a person: a package schedule in its package's
    words; one whose time this venv changed shows the package's too."""
    pkg = rec.get("package") or {}
    if pkg and "when" not in (pkg.get("overridden") or []):
        return str(pkg.get("words") or schedule_when(rec.get("when")))
    text = schedule_when(rec.get("when"))
    if pkg:
        text += f" (package says {pkg.get('words')})"
    return text


def _paused_word(rec: dict[str, Any]) -> str:
    return "offered" if rec.get("offered") else "paused"


def _target(rec: dict[str, Any]) -> str:
    return " ".join([str(rec.get("runnable") or ""), *[str(c) for c in rec.get("command") or []]]).strip()


def _cut(text: str, limit: int = MAX_CELL) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Plain, left-aligned columns, two spaces apart. Long cells are cut, except
    in the last column, which can run on."""
    last = len(headers) - 1
    cells = [[str(c) if i == last else _cut(str(c)) for i, c in enumerate(row)] for row in rows]
    widths = [max([len(h), *(len(r[i]) for r in cells)]) for i, h in enumerate(headers)]
    lines = ["  ".join(h.upper().ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines += ["  ".join(r[i].ljust(widths[i]) for i in range(len(headers))).rstrip() for r in cells]
    return lines


def fields(pairs: list[tuple[str, str]]) -> list[str]:
    """``Label  value`` lines with the labels lined up (empty values skipped)."""
    pairs = [(k, v) for k, v in pairs if v]
    if not pairs:
        return []
    width = max(len(k) for k, _ in pairs)
    return [f"{k:<{width}}  {v}" for k, v in pairs]


def _join(*blocks: list[str]) -> str:
    out: list[str] = []
    for block in blocks:
        if not block:
            continue
        if out:
            out.append("")
        out.extend(block)
    return "\n".join(out)


def error(payload: dict[str, Any]) -> str:
    """An error payload as one or two lines (the cli prints these to stderr)."""
    lines = [f"runspec-scheduler: {payload.get('error') or 'failed'}"]
    if payload.get("hint"):
        lines.append(f"  {payload['hint']}")
    return "\n".join(lines)


# ── status ────────────────────────────────────────────────────────────────────


def _last_run(rec: dict[str, Any], now: _dt.datetime | None) -> str:
    runs = rec.get("runs") or []
    if not runs:
        return "never"
    run = runs[0]
    state = str(run.get("status") or "")
    if state == "running":
        began = parse_ts(run.get("start_ts"))
        return f"running ({span((_now(now) - began).total_seconds())})" if began else "running"
    code = run.get("exit_code")
    if state == "skipped":
        word = "skipped"
    elif state == "completed" or code == 0:
        word = "ok"
    else:
        word = "failed" if code in (None, "") else f"failed (exit {code})"
    parts = [word, ago(run.get("start_ts"), now), run_time(run.get("duration_ms"))]
    return ", ".join(p for p in parts if p)


def _schedule_state(rec: dict[str, Any], backend: str) -> str:
    if not rec.get("enabled", True):
        return _paused_word(rec)
    notes: list[str] = []
    if rec.get("installable") is False:
        notes.append(f"won't run: {rec.get('not_installable_reason') or 'not installable'}")
    elif rec.get("installed") is False:
        notes.append("not loaded by the daemon" if backend == "daemon" else "not in crontab (run sync)")
    elif rec.get("line") == "legacy":
        notes.append("old crontab line (run sync)")
    attention = rec.get("attention") or {}
    state = attention.get("state")
    count = attention.get("failures") or 0
    if state == "failed":
        notes.insert(0, "failing" + (f" ({count} runs)" if count > 1 else ""))
    elif state == "handling":
        who = (attention.get("note") or {}).get("actor")
        notes.insert(0, "being handled" + (f" by {who}" if who else ""))
    elif state == "done":
        notes.insert(0, "dealt with")
    return "; ".join(notes) or "ok"


def _daemon_line(payload: dict[str, Any], now: _dt.datetime | None) -> str:
    daemon = payload.get("daemon") or {}
    backend = payload.get("backend") or "cron"
    state = daemon.get("state") or "stopped"
    if state == "stopped":
        if backend != "daemon":
            return ""
        return "not running — start it with: runspec-scheduler restart (or install-service --write)"
    bits = [f"pid {daemon.get('pid')}"]
    if daemon.get("uptime_s") is not None:
        bits.append(f"up {span(daemon['uptime_s'])}")
    if state == "stale":
        age = daemon.get("heartbeat_age_s")
        text = "not responding" + (f" (no heartbeat for {span(age)})" if age is not None else "")
    else:
        text = "running"
    text += f" ({', '.join(bits)})"
    if daemon.get("version"):
        text += f", version {daemon['version']}"
    if backend != "daemon":
        text += " — fires nothing, this venv uses cron"
    return text


def _keeper_line(payload: dict[str, Any]) -> str:
    keepers = payload.get("keeper")
    if keepers is None:
        return ""
    if not keepers:
        return "none (runspec-scheduler install-service)" if payload.get("backend") == "daemon" else ""
    return ", ".join(str(k.get("manager")) for k in keepers)


def _attention_block(recs: list[dict[str, Any]], now: _dt.datetime | None) -> list[str]:
    lines: list[str] = []
    for rec in recs:
        attention = rec.get("attention") or {}
        if attention.get("state") not in ("failed", "handling"):
            continue
        since = ago(attention.get("since"), now)
        head = f"{rec.get('id')} — failing"
        if attention.get("failures"):
            head += f" for {attention['failures']} run{'s' if attention['failures'] != 1 else ''}"
        if since:
            head += f", since {since}"
        lines.append(head)
        note = attention.get("note") or {}
        if note:
            who = note.get("actor") or note.get("os_user") or "someone"
            said = f": {note['note']}" if note.get("note") else ""
            lines.append(f"  {who} is on it{said}")
        out = rec.get("last_output") or {}
        tail: list[str] = []
        for text in (out.get("stderr"), out.get("stdout")):
            if text and not tail:
                tail = str(text).rstrip().splitlines()[-OUTPUT_LINES:]
        tail = tail or [str(e) for e in (out.get("errors") or [])][-OUTPUT_LINES:]
        lines += [f"  | {line}" for line in tail]
        lines.append(f"  claim it: runspec history claim --label sched:{rec.get('id')} --note '…'")
    return lines


def status(payload: dict[str, Any], now: _dt.datetime | None = None) -> str:
    backend = str(payload.get("backend") or "cron")
    settings = payload.get("settings") or {}
    daemon = payload.get("daemon") or {}
    head = fields(
        [
            ("Venv", str(payload.get("venv") or "")),
            ("Backend", backend + (" (crontab lines)" if backend == "cron" else "")),
            ("Daemon", _daemon_line(payload, now)),
            ("Keeper", _keeper_line(payload)),
            (
                "Settings",
                f"timeout {duration(settings.get('timeout'))}, misfire grace {duration(settings.get('misfire_grace'))}, catch-up window {duration(settings.get('catch_up_window'))}"
                if backend == "daemon" and settings
                else "",
            ),
            ("Zone", zone_line(settings, payload.get("host_zone"))),
            ("Packages", _packages_line(payload)),
            ("Config", str(payload.get("config") or "")),
        ]
    )
    warnings: list[str] = []
    pkgs = payload.get("packages") or {}
    for err in pkgs.get("errors") or []:
        where = err.get("id") or err.get("runnable") or "a package"
        warnings.append(f"Package schedule {where} can't be used: {err.get('error')}")
    for orphan in pkgs.get("orphans") or []:
        warnings.append(f"{orphan.get('id')} changes a package schedule that isn't here ({orphan.get('reason')}); drop it with: runspec-scheduler remove --id {orphan.get('id')}")
    if daemon.get("restart_needed"):
        warnings.append("The daemon is running an older version than is installed: runspec-scheduler restart")
    if daemon.get("config_error"):
        warnings.append(f"The daemon couldn't read the config, so it kept the last good one: {daemon['config_error']}")
    running = daemon.get("running") or []
    if running and daemon.get("state") == "running":
        warnings.append(f"Running now: {', '.join(str(r.get('id') if isinstance(r, dict) else r) for r in running)}")
    for err in payload.get("errors") or []:
        warnings.append(str(err))

    recs = payload.get("schedules") or []
    if recs:
        rows = [
            [
                str(rec.get("id")),
                _target(rec),
                rec_when(rec),
                when_next(rec.get("next_run"), now) if rec.get("enabled", True) else "-",
                _last_run(rec, now),
                _schedule_state(rec, backend),
            ]
            for rec in recs
        ]
        body = table(["id", "runnable", "when", "next run", "last run", "state"], rows)
    else:
        body = ["No schedules yet. Add one with: runspec-scheduler add --id … --runnable … --cron '0 2 * * *'"]

    stale = [f"{s.get('id')} ({s.get('user')}): {s.get('reason')}" for s in payload.get("stale") or []]
    stale_block = ["Crontab lines this venv no longer wants (run sync to remove them):", *(f"  {s}" for s in stale)] if stale else []
    attention = _attention_block(recs, now)
    attention_block = ["Needs attention:", *attention] if attention else []
    return _join(head, warnings, body, _offered_block(recs), attention_block, stale_block)


def zone_line(settings: dict[str, Any], host_zone: Any) -> str:
    """Which clock a schedule with no zone of its own fires on."""
    venv = str(settings.get("timezone") or "")
    if venv:
        return f"{venv} (this venv's; the host is on {host_zone})" if host_zone and host_zone != venv else f"{venv} (this venv's)"
    return f"{host_zone} (the host's)" if host_zone else ""


_POLICY_WORDS = {
    "offer": "offered, off until turned on",
    "auto": "on as soon as they're installed",
    "off": "ignored",
}


def _packages_line(payload: dict[str, Any]) -> str:
    pkgs = payload.get("packages") or {}
    policy = str(pkgs.get("policy") or (payload.get("settings") or {}).get("package_schedules") or "")
    has = any((rec.get("origin") == "package") for rec in payload.get("schedules") or [])
    if not policy or (policy == "offer" and not has):
        return ""
    text = f"{policy}: {_POLICY_WORDS.get(policy, policy)}"
    waiting = pkgs.get("offered") or 0
    if waiting:
        text += f" ({waiting} waiting)"
    return text


def _offered_block(recs: list[dict[str, Any]]) -> list[str]:
    offered = [r for r in recs if r.get("offered")]
    if not offered:
        return []
    lines = ["Offered by installed packages (off until you turn them on):"]
    width = max(len(str(r.get("id"))) for r in offered)
    for rec in offered:
        lines.append(f"  {str(rec.get('id')):<{width}}  {rec_when(rec)}" + (f" — {rec['description']}" if rec.get("description") else ""))
    lines.append("  turn one on: runspec-scheduler enable --id <id>   hide it: runspec-scheduler disable --id <id>")
    return lines


# ── the other verbs ───────────────────────────────────────────────────────────


def schedules(payload: dict[str, Any], now: _dt.datetime | None = None) -> str:
    """``list``: the configured schedules."""
    head = fields([("Venv", str(payload.get("venv") or "")), ("Backend", str(payload.get("backend") or "")), ("Config", str(payload.get("config") or ""))])
    recs = payload.get("schedules") or []
    if not recs:
        return _join(head, ["No schedules yet."])
    rows = [
        [
            str(rec.get("id")),
            _target(rec),
            rec_when(rec),
            when_next(rec.get("next_run"), now) if rec.get("enabled", True) else _paused_word(rec),
            str(rec.get("run_as") or ""),
            ", ".join(f"{k}={v}" for k, v in (rec.get("args") or {}).items()),
        ]
        for rec in recs
    ]
    return _join(head, table(["id", "runnable", "when", "next run", "run as", "args"], rows))


def added(payload: dict[str, Any]) -> str:
    target = " ".join([str(payload.get("runnable") or ""), *[str(c) for c in payload.get("command") or []]]).strip()
    lines = [f"Saved schedule {payload.get('id')}: runs {target}" + (f" as {payload['run_as']}" if payload.get("run_as") else "") + "."]
    if payload.get("env_file"):
        lines.append("Its credentials are in a file only that account can read.")
    if payload.get("backend") == "daemon":
        lines.append("The daemon picks it up within a few seconds.")
    else:
        lines.append("Write it into the crontab with: runspec-scheduler sync")
    return "\n".join(lines)


def removed(payload: dict[str, Any]) -> str:
    sid = payload.get("id")
    if payload.get("package") and not payload.get("ok"):
        return f"{sid} comes from the {payload['package']} package and this venv hasn't changed it, so there's nothing to remove. Turn it off with: runspec-scheduler disable --id {sid}"
    if not payload.get("ok"):
        return f"There's no schedule {sid} in this venv."
    lines = [f"Put {sid} back to what the {payload['package']} package says." if payload.get("package") else f"Removed schedule {sid}."]
    if payload.get("backend") == "cron":
        lines.append("Take its line out of the crontab with: runspec-scheduler sync")
    return "\n".join(lines)


def ran(payload: dict[str, Any]) -> str:
    """``run-now`` in the foreground."""
    code = payload.get("exit_code")
    lines = [f"{payload.get('id')} ({payload.get('runnable')}) " + ("finished OK." if code == 0 else f"failed with exit code {code}.")]
    for name in ("stdout", "stderr"):
        text = str(payload.get(name) or "").rstrip()
        if text:
            lines += ["", f"{name}:", *(f"  {line}" for line in text.splitlines())]
    return "\n".join(lines)


def started(payload: dict[str, Any]) -> str:
    """``run-now --detach``."""
    sid = payload.get("id")
    if payload.get("finished"):
        code = payload.get("exit_code")
        verdict = "finished OK" if code == 0 else f"failed with exit code {code}"
        return f"{sid} {verdict} straight away. Its output: runspec-scheduler status"
    return f"Started {sid} in the background as {payload.get('user')} (pid {payload.get('pid')}). Follow it with: runspec-scheduler status"


def synced(payload: dict[str, Any]) -> str:
    dry = bool(payload.get("dry_run"))
    installed = payload.get("installed") or []
    lines: list[str] = []
    if payload.get("backend") == "daemon":
        lines.append("This venv uses the daemon, so it has no crontab lines" + (" (any left behind would be removed)." if dry else " (any left behind were removed)."))
    elif installed:
        verb = "Would write" if dry else "Wrote"
        lines.append(f"{verb} {len(installed)} crontab line{'s' if len(installed) != 1 else ''}:")
        lines += [f"  {i.get('id')}  {i.get('runnable')}  as {i.get('user')}" for i in installed]
    else:
        lines.append("No schedules to put in the crontab." + ("" if dry else " Any old lines were removed."))
    skipped = payload.get("skipped") or []
    if skipped:
        lines += ["", "Left out:", *(f"  {s.get('id')}: {s.get('reason')}" for s in skipped)]
    errors = payload.get("errors") or []
    if errors:
        lines += ["", "Couldn't update:", *(f"  {e.get('user')}: {e.get('error')}" for e in errors)]
    if dry:
        lines += ["", "Nothing was changed (--dry-run)."]
    return "\n".join(lines)


def used(payload: dict[str, Any]) -> str:
    backend = payload.get("backend")
    previous = payload.get("previous")
    lines: list[str] = []
    if not payload.get("ok") and payload.get("error"):
        lines.append(f"runspec-scheduler: {payload['error']}")
    elif payload.get("ok"):
        if backend == previous:
            lines.append(f"This venv already uses {backend}.")
        else:
            lines.append(f"This venv now uses {backend} (was {previous}).")
    crontab = payload.get("crontab") or {}
    if backend == "cron" and crontab.get("installed"):
        lines.append(f"Wrote {len(crontab['installed'])} crontab line(s).")
    for err in crontab.get("errors") or []:
        lines.append(f"Couldn't update {err.get('user')}'s crontab: {err.get('error')}")
    daemon = payload.get("daemon") or {}
    if backend == "daemon" and daemon:
        lines.append(f"The daemon is {daemon.get('state')}.")
    if payload.get("hint"):
        lines.append(str(payload["hint"]).capitalize())
    return "\n".join(lines)


def settings(payload: dict[str, Any]) -> str:
    values = payload.get("settings") or {}
    changed = payload.get("changed") or []
    policy = str(values.get("package_schedules") or "offer")
    lines = fields(
        [
            ("Backend", str(values.get("backend") or "")),
            ("Timeout", duration(values.get("timeout"))),
            ("Misfire grace", duration(values.get("misfire_grace"))),
            ("Catch-up window", duration(values.get("catch_up_window"))),
            ("Zone", zone_line(values, payload.get("host_zone")) or "the host's"),
            ("Package schedules", f"{policy}: {_POLICY_WORDS.get(policy, policy)}"),
            ("Config", str(payload.get("config") or "")),
        ]
    )
    if changed:
        lines += ["", f"Changed {', '.join(c.replace('_', ' ') for c in changed)}. A running daemon picks it up by itself."]
    lines += [f"Warning: {w}" for w in payload.get("warnings") or []]
    if payload.get("hint"):
        lines.append(str(payload["hint"])[:1].upper() + str(payload["hint"])[1:] + ".")
    return "\n".join(lines)


def toggled(payload: dict[str, Any]) -> str:
    """``enable`` / ``disable``."""
    sid = payload.get("id")
    lines = [f"Turned {sid} {'on' if payload.get('enabled') else 'off'}."]
    if payload.get("backend") == "daemon":
        lines.append("The daemon picks it up within a few seconds.")
    else:
        lines.append("Bring the crontab in line with: runspec-scheduler sync")
    return "\n".join(lines)


def plan(payload: dict[str, Any]) -> str:
    """``install-service`` and ``restart``: a keeper plan, before or after it ran."""
    written = bool(payload.get("written"))
    lines: list[str] = []
    head = [
        ("Keeper", str(payload.get("manager") or "")),
        ("Why", str(payload.get("reason") or "")),
    ]
    lines += fields(head)
    done = payload.get("done")
    steps = payload.get("steps") or []
    if done is not None:
        if done:
            lines += ["", "Done:", *(f"  ✓ {s}" for s in done)]
        left = [s for s in steps if s not in done]
        if left and not payload.get("ok"):
            lines += ["", "Not done:", *(f"  - {s}" for s in left)]
    elif steps:
        lines += ["", "Would do:" if not written else "Steps:", *(f"  - {s}" for s in steps)]
    files = [f for f in payload.get("files") or [] if isinstance(f, dict)]
    if files and not written:
        lines += ["", "Files:"]
        for f in files:
            by = f.get("by")
            lines.append(f"  {f.get('path')}" + (f"  (written by {by})" if by else ""))
            if by == "root" and f.get("content"):
                # Root has to write this one by hand, so show what goes in it.
                lines += [f"    {line}" for line in str(f["content"]).rstrip().splitlines()]
    cron = payload.get("crontab") or []
    if cron and not written:
        lines += ["", "Crontab lines:", *(f"  {c}" for c in cron)]
    root = payload.get("root_steps") or []
    if root:
        lines += ["", "For root to do:", *(f"  {r}" for r in root)]
    daemon = payload.get("daemon") or {}
    if daemon.get("state"):
        lines += ["", f"The daemon is {daemon['state']}" + (f" (pid {daemon['pid']})." if daemon.get("pid") else ".")]
    for warning in payload.get("warnings") or []:
        lines += ["", f"Note: {warning}"]
    if payload.get("error"):
        lines += ["", f"Error: {payload['error']}"]
    if payload.get("hint"):
        lines += ["", str(payload["hint"]).capitalize()]
    return "\n".join(lines).strip("\n")
