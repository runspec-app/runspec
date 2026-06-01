"""
validator.py — Two-pass validation: individual args then group constraints.

Pass 1: validate each arg in isolation (type, range, required, choices)
Pass 2: validate group constraints across args
"""

from __future__ import annotations

from typing import Any

from runspec import errors


def validate_args(
    parsed_values: dict[str, Any],
    arg_specs: dict[str, Any],
) -> list[str]:
    """
    Pass 1: validate each argument individually.

    Args:
        parsed_values: {arg_name: raw_value} from argv/env/defaults
        arg_specs:     {arg_name: inferred_spec_dict}

    Returns:
        List of error messages. Empty list means all valid.
    """
    error_messages: list[str] = []

    for name, spec in arg_specs.items():
        # parsed_values is keyed by the underscore-normalised name (see
        # _parse_argv / _apply_env / _apply_defaults). Spec names may be
        # hyphenated, so normalise before the lookup — the raw name is kept for
        # display in error messages.
        value = parsed_values.get(name.replace("-", "_"))

        # Required check
        if spec.get("required") and value is None:
            error_messages.append(errors.format_missing_required(name, spec))
            continue

        if value is None:
            continue

        # Deprecated warning (not an error)
        if spec.get("deprecated"):
            import warnings

            warnings.warn(errors.format_deprecated(name, spec["deprecated"]), stacklevel=6)

    return error_messages


def validate_groups(
    parsed_values: dict[str, Any],
    group_specs: dict[str, Any],
) -> list[str]:
    """
    Pass 2: validate group constraints.

    Args:
        parsed_values: {arg_name: raw_value} — None means not provided
        group_specs:   normalised group dicts from the spec

    Returns:
        List of error messages. Empty list means all valid.
    """
    error_messages: list[str] = []

    for group_name, group in group_specs.items():
        group_args = group.get("args", [])
        # parsed_values is keyed by the underscore-normalised name; group args
        # may be hyphenated, so normalise at every lookup site below.
        provided = [a for a in group_args if parsed_values.get(a.replace("-", "_")) is not None]

        if group.get("exclusive") and len(provided) > 1:
            error_messages.append(errors.format_group_exclusive(group_name, provided))

        elif group.get("inclusive") and 0 < len(provided) < len(group_args):
            missing = [a for a in group_args if a not in provided]
            error_messages.append(errors.format_group_inclusive(group_name, missing))

        elif group.get("at_least_one") and len(provided) == 0:
            error_messages.append(errors.format_group_at_least_one(group_name, group_args))

        elif group.get("exactly_one") and len(provided) != 1:
            error_messages.append(errors.format_group_exactly_one(group_name, group_args, provided))

        elif group.get("condition"):
            condition_arg = group["condition"]
            if parsed_values.get(condition_arg.replace("-", "_")) is not None:
                required_args = group.get("requires", [])
                missing = [a for a in required_args if parsed_values.get(a.replace("-", "_")) is None]
                if missing:
                    error_messages.append(errors.format_group_inclusive(group_name, missing))

    return error_messages


def raise_if_errors(error_messages: list[str]) -> None:
    """Raise a RunSpecError with all collected error messages if any exist."""
    if error_messages:
        raise errors.RunSpecError("\n\n".join(error_messages))
