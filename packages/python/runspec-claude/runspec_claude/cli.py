"""cli.py — the `claude-run` entry point.

One binary, four verbs. ``rs.parse("claude-run")`` resolves the subcommand and
the (globals + per-command) args; we dispatch on ``spec.runspec_command`` and
delegate the actual work to :mod:`runspec_claude.runner`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

import runspec as rs

from .runner import ClaudeError, ClaudeNotFoundError, list_sessions, run_claude

logger = logging.getLogger(__name__)


def _opt(arg: Any) -> Any:
    """Underlying value of a runspec ``Arg`` (or the value as-is); '' -> None."""
    value = getattr(arg, "value", arg)
    if isinstance(value, str) and not value:
        return None
    return value


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def _run_turn(spec: rs.RunSpec, *, mode: str, session: str | None) -> None:
    """Shared body for ask / continue / resume."""
    result = run_claude(
        str(spec.prompt.value),
        mode=mode,
        session=session,
        model=_opt(spec.model),
        cwd=_opt(spec.dir),
        add_dir=_opt(spec.add_dir),
        permission_mode=_opt(spec.permission_mode),
        allowed_tools=_opt(spec.allowed_tools),
        max_turns=_opt(spec.max_turns),
        append_system_prompt=_opt(spec.append_system_prompt),
        bare=bool(spec.bare.value),
    )
    if result.get("session_id"):
        logger.info("session %s (%s turns)", result["session_id"], result.get("num_turns", "?"))
    _emit(result)
    if result.get("is_error"):
        sys.exit(1)


def main() -> None:
    spec = rs.parse("claude-run")
    command = spec.runspec_command

    try:
        if command == "ask":
            _run_turn(spec, mode="ask", session=None)
        elif command == "continue":
            _run_turn(spec, mode="continue", session=None)
        elif command == "resume":
            _run_turn(spec, mode="resume", session=str(spec.session.value))
        elif command == "sessions":
            sessions = list_sessions(cwd=_opt(spec.dir), limit=int(spec.limit.value))
            for s in sessions:
                # Render the mtime as ISO for human/agent consumption.
                s["modified"] = datetime.fromtimestamp(s["modified"], tz=timezone.utc).isoformat()
            _emit({"sessions": sessions, "count": len(sessions)})
        else:  # pragma: no cover - require-command makes this unreachable
            _emit({"error": "no command — try: ask, continue, resume, sessions"})
            sys.exit(2)
    except ClaudeNotFoundError as exc:
        _emit({"error": str(exc), "kind": "claude-not-found"})
        sys.exit(127)
    except ClaudeError as exc:
        _emit({"error": str(exc)})
        sys.exit(1)
