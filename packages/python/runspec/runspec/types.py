"""
types.py — Type registry and built-in Python type coercers.

The type registry is the boundary between the language-agnostic core
and the Python language pack. Core declares type names as strings.
This module coerces those strings into native Python types.

Custom types can be registered by downstream packages or user code:
    import runspec
    runspec.register_type("json-file", lambda v, arg: json.loads(Path(v).read_text()))
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

# Type coercer signature: (raw_value: str, arg_spec: dict) -> Any
TypeCoercer = Callable[[Any, dict[str, Any]], Any]

# The global type registry
_REGISTRY: dict[str, TypeCoercer] = {}


def register_type(name: str, coercer: TypeCoercer) -> None:
    """
    Register a custom type coercer.

    Args:
        name:    The type name as it appears in the spec (e.g. "json-file")
        coercer: Callable(raw_value, arg_spec) → coerced value

    Example:
        import json
        import runspec
        runspec.register_type(
            "json-file",
            lambda v, arg: json.loads(Path(v).read_text())
        )
    """
    _REGISTRY[name] = coercer


def coerce(raw_value: Any, arg_spec: dict[str, Any]) -> Any:
    """
    Coerce a raw string value to its native Python type.

    Args:
        raw_value: The raw value from argv, env, or default
        arg_spec:  The fully inferred arg spec dict

    Returns:
        The coerced native Python value

    Raises:
        TypeError:  if the type is unknown
        ValueError: if coercion fails (e.g. "abc" for an int arg)
    """
    type_name = arg_spec.get("type", "str")
    coercer = _REGISTRY.get(type_name)

    if coercer is None:
        raise TypeError(
            f"Unknown type '{type_name}' for argument '{arg_spec.get('name', '?')}'. Registered types: {', '.join(sorted(_REGISTRY.keys()))}\nRegister custom types with runspec.register_type()."
        )

    try:
        return coercer(raw_value, arg_spec)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Cannot coerce value {raw_value!r} to type '{type_name}' for argument '--{arg_spec.get('name', '?')}': {e}") from e


def list_types() -> list[str]:
    """Return all registered type names."""
    return sorted(_REGISTRY.keys())


# ── Built-in Python type coercers ─────────────────────────────────────────────


def _coerce_str(value: Any, arg: dict[str, Any]) -> str:
    return str(value)


def _coerce_int(value: Any, arg: dict[str, Any]) -> int:
    coerced = int(value)
    _check_range(coerced, arg)
    return coerced


def _coerce_float(value: Any, arg: dict[str, Any]) -> float:
    coerced = float(value)
    _check_range(coerced, arg)
    return coerced


def _coerce_bool(value: Any, arg: dict[str, Any]) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in ("true", "1", "yes", "on"):
            return True
        if value.lower() in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"Cannot interpret {value!r} as bool")


def _coerce_flag(value: Any, arg: dict[str, Any]) -> bool:
    """Flags are True when present on CLI, False otherwise."""
    if isinstance(value, bool):
        return value
    return bool(value)


def _coerce_path(value: Any, arg: dict[str, Any]) -> Path:
    return Path(str(value)).expanduser().resolve()


def _coerce_choice(value: Any, arg: dict[str, Any]) -> str:
    coerced = str(value)
    options = arg.get("options", [])
    if options and coerced not in options:
        from runspec.errors import format_invalid_choice

        raise ValueError(format_invalid_choice(coerced, options, arg.get("name", "?")))
    return coerced


def _coerce_rest(value: Any, arg: dict[str, Any]) -> list[str]:
    """Pass-through args (everything after `--`). Always a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _check_range(value: int | float, arg: dict[str, Any]) -> None:
    """Validate a numeric value is within the declared range."""
    range_ = arg.get("range")
    if range_ is not None:
        min_val, max_val = range_
        if not (min_val <= value <= max_val):
            raise ValueError(f"Value {value} is out of range [{min_val}, {max_val}]")


# Register all built-in coercers
register_type("str", _coerce_str)
register_type("int", _coerce_int)
register_type("float", _coerce_float)
register_type("bool", _coerce_bool)
register_type("flag", _coerce_flag)
register_type("path", _coerce_path)
register_type("choice", _coerce_choice)
register_type("rest", _coerce_rest)
