"""Who is handling a failing job — the shared "I'm on it" / "dealt with" marker.

A person claims a failed run (``handling``), says it was dealt with (``done``,
with a note) or lets the claim go (``released``). The notes live in the venv's
runs index next to the runs (``run_notes`` table, see :mod:`runspec.runsdb`), so
everyone reading that venv — every console, ``runspec history`` on the host,
``runspec-scheduler status`` — sees the same thing.

A note covers the job's **failure streak**: the failed runs since its last
success. So one claim covers a job that keeps failing for the same reason, and
the next successful run ends it. A note on a run from before that success no
longer applies. This module is the pure part: given a job's runs (newest first,
each carrying its latest note under ``note``), say where the job stands.
"""

from __future__ import annotations

from typing import Any

STATES = ("handling", "done", "released")
VERBS = {"claim": "handling", "resolve": "done", "release": "released"}
FAILED = frozenset({"failed", "crashed"})
# Neither breaks a streak nor joins it: a run in flight, or a fire that was skipped.
NEUTRAL = frozenset({"running", "skipped"})

MAX_NOTE = 500


def job_key(row: dict[str, Any]) -> str:
    """The job a run belongs to: its ``runspec exec --label`` (``sched:<id>`` for a
    scheduled fire), else its runnable."""
    return str(row.get("label") or row.get("runnable") or "")


def failure_streak(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The job's failed runs since its last success, newest first. ``rows`` must be
    one job's runs, newest first. Empty when the newest finished run succeeded."""
    streak: list[dict[str, Any]] = []
    for r in rows:
        status = r.get("status")
        if status in NEUTRAL:
            continue
        if status in FAILED:
            streak.append(r)
            continue
        break  # completed — the streak (if any) starts after it
    return streak


def job_state(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Where one job stands, from its runs (newest first, each with its latest
    ``note`` or none):

    * ``clean`` — its newest finished run succeeded (or it has none);
    * ``failed`` — failing, and nobody has claimed it (or the claim was released);
    * ``handling`` — someone said they are on it;
    * ``done`` — someone said it was dealt with (counts as clean until it next
      succeeds or someone claims it again).

    Returns ``{state, failures, since, run_id, note}``: how many runs in the
    streak, when it started, the newest failed run (the one to claim), and the
    newest note in the streak (``None`` for clean / unclaimed)."""
    streak = failure_streak(rows)
    if not streak:
        return {"state": "clean", "failures": 0, "since": None, "run_id": None, "note": None}
    notes = [r["note"] for r in streak if isinstance(r.get("note"), dict)]
    latest = max(notes, key=lambda n: str(n.get("ts") or ""), default=None)
    state = "failed"
    if latest is not None and latest.get("state") in ("handling", "done"):
        state = str(latest["state"])
    return {
        "state": state,
        "failures": len(streak),
        "since": streak[-1].get("start_ts"),
        "run_id": streak[0].get("run_id"),
        "note": latest if state != "failed" else None,
    }


def jobs(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """:func:`job_state` for every job in ``rows`` (any order), keyed by :func:`job_key`."""
    by_job: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(rows, key=lambda r: str(r.get("start_ts") or ""), reverse=True):
        by_job.setdefault(job_key(r), []).append(r)
    return {k: job_state(v) for k, v in by_job.items()}


def clean_note(text: str | None) -> str:
    """A note as stored: one line of at most :data:`MAX_NOTE` characters."""
    out = " ".join(str(text or "").split())
    return out[:MAX_NOTE]
