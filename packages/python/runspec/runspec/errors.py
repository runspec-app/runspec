"""
errors.py — Human-first error formatting with fuzzy suggestions.

Every error includes:
  - what failed
  - what was expected
  - what was received
  - a suggestion where possible (via difflib, stdlib, zero dependencies)
"""

from __future__ import annotations

import difflib
from typing import Any


class RunSpecError(Exception):
    """Base class for all runspec errors."""


class MissingRequiredArg(RunSpecError):
    """A required argument was not provided."""


class InvalidChoice(RunSpecError):
    """A value was not in the declared options list."""


class OutOfRange(RunSpecError):
    """A numeric value was outside the declared range."""


class InvalidPattern(RunSpecError):
    """A string value did not match the declared pattern."""


class InvalidLength(RunSpecError):
    """A string value violated the declared min-/max-length bounds."""


class UnknownArg(RunSpecError):
    """An argument was provided that is not in the spec."""


class GroupViolation(RunSpecError):
    """A group constraint was violated."""


class AutonomyViolation(RunSpecError):
    """An agent attempted to exceed its declared autonomy level."""


def format_missing_required(name: str, arg_spec: dict[str, Any]) -> str:
    lines = [
        f"✗  Missing required argument: --{name}",
        f"   Type: {arg_spec.get('type', 'str')}",
    ]
    if arg_spec.get("description"):
        lines.append(f"   Description: {arg_spec['description']}")
    if arg_spec.get("env"):
        lines.append(f"   Tip: set environment variable {arg_spec['env']} as an alternative")
    return "\n".join(lines)


def format_invalid_choice(value: str, options: list[str], name: str) -> str:
    lines = [
        f"✗  Invalid value for --{name}: {value!r}",
        f"   Expected one of: {', '.join(str(o) for o in options)}",
        f"   Got: {value!r}",
    ]
    suggestion = _suggest(value, [str(o) for o in options])
    if suggestion:
        lines.append(f"\n   Did you mean: {suggestion}?")
    return "\n".join(lines)


def format_out_of_range(
    value: int | float,
    range_: tuple[Any, Any],
    name: str,
) -> str:
    min_val, max_val = range_
    return "\n".join(
        [
            f"✗  Value out of range for --{name}: {value}",
            f"   Expected: between {min_val} and {max_val}",
            f"   Got: {value}",
        ]
    )


def format_invalid_pattern(value: str, pattern: str, name: str) -> str:
    return "\n".join(
        [
            f"✗  Invalid value for --{name}: {value!r}",
            f"   Expected: a value matching pattern {pattern!r}",
            f"   Got: {value!r}",
        ]
    )


def format_too_short(value: str, min_length: int, name: str) -> str:
    return "\n".join(
        [
            f"✗  Value too short for --{name}: {value!r}",
            f"   Expected: at least {min_length} character{'' if min_length == 1 else 's'}",
            f"   Got: {len(value)}",
        ]
    )


def format_too_long(value: str, max_length: int, name: str) -> str:
    return "\n".join(
        [
            f"✗  Value too long for --{name}: {value!r}",
            f"   Expected: at most {max_length} character{'' if max_length == 1 else 's'}",
            f"   Got: {len(value)}",
        ]
    )


def format_unknown_arg(name: str, known_args: list[str]) -> str:
    lines = [
        f"✗  Unknown argument: --{name}",
        f"   Known arguments: {', '.join(f'--{a}' for a in sorted(known_args))}",
    ]
    suggestion = _suggest(name, known_args)
    if suggestion:
        lines.append(f"\n   Did you mean: --{suggestion}?")
    return "\n".join(lines)


def format_group_exclusive(group_name: str, provided: list[str]) -> str:
    return "\n".join(
        [
            f"✗  Conflicting arguments in group '{group_name}'",
            f"   --{provided[0]} and --{provided[1]} cannot be used together",
            "   Choose one or the other",
        ]
    )


def format_group_inclusive(group_name: str, missing: list[str]) -> str:
    missing_flags = " and ".join(f"--{m}" for m in missing)
    return "\n".join(
        [
            f"✗  Incomplete argument group '{group_name}'",
            "   Providing one of these args requires all of them",
            f"   Also provide: {missing_flags}",
        ]
    )


def format_group_at_least_one(group_name: str, args: list[str]) -> str:
    return "\n".join(
        [
            f"✗  Group '{group_name}' requires at least one argument",
            f"   Provide at least one of: {', '.join(f'--{a}' for a in args)}",
        ]
    )


def format_group_exactly_one(group_name: str, args: list[str], provided: list[str]) -> str:
    if not provided:
        return "\n".join(
            [
                f"✗  Group '{group_name}' requires exactly one argument",
                f"   Provide exactly one of: {', '.join(f'--{a}' for a in args)}",
            ]
        )
    return "\n".join(
        [
            f"✗  Group '{group_name}' requires exactly one argument",
            f"   Got {len(provided)}: {', '.join(f'--{a}' for a in provided)}",
            f"   Provide exactly one of: {', '.join(f'--{a}' for a in args)}",
        ]
    )


def format_autonomy_violation(
    script_name: str,
    required_level: str,
    reason: str | None,
) -> str:
    lines = [
        f"✗  Cannot run '{script_name}' autonomously",
        f"   Autonomy level: {required_level}",
    ]
    if reason:
        lines.append(f"   Reason: {reason}")
    lines.append("\n   Awaiting human confirmation...")
    return "\n".join(lines)


def format_deprecated(name: str, message: str) -> str:
    return f"⚠  --{name} is deprecated: {message}"


def _suggest(value: str, candidates: list[str]) -> str | None:
    """
    Return the closest match from candidates using difflib.
    Returns None if no close match found.
    Uses stdlib only — zero extra dependencies.
    """
    matches = difflib.get_close_matches(value, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None
