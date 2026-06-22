"""runner.py — pure logic for driving the Claude Code CLI (`claude`) headlessly.

No ``runspec`` dependency lives here: every function takes plain parameters and
either returns plain data or raises a typed error. The thin CLI wrappers in
:mod:`runspec_claude.cli` read args with ``runspec`` and call into here, so the
same logic is importable by a private wrapper package that wants to bake in
corporate defaults (a fixed model, allowlist, working directory, …).

The headless contract is ``claude -p "<prompt>" --output-format json``; we parse
the final JSON result object and normalise the fields we surface.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

__all__ = [
    "ClaudeError",
    "ClaudeNotFoundError",
    "run_claude",
    "list_sessions",
    "encode_project_dir",
]

# Keys we lift out of the `--output-format json` result object. The CLI emits
# more than this; we surface the stable, useful subset and keep the rest under
# "raw" so nothing is silently lost.
_RESULT_KEYS = ("result", "session_id", "is_error", "num_turns", "total_cost_usd", "duration_ms", "subtype")


class ClaudeError(Exception):
    """The ``claude`` CLI ran but failed, or its output could not be parsed."""


class ClaudeNotFoundError(ClaudeError):
    """The ``claude`` binary is not on PATH."""


def _build_command(
    *,
    prompt: str | None,
    mode: str,
    session: str | None,
    model: str | None,
    add_dir: str | None,
    permission_mode: str | None,
    allowed_tools: str | None,
    max_turns: int | None,
    append_system_prompt: str | None,
    bare: bool,
    claude_bin: str,
) -> list[str]:
    """Assemble the argv for a headless ``claude`` invocation.

    ``mode`` is one of ``"ask"`` (fresh run), ``"continue"`` (most recent
    session in cwd) or ``"resume"`` (``session`` required).
    """
    cmd: list[str] = [claude_bin, "-p"]
    if prompt is not None:
        cmd.append(prompt)
    cmd += ["--output-format", "json"]

    if mode == "continue":
        cmd.append("--continue")
    elif mode == "resume":
        if not session:
            raise ClaudeError("resume requires a session id")
        cmd += ["--resume", session]

    if model:
        cmd += ["--model", model]
    if add_dir:
        cmd += ["--add-dir", add_dir]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    # max_turns of 0 (or None) means "leave it uncapped".
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    if append_system_prompt:
        cmd += ["--append-system-prompt", append_system_prompt]
    if bare:
        cmd.append("--bare")
    return cmd


def _normalise_result(stdout: str) -> dict[str, Any]:
    """Parse the `--output-format json` result object into our surfaced shape."""
    text = stdout.strip()
    if not text:
        raise ClaudeError("claude produced no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # stream-json or an unexpected format: take the last non-empty JSON line.
        payload = None
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if payload is None:
            raise ClaudeError(f"could not parse claude output as JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ClaudeError("unexpected claude output (not a JSON object)")

    out: dict[str, Any] = {k: payload[k] for k in _RESULT_KEYS if k in payload}
    out["raw"] = payload
    return out


def run_claude(
    prompt: str | None,
    *,
    mode: str = "ask",
    session: str | None = None,
    model: str | None = None,
    cwd: str | None = None,
    add_dir: str | None = None,
    permission_mode: str | None = "default",
    allowed_tools: str | None = None,
    max_turns: int | None = None,
    append_system_prompt: str | None = None,
    bare: bool = False,
    claude_bin: str = "claude",
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run a headless ``claude`` turn and return the parsed result.

    Returns a dict with (when present) ``result``, ``session_id``, ``is_error``,
    ``num_turns``, ``total_cost_usd``, ``duration_ms`` and the full ``raw``
    payload. ``is_error`` reflects the CLI's own flag — callers decide how to map
    it to an exit code.

    Raises :class:`ClaudeNotFoundError` if the binary is missing and
    :class:`ClaudeError` on a non-zero exit with unparseable output, a timeout,
    or output that is not JSON.
    """
    cmd = _build_command(
        prompt=prompt,
        mode=mode,
        session=session,
        model=model,
        add_dir=add_dir,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        append_system_prompt=append_system_prompt,
        bare=bare,
        claude_bin=claude_bin,
    )
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ClaudeNotFoundError(f"the '{claude_bin}' CLI was not found on PATH — install Claude Code on this host") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeError(f"claude timed out after {timeout}s") from exc

    # The CLI usually still prints a JSON result (with is_error=true) on failure;
    # prefer that over the raw exit code so the caller gets a structured answer.
    if proc.stdout.strip():
        return _normalise_result(proc.stdout)

    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"claude exited with status {proc.returncode}"
        raise ClaudeError(detail)
    raise ClaudeError("claude produced no output")


def encode_project_dir(path: str | os.PathLike[str]) -> str:
    """Encode a working directory the way Claude Code names its transcript dir.

    The project subdirectory under ``~/.claude/projects`` is the absolute path
    with every non-alphanumeric character replaced by ``-``.
    """
    absolute = os.path.abspath(os.fspath(path))
    return re.sub(r"[^a-zA-Z0-9]", "-", absolute)


def _transcripts_root(config_dir: str | os.PathLike[str] | None) -> Path:
    """Resolve the ``projects`` root, honouring ``CLAUDE_CONFIG_DIR``."""
    if config_dir is not None:
        base = Path(config_dir)
    else:
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        base = Path(env) if env else Path.home() / ".claude"
    return base / "projects"


def _sniff_title(transcript: Path) -> str | None:
    """Best-effort human-readable title: the first user message text.

    The JSONL line schema is undocumented, so this is defensive — it returns
    ``None`` rather than raising on anything unexpected.
    """
    try:
        with transcript.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "user":
                    continue
                message = obj.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                text = _content_text(content)
                if text:
                    return text[:120]
    except OSError:
        return None
    return None


def _content_text(content: Any) -> str | None:
    """Pull plain text out of a message ``content`` (string or content blocks)."""
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", "")).strip()
                if text:
                    return text
    return None


def list_sessions(
    *,
    cwd: str | None = None,
    config_dir: str | os.PathLike[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List recent Claude Code sessions for ``cwd`` (default: current directory).

    Reads ``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` (an
    undocumented but stable layout) and returns the most-recently-modified
    sessions first: ``{"session_id", "modified", "title", "path"}``. Returns an
    empty list when the directory does not exist. ``title`` may be ``None``.
    """
    project_dir = _transcripts_root(config_dir) / encode_project_dir(cwd or os.getcwd())
    if not project_dir.is_dir():
        return []

    transcripts = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    sessions: list[dict[str, Any]] = []
    for transcript in transcripts[: max(limit, 0)]:
        sessions.append(
            {
                "session_id": transcript.stem,
                "modified": transcript.stat().st_mtime,
                "title": _sniff_title(transcript),
                "path": str(transcript),
            }
        )
    return sessions
