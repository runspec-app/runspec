"""
parser.py — Main entry point. Builds a RunSpec from sys.argv.

Orchestrates: find → load → infer → parse argv → validate → coerce → build RunSpec
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from typing import Any

from runspec import errors
from runspec.env import apply_env_file
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
        config_path: Override config file location. If None, resolution order is:
                     RUNSPEC_CONFIG env var → walk up from the caller's
                     package directory → walk up from cwd. The caller-relative
                     walk is what makes installed entry points work from any
                     working directory.

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
    # 1. Find config — explicit arg > RUNSPEC_CONFIG > caller's package > cwd.
    # The caller-relative walk locates runspec.toml shipped inside an installed
    # package directory (e.g. .../site-packages/mypkg/runspec.toml), so entry
    # points work no matter where the user runs them from.
    if config_path is None:
        env_config = os.environ.get("RUNSPEC_CONFIG")
        config_path = Path(env_config) if env_config else find_config(caller=_detect_caller_file())

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

    # 3.5. Load .runspec_env file into os.environ (before _apply_env so
    # RUNSPEC_<RUNNABLE>_ARG_* vars in the file feed into arg resolution)
    runspec_env_data, runspec_env_applied = apply_env_file(raw, name)

    # 4. Infer defaults for the script
    raw_script = infer_script(raw["runnables"][name], config["autonomy_default"])

    # 4.5. Auto-inject --debug flag when [config.logging] is present.
    # Without --debug: stdout = INFO+, stderr = WARNING+ (file always = DEBUG).
    # With --debug:    stdout also includes DEBUG records and tracebacks.
    if config.get("logging") and "debug" not in raw_script["args"]:
        raw_script["args"]["debug"] = {
            "name": "debug",
            "type": "flag",
            "options": None,
            "default": False,
            "required": False,
            "description": "Show DEBUG records and tracebacks on stdout.",
            "multiple": False,
            "delimiter": None,
            "short": None,
            "env": None,
            "deprecated": None,
            "autonomy": None,
            "ui": None,
            "meta": None,
            "position": None,
            "range": None,
        }

    # Auto-inject --no-summary when [config.logging] is present. Suppresses the
    # per-run summary record/stderr line for that one invocation.
    if config.get("logging") and "no-summary" not in raw_script["args"]:
        raw_script["args"]["no-summary"] = {
            "name": "no-summary",
            "type": "flag",
            "options": None,
            "default": False,
            "required": False,
            "description": "Suppress the per-run summary record and stderr line.",
            "multiple": False,
            "delimiter": None,
            "short": None,
            "env": None,
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
        _print_help(name, raw_script, command_path)
        sys.exit(0)

    # 7. Parse argv into raw values
    parsed_values = _parse_argv(argv_list, raw_script["args"])

    # Build initial source map — anything non-None after argv parsing came from CLI
    sources: dict[str, str] = {norm: "cli" for norm, val in parsed_values.items() if val is not None}

    # 8. Apply env var fallbacks
    parsed_values, sources = _apply_env(parsed_values, sources, raw_script["args"], name, runspec_env_applied)

    # 9. Apply defaults
    parsed_values, sources = _apply_defaults(parsed_values, sources, raw_script["args"])

    # 10. Pass 1 — validate individual args
    arg_errors = validate_args(parsed_values, raw_script["args"])
    raise_if_errors(arg_errors)

    # 11. Pass 2 — validate groups
    group_errors = validate_groups(parsed_values, raw_script["groups"])
    raise_if_errors(group_errors)

    # 12. Coerce values to native Python types
    coerced_values = _coerce_values(parsed_values, sources, raw_script["args"])

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
        runspec_env_data=runspec_env_data,
    )

    # 16. Configure logging (no-op when [config.logging] absent)
    debug = False
    no_summary = False
    if config.get("logging"):
        pair = coerced_values.get("debug")  # stores (value, source) tuples
        if pair and pair[0] is not None:
            debug = bool(pair[0])
        ns_pair = coerced_values.get("no_summary")  # _coerce_values normalises dashes
        if ns_pair and ns_pair[0] is not None:
            no_summary = bool(ns_pair[0])

    # Serialize invocation args for the run_summary audit record.
    # Excludes auto-injected flags (debug, no_summary) from the logged args.
    _auto_args = {"debug", "no_summary"}
    invocation_args = {k: {"value": str(arg.value), "source": arg.source} for k, arg in runspec_obj._args.items() if arg.value is not None and k not in _auto_args}

    try:
        configure_logging(
            config.get("logging"),
            runnable_name=name,
            debug=debug,
            no_summary=no_summary,
            autonomy=autonomy,
            agent=agent,
            command_path=command_path,
            invocation_args=invocation_args,
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


def _print_help(name: str, script: dict[str, Any], command_path: list[str] | None = None) -> None:
    """Print a human-readable help message for a runnable and exit."""
    full_name = " ".join([name, *(command_path or [])])
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

    def _arg_token(spec: dict[str, Any]) -> str:
        # Choices render their options inline; other types render as <type>.
        opts = spec.get("options")
        if opts:
            return "<" + "|".join(str(o) for o in opts) + ">"
        return f"<{spec.get('type', 'str')}>"

    # ── Usage line ────────────────────────────────────────────────────────────
    # Order: name [flags] [positionals] [<command>] [-- <rest>...]
    # Rest stays last because '--' terminates argument parsing.
    usage_parts = [full_name]
    for arg_name, spec in flag_args:
        flag = f"--{arg_name}"
        if spec.get("type") == "flag":
            usage_parts.append(f"[{flag}]")
        elif spec.get("required"):
            usage_parts.append(f"{flag} {_arg_token(spec)}")
        else:
            usage_parts.append(f"[{flag} {_arg_token(spec)}]")
    for _, arg_name, spec in positional_args:
        if spec.get("required"):
            usage_parts.append(f"<{arg_name}>")
        else:
            usage_parts.append(f"[<{arg_name}>]")
    if commands:
        usage_parts.append("<command>")
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
    print("\n  -h, --help    Show this message and exit")


def _infer_from_argv() -> str:
    """Infer script name from sys.argv[0]."""
    return Path(sys.argv[0]).stem if sys.argv else "unknown"


def _detect_caller_file() -> Path | None:
    """
    Return the file path of the first frame on the call stack whose module
    lives outside the runspec package. That's the user code that invoked
    parse() — its directory is where the bundled runspec.toml lives.

    Returns None when no such frame can be identified (e.g. invoked from
    a REPL or compiled extension with no __file__).
    """
    frame = inspect.currentframe()
    if frame is None:
        return None
    try:
        frame = frame.f_back  # skip _detect_caller_file itself
        while frame is not None:
            module = inspect.getmodule(frame)
            name = getattr(module, "__name__", "") if module else ""
            if name and not name.startswith("runspec.") and name != "runspec":
                file = frame.f_globals.get("__file__")
                if file:
                    return Path(file).resolve()
            frame = frame.f_back
    finally:
        del frame
    return None


def _resolve_subcommand(
    raw_script: dict[str, Any],
    argv: list[str],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """
    Walk into nested subcommands as long as argv[0] matches a declared
    command name at the current depth. Returns the deepest resolved
    script spec, the full command path, and the remaining argv.
    """
    path: list[str] = []
    while argv:
        commands = raw_script.get("commands", {})
        if not commands or argv[0] not in commands:
            break
        path.append(argv[0])
        raw_script = commands[argv[0]]
        argv = argv[1:]
    return raw_script, path, argv


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
    sources: dict[str, str],
    arg_specs: dict[str, Any],
    runnable_name: str,
    runspec_env_applied: frozenset[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Apply environment variable fallbacks where values are still None.

    Resolution order:
      1. RUNSPEC_<RUNNABLE>_ARG_<ARGNAME>  — automatic for every arg, user-settable
      2. env aliases                        — developer-declared list, for CI/Ansible/etc
    """
    runnable_prefix = runnable_name.upper().replace("-", "_")
    result = dict(parsed)
    new_sources = dict(sources)
    for name, spec in arg_specs.items():
        norm = name.replace("-", "_")
        if result.get(norm) is not None:
            continue
        # Tier 2a: automatic RUNSPEC_<RUNNABLE>_ARG_<ARGNAME>
        auto_key = f"RUNSPEC_{runnable_prefix}_ARG_{name.upper().replace('-', '_')}"
        env_val = os.environ.get(auto_key)
        if env_val is not None:
            result[norm] = env_val
            new_sources[norm] = "runspec_env" if auto_key in runspec_env_applied else "env"
            continue
        # Tier 2b: developer-declared aliases
        for alias in spec.get("env") or []:
            env_val = os.environ.get(alias)
            if env_val is not None:
                result[norm] = env_val
                new_sources[norm] = "runspec_env" if alias in runspec_env_applied else "env"
                break
    return result, new_sources


def _apply_defaults(
    parsed: dict[str, Any],
    sources: dict[str, str],
    arg_specs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    """Apply spec defaults where values are still None."""
    result = dict(parsed)
    new_sources = dict(sources)
    for name, spec in arg_specs.items():
        norm = name.replace("-", "_")
        if result.get(norm) is None and spec.get("default") is not None:
            result[norm] = spec["default"]
            new_sources[norm] = "spec_default"
    return result, new_sources


def _coerce_values(
    parsed: dict[str, Any],
    sources: dict[str, str],
    arg_specs: dict[str, Any],
) -> dict[str, Any]:
    """Coerce all resolved values to native Python types."""
    result: dict[str, Any] = {}
    for name, spec in arg_specs.items():
        norm = name.replace("-", "_")
        value = parsed.get(norm)
        if value is None:
            result[norm] = (None, "not_set")
            continue
        source = sources.get(norm, "not_set")
        try:
            coerced = coerce(value, spec)
        except (ValueError, TypeError) as e:
            raise errors.RunSpecError(f"✗  {e}") from e
        result[norm] = (coerced, source)
    return result


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
    runspec_env_data: dict[str, str] | None = None,
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
        _runspec_env=runspec_env_data or {},
    )

    for arg_name, spec in arg_specs.items():
        norm = arg_name.replace("-", "_")
        value, source = coerced_values.get(norm, (None, "not_set"))

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
