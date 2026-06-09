"""Exception types raised by the pure helper functions.

The functions in this package do the work and *raise* on failure; the thin
runnable wrappers in ``runspec-linux`` catch these and turn them into the JSON
error payloads + non-zero exits that the CLI/agent surface expects. Keeping the
error types here (rather than re-deriving them from messages) lets each wrapper
format its own shape — e.g. a missing tool vs. a command that ran but failed.
"""


class LinuxCoreError(Exception):
    """Base class for all runspec-linux-core failures."""


class ToolNotFoundError(LinuxCoreError):
    """A required system tool (systemctl, docker, ss, last, ...) is not on PATH."""


class CommandError(LinuxCoreError):
    """A subprocess ran to completion but reported failure (non-zero exit)."""
