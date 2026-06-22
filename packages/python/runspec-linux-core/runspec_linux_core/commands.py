"""Command-location helpers — find executables on the search PATH."""

import os


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _resolve(name: str, dirs: list[str]) -> list[str]:
    """Return every executable match for ``name`` across ``dirs`` (in order)."""
    # A name with a path separator is a path, not a bare command: check it
    # directly rather than searching the PATH (mirrors `which`/`shutil.which`).
    if os.sep in name or (os.altsep and os.altsep in name):
        return [os.path.abspath(name)] if _is_executable(name) else []
    seen: set[str] = set()
    matches: list[str] = []
    for d in dirs:
        full = os.path.join(d, name)
        if full in seen:
            continue
        seen.add(full)
        if _is_executable(full):
            matches.append(full)
    return matches


def which(
    names: str | list[str],
    *,
    all_matches: bool = False,
    path: str | None = None,
) -> dict:
    """Locate executables on the search PATH, like the ``which`` command.

    ``names`` is a single command name or a list of them; a string is split on
    whitespace so ``"python3 pip3"`` resolves both. ``all_matches`` returns every
    match found across the PATH (not just the first one that would run). ``path``
    overrides the search path (defaults to ``$PATH``, falling back to
    ``os.defpath``).

    Returns::

        {"path": "<search path used>",
         "all_found": bool,                       # every requested name resolved
         "results": [{"name": str, "found": bool,
                      "path": str | None,         # first match, else None
                      "paths": [str, ...]}]}      # all matches if all_matches

    Raises ``ValueError`` when no name is given.
    """
    if isinstance(names, str):
        names = names.split()
    names = [n for n in names if n]
    if not names:
        raise ValueError("no command name given")

    search = os.environ.get("PATH", os.defpath) if path is None else path
    dirs = [d or os.curdir for d in search.split(os.pathsep)]

    results = []
    for name in names:
        matches = _resolve(name, dirs)
        results.append(
            {
                "name": name,
                "found": bool(matches),
                "path": matches[0] if matches else None,
                "paths": matches if all_matches else matches[:1],
            }
        )
    return {
        "path": search,
        "all_found": all(r["found"] for r in results),
        "results": results,
    }
