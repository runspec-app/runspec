"""
parser.py — Main entry point. Builds a RunSpec from sys.argv.

Orchestrates: find → load → infer → parse argv → validate → coerce → build RunSpec
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from runspec import errors
from runspec.finder import find_config
from runspec.inference import effective_autonomy, infer_script
from runspec.loader import load_raw
from runspec.logging_setup import configure_logging
from runspec.models import Arg, Group, RunSpec
from runspec.types import coerce
from runspec.validator import raise_if_errors, validate_args, validate_groups


def parse(script_name: str | None = None, argv: list[str] | None = None, config_path: Path | None = None) -> RunSpec:
    """
    Parse arguments for the calling runnable.

    Args:
        script_name: Override runnable name. Inferred from [project.scripts] if None.
        argv:        Override sys.argv. Uses sys.argv[1:] if None. Useful for testing.
        config_path: Override config file location. Walks up from cwd if None.

    Returns:
        RunSpec — the fully parsed, validated, coerced argument namespace.
    """
    try:
        return _parse_impl(script_name, argv, config_path)
    except errors.RunSpecError as e:
        print(str(e))
        sys.exit(1)
    except FileNotFoundError as e:
        print(str(e))
        sys.exit(1)


def _parse_impl(script_name: str | None = None, argv: list[str] | None = None, config_path: Path | None = None) -> RunSpec:
    """Internal: full parse pipeline. Raises on error — call parse() for CLI use."""
    # 1. Find config — explicit arg > RUNSPEC_CONFIG env var > walk up from cwd
    if config_path is None:
        env_config = os.environ.get("RUNSPEC_CONFIG")
        config_path = Path(env_config) if env_config else find_config()

    # 2. Load and normalise TOML
    raw = load_raw(config_path)
    config = raw["config"]

    # 3. Resolve runnable name
    name = script_name or _infer_from_argv()

    # Guard reserved name
    if name == "config":
        raise errors.RunSpecError("✗  'config' is a reserved name in runspec.\n   Rename your runnable to something else.")

    if name not in raw["runnables"]:
        available = ", ".join(raw["runnables"].keys()) or "(none)"
        raise errors.RunSpecError(f"✗  Runnable '{name}' not found in runspec config.\n   Available runnables: {available}\n   Config: {config_path}")

    # 4. Infer defaults for the script
    raw_script = infer_script(raw["runnables"][name], config["autonomy_default"])

    # 4.5. Auto-inject log-level arg when [config.logging] is present
    if config.get("logging") and "log-level" not in raw_script["args"]:
        raw_script["args"]["log-level"] = {
            "name": "log-level",
            "type": "choice",
            "options": ["debug", "info", "warning", "error", "critical"],
            "default": config["logging"]["level"],
            "required": False,
            "description": "Override the console log level for this invocation.",
            "multiple": False,
            "delimiter": None,
            "short": None,
            "env": "RUNSPEC_LOG_LEVEL",
            "deprecated": None,
            "autonomy": None,
            "ui": None,
            "meta": None,
            "position": None,
            "range": None,
        }

    # 5. Resolve subcommand if any
    argv_list = argv if argv is not None else sys.argv[1:]
    raw_script, command_path, argv_list = _resolve_subcommand(raw_script, argv_list)

    # 6. Handle --help / -h before any validation
    if "--help" in argv_list or "-h" in argv_list:
        _print_help(name, raw_script, command_path[-1] if command_path else None)
        sys.exit(0)

    # 7. Parse argv into raw values
    parsed_values = _parse_argv(argv_list, raw_script["args"])

    # 8. Apply env var fallbacks
    parsed_values = _apply_env(parsed_values, raw_script["args"])

    # 9. Apply defaults
    parsed_values = _apply_defaults(parsed_values, raw_script["args"])

    # 10. Pass 1 — validate individual args
    arg_errors = validate_args(parsed_values, raw_script["args"])
    raise_if_errors(arg_errors)

    # 11. Pass 2 — validate groups
    group_errors = validate_groups(parsed_values, raw_script["groups"])
    raise_if_errors(group_errors)

    # 12. Coerce values to native Python types
    coerced_values = _coerce_values(parsed_values, raw_script["args"])

    # 13. Calculate effective autonomy
    autonomy = effective_autonomy(
        raw_script["autonomy"],
        parsed_values,
        raw_script["args"],
    )

    # 14. Detect agent context
    agent = os.environ.get("RUNSPEC_AGENT", "").lower() in ("1", "true", "yes")

    # 15. Build RunSpec
    runspec_obj = _build_runspec(
        name=name,
        config_path=config_path,
        command_path=command_path,
        autonomy=autonomy,
        agent=agent,
        coerced_values=coerced_values,
        arg_specs=raw_script["args"],
        group_specs=raw_script["groups"],
        raw_script=raw_script,
    )

    # 16. Configure logging (no-op when [config.logging] absent)
    log_level_override = None
    if config.get("logging"):
        pair = coerced_values.get("log_level")  # stores (value, source) tuples
        if pair and pair[0] is not None:
            log_level_override = str(pair[0])
    try:
        configure_logging(
            config.get("logging"),
            agent=agent,
            runnable_name=name,
            config_path=config_path,
            log_level_override=log_level_override,
        )
    except ValueError as e:
        raise errors.RunSpecError(str(e)) from e

    return runspec_obj


def load_spec(script_name: str | None = None, config_path: Path | None = None) -> RunSpec:
    """
    Load the spec without parsing argv. Useful for introspection,
    emit, and scaffold operations.

    Returns a RunSpec with default values only — no CLI args applied.
    """
    return parse(script_name=script_name, argv=[], config_path=config_path)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _print_help(name: str, script: dict[str, Any], command: str | None) -> None:
    """Print a human-readable help message for a runnable and exit."""
    full_name = f"{name} {command}" if command else name
    description = script.get("description") or ""
    args = script.get("args", {})
    commands = script.get("commands", {})
    examples = script.get("examples", [])

    # Partition args into positionals, rest, and flags
    positional_args = sorted(
        ((spec["position"], arg_name, spec) for arg_name, spec in args.items() if spec.get("position") is not None),
        key=lambda p: p[0],
    )
    rest_args = [(arg_name, spec) for arg_name, spec in args.items() if spec.get("type") == "rest"]
    flag_args = [(arg_name, spec) for arg_name, spec in args.items() if spec.get("position") is None and spec.get("type") != "rest"]

    # ── Usage line ────────────────────────────────────────────────────────────
    usage_parts = [full_name]
    if commands:
        usage_parts.append("<command>")
    for arg_name, spec in flag_args:
        flag = f"--{arg_name}"
        if spec.get("type") == "flag":
            usage_parts.append(f"[{flag}]")
        elif spec.get("required"):
            usage_parts.append(f"{flag} <{spec.get('type', 'str')}>")
        else:
            usage_parts.append(f"[{flag} <{spec.get('type', 'str')}>]")
    for _, arg_name, spec in positional_args:
        if spec.get("required"):
            usage_parts.append(f"<{arg_name}>")
        else:
            usage_parts.append(f"[<{arg_name}>]")
    for arg_name, _ in rest_args:
        usage_parts.append(f"[-- <{arg_name}>...]")

    print(f"Usage: {' '.join(usage_parts)}")
    if description:
        print(f"\n{description}")

    # ── Commands ──────────────────────────────────────────────────────────────
    if commands:
        print("\nCommands:")
        cmd_col = max(len(c) for c in commands) + 2
        for cmd_name, cmd_spec in commands.items():
            cmd_desc = cmd_spec.get("description") or ""
            print(f"  {cmd_name:<{cmd_col}} {cmd_desc}")

    # ── Positional arguments ──────────────────────────────────────────────────
    if positional_args or rest_args:
        print("\nPositional arguments:")
        for _, arg_name, spec in positional_args:
            label = f"  <{arg_name}>"
            parts = [spec.get("type", "str")]
            if spec.get("required"):
                parts.append("required")
            elif spec.get("default") is not None:
                parts.append(f"default: {spec['default']}")
            desc = spec.get("description") or ""
            print(f"{label:<24} {desc}  ({', '.join(parts)})" if desc else f"{label:<24} ({', '.join(parts)})")
        for arg_name, spec in rest_args:
            label = f"  -- <{arg_name}>..."
            desc = spec.get("description") or ""
            print(f"{label:<24} {desc}  (rest)" if desc else f"{label:<24} (rest)")

    # ── Options ───────────────────────────────────────────────────────────────
    if flag_args:
        header = "Options:" if (positional_args or rest_args) else "Arguments:"
        print(f"\n{header}")
        for arg_name, spec in flag_args:
            short = spec.get("short")
            flag = f"  {short}, --{arg_name}" if short else f"  --{arg_name}"
            arg_type = spec.get("type", "str")
            parts = []
            if arg_type == "flag":
                parts.append("flag")
            else:
                parts.append(arg_type)
            if spec.get("required"):
                parts.append("required")
            elif spec.get("default") is not None:
                parts.append(f"default: {spec['default']}")
            if spec.get("options"):
                parts.append(f"one of: {', '.join(str(o) for o in spec['options'])}")
            if spec.get("description"):
                print(f"{flag:<28} {spec['description']}  ({', '.join(parts)})")
            else:
                print(f"{flag:<28} ({', '.join(parts)})")

    # ── Autonomy ──────────────────────────────────────────────────────────────
    autonomy = script.get("autonomy")
    if autonomy:
        print(f"\nAutonomy: {autonomy}")
        if script.get("autonomy_reason"):
            print(f"  {script['autonomy_reason']}")

    # ── Examples ──────────────────────────────────────────────────────────────
    if examples:
        print("\nExamples:")
        ex_col = max(len(e["cmd"]) for e in examples) + 2
        for ex in examples:
            cmd_str = ex["cmd"]
            desc = ex.get("description") or ""
            if desc:
                print(f"  {cmd_str:<{ex_col}} # {desc}")
            else:
                print(f"  {cmd_str}")

    # ── Trailer ───────────────────────────────────────────────────────────────
    if commands:
        print(f"\nRun '{full_name} <command> --help' for focused help on a command.")
    else:
        print("\n  -h, --help    Show this message and exit")


def _infer_from_argv() -> str:
    """Infer script name from sys.argv[0]."""
    return Path(sys.argv[0]).stem if sys.argv else "unknown"


def _resolve_subcommand(
    raw_script: dict[str, Any],
    argv: list[str],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """
    If the script has subcommands and argv[0] matches one, return the
    subcommand spec, command path, and remaining argv. Otherwise return
    the script unchanged with an empty path.
    """
    commands = raw_script.get("commands", {})
    if not commands or not argv:
        return raw_script, [], argv

    candidate = argv[0]
    if candidate in commands:
        return commands[candidate], [candidate], argv[1:]

    return raw_script, [], argv


def _parse_argv(
    argv: list[str],
    arg_specs: dict[str, Any],
) -> dict[str, Any]:
    """
    Parse argv into a raw dict of {arg_name: value | None}.
    Handles --flag, --key value, --key=value, -short, and multiple values.

    Positional args (those with `position = N` in the spec) consume non-flag
    tokens in declaration order. A `rest` type arg captures everything after
    a literal `--` separator as a list.
    """
    # Build lookup maps and identify positional + rest args
    name_map: dict[str, str] = {}  # --flag-name → normalised_name
    short_map: dict[str, str] = {}  # -v → normalised_name
    positionals: list[tuple[int, str, dict[str, Any]]] = []  # (position, norm_name, spec)
    rest_name: str | None = None

    # Track owners for collision detection — error early on duplicates rather
    # than silently last-wins, which is the worst kind of bug.
    shorts_seen: dict[str, str] = {}
    positions_seen: dict[int, str] = {}
    rest_owner: str | None = None

    for name, spec in arg_specs.items():
        normalised = name.replace("-", "_")
        if spec.get("type") == "rest":
            if rest_owner is not None:
                print(f"✗  Multiple 'rest' args declared: '{rest_owner}' and '{name}'. At most one per runnable.")
                sys.exit(1)
            rest_owner = name
            rest_name = normalised
            continue
        if spec.get("position") is not None:
            pos = spec["position"]
            if pos in positions_seen:
                print(f"✗  Position {pos} is declared by both '{positions_seen[pos]}' and '{name}'.")
                sys.exit(1)
            positions_seen[pos] = name
            positionals.append((pos, normalised, spec))
            continue
        name_map[f"--{name}"] = normalised
        name_map[f"--{normalised}"] = normalised
        if spec.get("short"):
            short = spec["short"]
            if short == "-h":
                print(f"✗  Argument '--{name}' declares short='-h', which is reserved for --help.")
                sys.exit(1)
            if short in shorts_seen:
                print(f"✗  Short flag '{short}' is declared by both '--{shorts_seen[short]}' and '--{name}'.")
                sys.exit(1)
            shorts_seen[short] = name
            short_map[short] = normalised

    positionals.sort(key=lambda p: p[0])

    result: dict[str, Any] = {name.replace("-", "_"): None for name in arg_specs}

    unknown: list[str] = []  # collected unrecognised tokens, reported as one error at the end

    # Split on `--` for rest pass-through
    if "--" in argv:
        sep_idx = argv.index("--")
        rest_tokens = argv[sep_idx + 1 :]
        argv = argv[:sep_idx]
        if rest_name is not None:
            result[rest_name] = rest_tokens
        else:
            # `--` was used but no `rest` arg is declared — the trailing tokens
            # have no home. Surface them so typos like `serve --dev` aren't
            # silently dropped.
            unknown.extend(rest_tokens)

    positional_idx = 0
    i = 0
    while i < len(argv):
        token = argv[i]

        # --key=value form
        if "=" in token and token.startswith("--"):
            key, value = token.split("=", 1)
            norm = name_map.get(key)
            if norm:
                result[norm] = _append_or_set(result.get(norm), value, arg_specs.get(norm.replace("_", "-"), arg_specs.get(norm, {})))
            else:
                unknown.append(key)
            i += 1
            continue

        # --flag or -short
        norm = name_map.get(token) or short_map.get(token)
        if norm:
            spec = arg_specs.get(norm.replace("_", "-"), arg_specs.get(norm, {}))
            arg_type = spec.get("type", "str")

            if arg_type == "flag":
                result[norm] = True
                i += 1
            elif i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                raw_str = argv[i + 1]
                delimiter = spec.get("delimiter")
                parsed: str | list[str] = raw_str.split(delimiter) if delimiter else raw_str
                result[norm] = _append_or_set(result.get(norm), parsed, spec)
                i += 2
            else:
                options = spec.get("options")
                hint = f" Expected one of: {', '.join(str(o) for o in options)}" if options else ""
                print(f"✗  --{norm.replace('_', '-')} requires a value.{hint}")
                sys.exit(1)
            continue

        # Unknown flag — token starts with `-` but didn't match name_map or short_map
        if token.startswith("-"):
            unknown.append(token)
            i += 1
            continue

        # Positional — non-flag token assigned to next available positional spec
        if positional_idx < len(positionals):
            _, pos_name, _ = positionals[positional_idx]
            result[pos_name] = token
            positional_idx += 1
            i += 1
            continue

        # No positional slot left — extra positional token is also unknown
        unknown.append(token)
        i += 1

    if unknown:
        valid_flags = sorted({f for f in name_map if f.startswith("--")})
        hint = f"\n   Valid options: {', '.join(valid_flags)}" if valid_flags else ""
        joined = ", ".join(unknown)
        plural = "argument" if len(unknown) == 1 else "arguments"
        print(f"✗  Unknown {plural}: {joined}{hint}")
        sys.exit(1)

    return result


def _append_or_set(current: Any, value: Any, spec: dict[str, Any]) -> Any:
    """For multiple=true args, accumulate values. Otherwise set."""
    if spec.get("multiple"):
        if isinstance(value, list):
            return (current or []) + value
        return (current or []) + [value]
    return value


def _apply_env(
    parsed: dict[str, Any],
    arg_specs: dict[str, Any],
) -> dict[str, Any]:
    """Apply environment variable fallbacks where values are still None."""
    result = dict(parsed)
    for name, spec in arg_specs.items():
        norm = name.replace("-", "_")
        if result.get(norm) is None and spec.get("env"):
            env_val = os.environ.get(spec["env"])
            if env_val is not None:
                result[norm] = env_val
    return result


def _apply_defaults(
    parsed: dict[str, Any],
    arg_specs: dict[str, Any],
) -> dict[str, Any]:
    """Apply spec defaults where values are still None."""
    result = dict(parsed)
    for name, spec in arg_specs.items():
        norm = name.replace("-", "_")
        if result.get(norm) is None and spec.get("default") is not None:
            result[norm] = spec["default"]
    return result


def _coerce_values(
    parsed: dict[str, Any],
    arg_specs: dict[str, Any],
) -> dict[str, Any]:
    """Coerce all resolved values to native Python types."""
    result: dict[str, Any] = {}
    for name, spec in arg_specs.items():
        norm = name.replace("-", "_")
        value = parsed.get(norm)
        if value is None:
            result[norm] = (None, "default")
            continue
        source = _determine_source(norm, parsed)
        try:
            coerced = coerce(value, spec)
        except (ValueError, TypeError) as e:
            raise errors.RunSpecError(f"✗  {e}") from e
        result[norm] = (coerced, source)
    return result


def _determine_source(name: str, parsed: dict[str, Any]) -> str:
    """Determine where a value came from: cli, env, config, or default."""
    # Simplified — a full implementation would track this through the pipeline
    # For now: if value is non-None it came from cli or env
    return "cli" if parsed.get(name) is not None else "default"


def _build_runspec(
    name: str,
    config_path: Path,
    command_path: list[str],
    autonomy: str,
    agent: bool,
    coerced_values: dict[str, Any],
    arg_specs: dict[str, Any],
    group_specs: dict[str, Any],
    raw_script: dict[str, Any],
) -> RunSpec:
    """Assemble the final RunSpec object."""
    groups = [
        Group(
            name=gname,
            args=gspec.get("args", []),
            exclusive=gspec.get("exclusive", False),
            inclusive=gspec.get("inclusive", False),
            at_least_one=gspec.get("at_least_one", False),
            exactly_one=gspec.get("exactly_one", False),
            condition=gspec.get("condition"),
        )
        for gname, gspec in group_specs.items()
    ]

    runspec = RunSpec(
        __runspec_runnable__=name,
        __runspec_source__=config_path,
        __runspec_command_path__=command_path,
        __runspec_autonomy__=autonomy,
        __runspec_agent__=agent,
        __runspec_spec__=raw_script,
        __runspec_groups__=groups,
    )

    for arg_name, spec in arg_specs.items():
        norm = arg_name.replace("-", "_")
        value, source = coerced_values.get(norm, (None, "default"))

        arg = Arg(
            value=value,
            name=arg_name,
            type=spec.get("type", "str"),
            required=spec.get("required", False),
            default=spec.get("default"),
            description=spec.get("description"),
            options=spec.get("options"),
            range=spec.get("range"),
            multiple=spec.get("multiple", False),
            delimiter=spec.get("delimiter"),
            short=spec.get("short"),
            env=spec.get("env"),
            deprecated=spec.get("deprecated"),
            autonomy=spec.get("autonomy"),
            ui=spec.get("ui"),
            meta=spec.get("meta"),
            position=spec.get("position"),
            source=source,
        )
        runspec._set_arg(arg_name, arg)

    return runspec
