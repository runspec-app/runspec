"""cli.py — the ``filesystem`` entry point (folded into runspec-windows).

One binary, a verb per Filesystem MCP tool. ``rs.parse("filesystem")`` resolves the
subcommand and its args (from the merged ``[fs]`` tables in the pack's
``runspec.toml``); we unpack each ``.value`` at the boundary into a plain kwargs
dict and run the matching :mod:`runspec_fs_core` helper.

**Per-invocation ``--run-as``** (a global arg, like ``runspec-linux``'s venv
runnables): when set to a different OS user, the resolved ``(verb, kwargs)`` is
shipped over stdin to :mod:`runspec_windows.fs._worker` re-invoked as that user via
``sudo -n -u`` / ``su`` (:func:`runspec_fs_core.build_become`), so the in-process
verb runs with that user's permissions. Access is bounded by that user's OS
permissions — there is no directory jail.

A verbatim copy of ``runspec_fs.cli`` with the local ``_dispatch`` / ``_worker``
import paths; the logic is the shared ``runspec_fs_core``.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from typing import Any

import runspec as rs
from runspec_fs_core import (
    BecomeError,
    FsCoreError,
    build_become,
    current_user,
    escalation_supported,
    fill_verb_secrets,
    mask_secrets,
    mask_text,
)

from runspec_windows.fs._dispatch import run_verb


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str))


def _pair_edits(old_text: list[str], new_text: list[str]) -> list[list[str]]:
    """Zip the two parallel ``--old-text`` / ``--new-text`` lists into
    ``[old, new]`` pairs, erroring if their counts differ."""
    if len(old_text) != len(new_text):
        raise ValueError(f"--old-text / --new-text count mismatch: {len(old_text)} vs {len(new_text)} (pair them by position)")
    return [[old, new] for old, new in zip(old_text, new_text, strict=False)]


def _octal(mode: str | None) -> int | None:
    """``--mode 0640`` -> ``0o640`` (``None`` when unset)."""
    if not mode:
        return None
    try:
        return int(mode, 8)
    except ValueError:
        raise ValueError(f"--mode must be octal permissions like 0600 or 0640, got {mode!r}") from None


def _audit_secret_write(verb: str, kwargs: dict[str, Any], credential_ids: list[str], outcome: str) -> None:
    """One ``fs.secret_written`` audit event when the text held ``{{secret:…}}``
    placeholders: the path and credential ids, never the values."""
    if not credential_ids:
        return
    # The audit must never break the write's own result.
    with contextlib.suppress(Exception):
        rs.audit(
            "fs.secret_written",
            outcome,
            target={"path": str(kwargs.get("path", ""))},
            detail={"verb": verb, "credentials": credential_ids, "dry_run": bool(kwargs.get("dry_run"))},
        )


def _verb_kwargs(spec: rs.RunSpec) -> dict[str, Any]:
    """Unpack the parsed spec into a plain, JSON-round-trip-safe kwargs dict for
    the active verb (paths as strings), so the same dict serves the in-process
    path and the run-as worker."""
    command = spec.runspec_command

    if command == "read-text-file":
        return {"path": str(spec.path.value), "head": spec.head.value, "tail": spec.tail.value}
    if command == "read-media-file":
        return {"path": str(spec.path.value)}
    if command == "read-multiple-files":
        return {"paths": [str(p) for p in spec.paths.value]}
    if command == "list-directory":
        return {"path": str(spec.path.value)}
    if command == "list-directory-with-sizes":
        return {"path": str(spec.path.value), "sort_by": spec.sort_by.value}
    if command == "directory-size":
        return {"path": str(spec.path.value), "limit": spec.limit.value, "depth": spec.depth.value, "cross_mounts": spec.cross_mounts.value}
    if command == "directory-tree":
        return {"path": str(spec.path.value), "exclude": spec.exclude.value or None}
    if command == "search-files":
        return {"path": str(spec.path.value), "pattern": spec.pattern.value, "exclude": spec.exclude.value or None}
    if command == "get-file-info":
        return {"path": str(spec.path.value)}
    if command == "list-allowed-directories":
        return {}
    if command == "write-file":
        return {"path": str(spec.path.value), "content": spec.content.value, "mode": _octal(spec.mode.value)}
    if command == "edit-file":
        return {
            "path": str(spec.path.value),
            "edits": _pair_edits(spec.old_text.value, spec.new_text.value),
            "dry_run": spec.dry_run.value,
            "mode": _octal(spec.mode.value),
        }
    if command == "create-directory":
        return {"path": str(spec.path.value)}
    if command == "move-file":
        return {"source": str(spec.source.value), "destination": str(spec.destination.value)}

    # require-command = true makes a bare invocation unreachable.
    raise SystemExit("no command — see `filesystem --help`")  # pragma: no cover


def _run_as_worker(run_as: str, method: str, verb: str, kwargs: dict[str, Any], masks: dict[str, str] | None = None) -> str:
    """Re-invoke the verb as ``run_as`` via the worker under sudo/su and return
    its stdout. Raises :class:`BecomeError` on an escalation failure (a
    passwordless-sudo hint on the classic cases)."""
    if not escalation_supported():
        raise BecomeError("--run-as requires a POSIX host (sudo/su)")
    argv = build_become([sys.executable, "-m", "runspec_windows.fs._worker"], run_as, method)
    payload = json.dumps({"verb": verb, "kwargs": kwargs})
    try:
        proc = subprocess.run(argv, input=payload, capture_output=True, text=True)
    except FileNotFoundError as exc:  # sudo / su not on PATH
        raise BecomeError(f"{method} not found: {exc}") from exc

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        low = err.lower()
        if "password is required" in low or "a terminal is required" in low:
            raise BecomeError(f"passwordless {method} is not configured for run-as {run_as!r} ({err})")
        # The worker prints a JSON error on stdout for tool-level failures — pass
        # it through with the worker's exit code rather than masking it.
        out = (proc.stdout or "").strip()
        if out:
            print(mask_text(out, masks or {}))
            sys.exit(proc.returncode)
        raise BecomeError(err or f"run-as {run_as!r} failed (exit {proc.returncode})")
    return proc.stdout


def main() -> None:
    spec = rs.parse("filesystem")
    run_as = spec.run_as.value
    method = spec.become_method.value
    verb = spec.runspec_command
    # `request` is the call as given (placeholders, never values) — the audit
    # record reads only that. `kwargs` is what the verb runs with.
    request: dict[str, Any] = {}
    masks: dict[str, str] = {}
    credential_ids: list[str] = []
    try:
        request = _verb_kwargs(spec)
        # {{secret:<id>}} placeholders are filled here, before any --run-as
        # hand-off (sudo drops the environment; the worker gets the filled text
        # on stdin). The values are masked back out of everything printed.
        kwargs, masks, credential_ids = fill_verb_secrets(verb, request)
        if run_as and run_as != current_user():
            try:
                out = _run_as_worker(run_as, method, verb, kwargs, masks)
            except SystemExit:
                _audit_secret_write(verb, request, credential_ids, "failed")
                raise
            _audit_secret_write(verb, request, credential_ids, "succeeded")
            out = mask_text(out, masks)
            sys.stdout.write(out if out.endswith("\n") else out + "\n")
            return
        result = run_verb(verb, kwargs)
    except (FsCoreError, OSError, ValueError) as exc:
        _audit_secret_write(verb, request, credential_ids, "failed")
        _emit(mask_secrets({"error": str(exc), "kind": type(exc).__name__}, masks))
        sys.exit(1)
    _audit_secret_write(verb, request, credential_ids, "succeeded")
    _emit(mask_secrets(result, masks))


if __name__ == "__main__":
    main()
