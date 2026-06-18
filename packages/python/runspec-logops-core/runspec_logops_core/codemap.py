"""Map a stack trace (or a digest signature) to the few relevant source snippets.

Given a trace and the local git checkout, ``map_trace_to_sources`` extracts the
referenced frames, resolves each to a file in the checkout, and returns only the
±context window around each frame's line — capped at ``max_files`` frames and
``max_total_lines`` total. The agent gets the handful of lines that matter, never
the repo. Resolution is plain filesystem (``os.walk``); no ``git`` is required.
"""

from __future__ import annotations

import os
import re

from runspec_logops_core.errors import SourceNotFoundError

_IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".tox", "dist", "build", ".idea"}

# Frame shapes across runtimes.
_PY = re.compile(r'File\s+"([^"]+)",\s+line\s+(\d+),\s+in\s+(\S+)')
_JAVA = re.compile(r"at\s+([\w.$]+)\(([^()\s:]+):(\d+)\)")
_JS = re.compile(r"at\s+(?:([\w.$<>]+)\s+)?\(?((?:/|\./|\w:|[\w.\-]+/)[\w./\-]*\.[a-zA-Z]+):(\d+):\d+\)?")
_GENERIC = re.compile(r"\b([\w./\-]+\.[a-zA-Z]{1,5}):(\d+)\b")
_CAPWORD = re.compile(r"^[A-Z]\w+$")


def _frames_from_trace(trace: str) -> list[tuple[str | None, int | None, str | None]]:
    """Extract ``(file_hint, lineno, symbol)`` candidates from a raw trace."""
    out: list[tuple[str | None, int | None, str | None]] = []
    seen: set[tuple] = set()

    def add(file_hint: str | None, lineno: int | None, symbol: str | None) -> None:
        # Dedup by (basename, line) when a line is known — so the generic
        # ``file:line`` catch-all doesn't re-add a frame a typed regex already
        # captured with its symbol. Symbol-only frames key on the symbol instead.
        key = ("L", os.path.basename(file_hint) if file_hint else None, lineno) if lineno is not None else ("S", file_hint, symbol)
        if key not in seen:
            seen.add(key)
            out.append((file_hint, lineno, symbol))

    for m in _PY.finditer(trace):
        add(m.group(1), int(m.group(2)), m.group(3))
    for m in _JAVA.finditer(trace):
        add(m.group(2), int(m.group(3)), m.group(1))
    for m in _JS.finditer(trace):
        add(m.group(2), int(m.group(3)), m.group(1))
    for m in _GENERIC.finditer(trace):
        add(m.group(1), int(m.group(2)), None)
    return out


def _frames_from_signature(trace: str) -> list[tuple[str | None, int | None, str | None]]:
    """Extract frames from a digest signature like ``Exc@com.acme.OrderSvc.price``."""
    if "@" not in trace:
        return []
    out: list[tuple[str | None, int | None, str | None]] = []
    for frame in trace.split("@", 1)[1].split(">"):
        frame = frame.strip()
        if not frame:
            continue
        parts = frame.split(".")
        cls = next((p for p in reversed(parts) if _CAPWORD.match(p)), None)
        method = parts[-1] if parts else None
        out.append((cls, None, method))
    return out


def _find_file(repo_root: str, file_hint: str) -> str | None:
    """Resolve ``file_hint`` to a path under ``repo_root`` (exact rel path, then basename)."""
    exact = os.path.join(repo_root, file_hint)
    if os.path.isfile(exact):
        return exact
    target = os.path.basename(file_hint)
    for dirpath, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        if target in files:
            return os.path.join(dirpath, target)
    return None


def _find_class_file(repo_root: str, cls: str) -> str | None:
    """Find a file whose stem equals the class name (e.g. ``OrderSvc`` → OrderSvc.java)."""
    for dirpath, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for name in files:
            if os.path.splitext(name)[0] == cls:
                return os.path.join(dirpath, name)
    return None


def _symbol_line(lines: list[str], symbol: str) -> int | None:
    """Return the 1-based line number where ``symbol`` is defined/first referenced."""
    needles = (f"def {symbol}", f"{symbol}(", f" {symbol} ", f".{symbol}")
    for i, line in enumerate(lines, start=1):
        if any(n in line for n in needles):
            return i
    return None


def _snippet(path: str, lineno: int, context: int) -> list[str]:
    """Return ``lineno`` ±context as ``"<n>: <text>"`` strings (no trailing newline)."""
    with open(path, errors="replace") as fh:
        lines = fh.read().splitlines()
    start = max(0, lineno - context - 1)
    end = min(len(lines), lineno + context)
    return [f"{i + 1}: {lines[i]}" for i in range(start, end)]


def map_trace_to_sources(
    repo_root: str,
    trace: str,
    *,
    context: int = 8,
    max_files: int = 5,
    max_total_lines: int = 200,
) -> dict:
    """Resolve the frames in ``trace`` to source snippets in ``repo_root``.

    Raises :class:`SourceNotFoundError` if ``repo_root`` is not a directory.
    """
    if not os.path.isdir(repo_root):
        raise SourceNotFoundError(f"repo checkout not found: {repo_root}")

    candidates = _frames_from_trace(trace) or _frames_from_signature(trace)

    frames: list[dict] = []
    total_lines = 0
    for file_hint, lineno, symbol in candidates:
        if len(frames) >= max_files or total_lines >= max_total_lines:
            break

        path: str | None = None
        if file_hint and ("/" in file_hint or "." in file_hint and lineno is not None):
            path = _find_file(repo_root, file_hint)
        if path is None and file_hint:
            path = _find_class_file(repo_root, os.path.splitext(os.path.basename(file_hint))[0])
        if path is None:
            continue

        resolved_line = lineno
        if resolved_line is None and symbol:
            with open(path, errors="replace") as fh:
                resolved_line = _symbol_line(fh.read().splitlines(), symbol)
        if resolved_line is None:
            continue

        snippet = _snippet(path, resolved_line, context)
        room = max(0, max_total_lines - total_lines)
        snippet = snippet[:room]
        total_lines += len(snippet)
        frames.append({"file": os.path.relpath(path, repo_root), "lineno": resolved_line, "symbol": symbol, "snippet": snippet})

    return {
        "repo": repo_root,
        "frames": frames,
        "files_matched": len(frames),
        "truncated": len(frames) < len(candidates),
    }
