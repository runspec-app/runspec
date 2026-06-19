"""
skillargs.py — a skill's ``[skill.args]`` table *is* a runspec args table.

Parse / validate / shape skill parameters with the exact same core machinery a
runnable form uses, so a skill's inputs behave identically to a runnable's — one
type system, one set of inference rules, one validator, no parallel schema:

- ``parse_skill_args(table)``      → ``list[ArgDef]`` (the shape the Forms UI consumes)
- ``validate_skill_values(table, values)`` → ``list[str]`` (required + range / choice /
  pattern / length / type, reusing ``validator.validate_args`` + ``types.coerce``)

The console already depends on ``runspec``; this module is the single seam between
the console and the core arg machinery for skills.
"""

from __future__ import annotations

from typing import Any

from .discovery import _build_args

# Note: the core ``runspec`` arg machinery (loader / inference / validator / types)
# is imported lazily inside the functions below, mirroring the rest of the console
# (bridge / executor import ``runspec`` lazily). That keeps ``import
# runspec_console`` clean where the core isn't installed (e.g. the wheel smoke test);
# the imports resolve when a skill is actually parsed or validated.

# Types whose core coercer carries local-filesystem / list semantics that don't
# apply to a skill's *template* value: a skill ``path`` is usually a path on a
# remote host (coercing would ``expanduser().resolve()`` against the console
# machine), ``rest`` is a CLI passthrough, and ``flag`` is just a bool. They still
# render in the form; we just skip coercion-validation for them.
_SKIP_COERCE = frozenset({"path", "rest", "flag"})


def _inferred(table: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Normalise + infer a raw ``[skill.args]`` table into the core arg-spec dict
    (the same structure ``loader``/``inference`` produce for a runnable's args)."""
    from runspec import inference, loader

    normalised = loader._normalise_args(table or {})
    return {name: inference.infer_arg(arg) for name, arg in normalised.items()}


def parse_skill_args(table: dict[str, Any] | None) -> list[dict[str, Any]]:
    """A ``[skill.args]`` table → the same ``ArgDef[]`` a runnable exposes to the UI
    (via the shared ``discovery._build_args``)."""
    return _build_args(_inferred(table))


def validate_skill_values(
    table: dict[str, Any] | None, values: dict[str, Any]
) -> list[str]:
    """Validate operator-supplied ``values`` against a skill's args, reusing the core
    required check (``validate_args``) and per-type coercion (range / choice /
    pattern / length / numeric). Returns human-readable error messages ([] = valid)."""
    from runspec.types import coerce
    from runspec.validator import validate_args

    specs = _inferred(table)
    # validate_args keys parsed values by the underscore-normalised name.
    pv = {str(k).replace("-", "_"): v for k, v in (values or {}).items()}
    errors = list(validate_args(pv, specs))
    for name, spec in specs.items():
        value = pv.get(name.replace("-", "_"))
        if value is None or spec.get("type") in _SKIP_COERCE:
            continue
        try:
            coerce(value, spec)
        except (ValueError, TypeError) as exc:
            errors.append(str(exc))
    return errors
