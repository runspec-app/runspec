"""runspec-claude — drive the Claude Code CLI (`claude`) headlessly from runspec.

Public API (importable for a private wrapper package that bakes in defaults):

    from runspec_claude import run_claude, list_sessions, ClaudeError
"""

from __future__ import annotations

from .runner import (
    ClaudeError,
    ClaudeNotFoundError,
    encode_project_dir,
    list_sessions,
    run_claude,
)

__all__ = [
    "run_claude",
    "list_sessions",
    "encode_project_dir",
    "ClaudeError",
    "ClaudeNotFoundError",
]
