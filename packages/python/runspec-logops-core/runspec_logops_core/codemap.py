"""Map a stack trace (or a digest signature) to the few relevant source snippets.

Given a trace and the local git checkout, ``map_trace_to_sources`` extracts the
referenced frames, resolves each to a file in the checkout, and returns only the
±context window around each frame's line — capped at ``max_files`` frames and
``max_total_lines`` total. The agent gets the handful of lines that matter, never
the repo. Resolution is plain filesystem (``os.walk``); no ``git`` is required.

Resolution is layered, best-effort: a frame that carries a file + line (a raw
stack) resolves directly; a digest *signature* (class + method, no line) resolves
by the class's file, and failing that by **searching the checkout for the symbol's
definition** — so a Java inner class, a file whose name differs from the class, or
a Python function-only frame still lands on real code.
"""

from __future__ import annotations

import os
import re

from runspec_logops_core.errors import SourceNotFoundError

_IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".tox", "dist", "build", ".idea"}

# Extensions worth scanning for a definition fallback (skip data/binaries so the
# repo-wide search stays cheap and relevant).
_CODE_EXT = {
    ".java",
    ".kt",
    ".scala",
    ".groovy",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".go",
    ".rb",
    ".cs",
    ".php",
    ".rs",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".m",
    ".mm",
    ".swift",
    ".clj",
    ".kts",
}
_MAX_FILE_BYTES = 2_000_000  # don't scan a single huge file during the fallback

# Frame shapes across runtimes.
_PY = re.compile(r'File\s+"([^"]+)",\s+line\s+(\d+),\s+in\s+(\S+)')
_JAVA = re.compile(r"at\s+([\w.$]+)\(([^()\s:]+):(\d+)\)")
_JS = re.compile(r"at\s+(?:([\w.$<>]+)\s+)?\(?((?:/|\./|\w:|[\w.\-]+/)[\w./\-]*\.[a-zA-Z]+):(\d+):\d+\)?")
_GENERIC = re.compile(r"\b([\w./\-]+\.[a-zA-Z]{1,5}):(\d+)\b")
_CAPWORD = re.compile(r"^[A-Z]\w+$")
_IDENT = re.compile(r"^[\w.$]+$")


def _def_patterns(symbol: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Return (strong, methodish) regexes that match a *definition* of ``symbol``.

    ``strong`` matches a keyworded declaration (``def``/``function``/``class``/…);
    ``methodish`` matches a Java/C-style signature — modifiers/return type then
    ``symbol(`` at the start of a line — without matching a bare call like
    ``x.symbol(`` (a ``.`` before the name, or no preceding type token, won't
    match)."""
    s = re.escape(symbol)
    strong = re.compile(rf"\b(?:def|function|func|fn|class|interface|enum|trait|sub)\s+{s}\b")
    methodish = re.compile(rf"^\s*(?:(?:public|private|protected|static|final|synchronized|abstract|native|default|override|open|suspend)\s+)*[\w<>\[\].,?\s]+\s+{s}\s*\(")
    return strong, methodish


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
    """Extract frames from a digest signature like ``Exc@com.acme.OrderSvc.price``.

    The class hint is the right-most Capitalised segment with any inner-class
    suffix dropped (``Outer$Inner`` → ``Outer``, the file that defines it); the
    symbol is the final segment (the method/function)."""
    if "@" not in trace:
        return []
    out: list[tuple[str | None, int | None, str | None]] = []
    for frame in trace.split("@", 1)[1].split(">"):
        frame = frame.strip()
        if not frame:
            continue
        parts = frame.split(".")
        cls = next((p.split("$", 1)[0] for p in reversed(parts) if _CAPWORD.match(p.split("$", 1)[0])), None)
        method = parts[-1] if parts else None
        out.append((cls, None, method))
    return out


def _frames_from_bare(trace: str) -> list[tuple[str | None, int | None, str | None]]:
    """Treat a bare identifier as a symbol/class to locate.

    A summarize-log signature can be just an exception class (``BackoffError``) or a
    dotted name (``pkg.mod.func``) with no ``@`` and no ``file:line``. Resolve it to
    where that symbol is *defined* in the checkout."""
    t = trace.strip()
    if not t or not _IDENT.match(t) or "@" in t:
        return []
    parts = t.split(".")
    cls = next((p.split("$", 1)[0] for p in reversed(parts) if _CAPWORD.match(p.split("$", 1)[0])), None)
    symbol = parts[-1].split("$")[-1] if parts else t
    # A bare ``BackoffError`` is the class itself — search for its definition.
    if cls and symbol == cls:
        return [(cls, None, cls)]
    return [(cls, None, symbol)]


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


def _iter_source_files(repo_root: str):
    """Yield code files under ``repo_root`` (ignored dirs skipped, huge files skipped)."""
    for dirpath, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        for name in files:
            if os.path.splitext(name)[1].lower() not in _CODE_EXT:
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.getsize(path) <= _MAX_FILE_BYTES:
                    yield path
            except OSError:
                continue


def _symbol_line(lines: list[str], symbol: str) -> int | None:
    """1-based line where ``symbol`` is *defined* (preferred), else first referenced."""
    strong, methodish = _def_patterns(symbol)
    fallback: int | None = None
    for i, line in enumerate(lines, start=1):
        if strong.search(line) or methodish.search(line):
            return i
        if fallback is None and (f"{symbol}(" in line or f".{symbol}" in line):
            fallback = i
    return fallback


def _find_definition(repo_root: str, symbol: str, class_hint: str | None) -> tuple[str, int] | None:
    """Search the checkout for ``symbol``'s definition. Best (lowest) rank wins:
    a keyworded def in the class's own file → a keyworded def anywhere → a
    method-style signature in the class file → one anywhere. Returns (path, line)."""
    strong, methodish = _def_patterns(symbol)
    best: tuple[int, str, int] | None = None  # (rank, path, lineno)
    for path in _iter_source_files(repo_root):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        in_class_file = class_hint is not None and os.path.splitext(os.path.basename(path))[0] == class_hint
        for i, line in enumerate(lines, start=1):
            if "import " in line:
                continue
            if strong.search(line):
                rank = 0 if in_class_file else 1
            elif methodish.search(line):
                rank = 2 if in_class_file else 3
            else:
                continue
            if best is None or rank < best[0]:
                best = (rank, path, i)
            if rank == 0:
                return path, i  # can't do better than a keyworded def in the class file
    return (best[1], best[2]) if best else None


def _snippet(path: str, lineno: int, context: int) -> list[str]:
    """Return ``lineno`` ±context as ``"<n>: <text>"`` strings (no trailing newline)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
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

    candidates = _frames_from_trace(trace) or _frames_from_signature(trace) or _frames_from_bare(trace)

    frames: list[dict] = []
    total_lines = 0
    for file_hint, lineno, symbol in candidates:
        if len(frames) >= max_files or total_lines >= max_total_lines:
            break

        # 1) A frame with a real path/filename + line (a raw stack) resolves directly.
        path: str | None = None
        if file_hint and ("/" in file_hint or ("." in file_hint and lineno is not None)):
            path = _find_file(repo_root, file_hint)
        class_hint = os.path.splitext(os.path.basename(file_hint))[0] if file_hint else None
        # 2) Otherwise try the class's own file.
        if path is None and class_hint:
            path = _find_class_file(repo_root, class_hint)

        resolved_line = lineno
        if path is not None and resolved_line is None and symbol:
            with open(path, encoding="utf-8", errors="replace") as fh:
                resolved_line = _symbol_line(fh.read().splitlines(), symbol)

        # 3) Fallback: no file, or the class file didn't contain the symbol — search
        # the checkout for the symbol's definition (handles inner classes, files
        # named differently from the class, and function-only signatures).
        if (path is None or resolved_line is None) and symbol:
            found = _find_definition(repo_root, symbol, class_hint)
            if found is not None:
                path, resolved_line = found

        if path is None or resolved_line is None:
            continue

        snippet = _snippet(path, resolved_line, context)
        room = max(0, max_total_lines - total_lines)
        snippet = snippet[:room]
        total_lines += len(snippet)
        frames.append({"file": os.path.relpath(path, repo_root), "lineno": resolved_line, "symbol": symbol, "snippet": snippet})

    result: dict = {
        "repo": repo_root,
        "frames": frames,
        "files_matched": len(frames),
        "truncated": len(frames) < len(candidates),
    }
    if not frames and candidates:
        result["note"] = "No frames resolved against the checkout. Pass a raw stack frame that includes a file and line (e.g. 'OrderSvc.java:42'), or confirm --repo points at the source root."
    return result
