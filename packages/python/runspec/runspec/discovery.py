"""The `discoverable` keyword — whether a runnable is listed at all.

`discoverable` is a plain boolean visibility gate, orthogonal to `serve` (which
declares *the context*). It is metadata only — it adds no runtime behaviour to
the runnable; the discovery surfaces (`runspec serve`, `runspec local`) read it
to decide whether to expose the runnable:

    absent / true   → visible (the default)
    false           → hidden from all discovery surfaces (internal helper)

A `false` runnable still runs when invoked directly on the CLI; it simply never
surfaces as a discovered tool (e.g. an MCP tool under `runspec serve`). Curating
which runnables a *self-service* audience may see is no longer a format concern —
that lives in the runspec-console catalog configuration.
"""

from __future__ import annotations

from typing import Any


def normalise_discoverable(value: Any) -> bool:
    """Return the canonical boolean for a raw `discoverable` value.

    Lenient — mirrors the rest of the loader: absent/`true` → ``True``,
    `false` → ``False``, any other shape falls back to the default (visible).
    Strict validation is `validate_discoverable`, surfaced by
    `runspec serve`/`local`.
    """
    return value is not False


def validate_discoverable(value: Any) -> list[str]:
    """Return human-readable error messages for an invalid `discoverable` value."""
    if value is None or isinstance(value, bool):
        return []
    return [f"discoverable must be true or false, got {type(value).__name__}"]
