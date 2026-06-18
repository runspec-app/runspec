"""Exception types raised by the pure helper functions.

The functions in this package do the work and *raise* on failure; the thin
runnable wrappers in ``runspec-logops`` (and any private wrapper that imports the
helpers) catch these and turn them into the JSON error payloads + non-zero exits
that the CLI/agent surface expects.
"""


class LogopsCoreError(Exception):
    """Base class for all runspec-logops-core failures."""


class SourceNotFoundError(LogopsCoreError):
    """A required input path (the log file or the repo checkout) does not exist."""
