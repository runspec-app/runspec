"""Exception types raised by the pure helper functions.

The functions in this package do the work and *raise* on failure; the thin
runnable wrappers in ``runspec-fs`` catch these (alongside the stdlib ``OSError``
family — ``FileNotFoundError``, ``NotADirectoryError``, ``PermissionError`` …)
and turn them into the JSON error payloads + non-zero exits that the CLI/agent
surface expects. The OS errors are left to propagate as themselves rather than
being re-wrapped, so a wrapper (or a corporate importer) can react to the exact
failure (missing path vs. permission denied) without parsing a message.
"""


class FsCoreError(Exception):
    """Base class for all runspec-fs-core failures that aren't a plain OSError."""


class EditNotAppliedError(FsCoreError):
    """An ``edit_file`` old-text block was not found in the file, so the edit
    could not be applied (nothing is written)."""
