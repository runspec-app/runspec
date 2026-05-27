"""
cli.py — The runspec command-line interface.

Commands:
    runspec init   [--name <name>] [--lang python|typescript|javascript]
                   [--example] [--write-project] [--project-dir <path>]
    runspec local  [--format text|json|mcp|openai|anthropic] [--runnable <name>]
    runspec serve
    runspec jump   [--list-jump-hosts] [<jump_host> [<tool>] [-- tool-args...]]

Top-level options:
    --version, -V    Print the runspec package version and exit
    --help,    -h    Show help for runspec or a subcommand

Help text, args, and examples are driven from runspec/runspec.toml — the
CLI parses --help through parse() against its own bundled spec.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_CLI_CONFIG = Path(__file__).parent / "runspec.toml"


def main() -> None:
    """Entry point for the runspec CLI binary."""
    args = sys.argv[1:]

    # Top-level --version / -V
    if args and args[0] in ("-V", "--version"):
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        try:
            print(f"runspec {_pkg_version('runspec')}")
        except PackageNotFoundError:
            print("runspec (version unknown)")
        return

    # Top-level --help, -h, or no args → dogfood our own runspec.toml
    if not args or args[0] in ("-h", "--help"):
        from runspec.parser import parse as _parse

        _parse(script_name="runspec", argv=["--help"], config_path=_CLI_CONFIG)
        return  # _print_help exits 0

    command = args[0]
    rest = args[1:]

    commands = {
        "init": cmd_init,
        "local": cmd_local,
        "serve": cmd_serve,
        "jump": cmd_jump,
        "env": cmd_env,
    }

    if command not in commands:
        print(f"✗  Unknown command: {command}")
        print(f"   Available commands: {', '.join(commands)}")
        sys.exit(1)

    # Each handler is responsible for parsing its own args.
    # --help is intercepted inside parse() (parser.py) via the spec.
    commands[command](rest)


def cmd_local(args: list[str]) -> None:
    """List discovered runnables with inline validation, or emit schemas."""
    from runspec.parser import parse as _parse

    parsed = _parse(script_name="runspec", argv=["local"] + args, config_path=_CLI_CONFIG)

    fmt = str(parsed.format)
    runnable_filter: str | None = parsed.runnable.value

    discovered = _deduplicate(_discover_installed())

    if runnable_filter:
        discovered = [d for d in discovered if d["runnable"] == runnable_filter]

    if not discovered:
        print("No runspec-aware runnables found in this environment.")
        print("Install a package that declares 'runspec' as a dependency,")
        print("or run 'pip install -e .' from your project directory.")
        return

    if fmt == "text":
        _print_local_text(discovered)
    else:
        bin_dir = Path(sys.executable).parent

        def _has_binary(name: str) -> bool:
            return (bin_dir / name).exists() or (bin_dir / f"{name}.exe").exists()

        callable_only = [d for d in discovered if _has_binary(d["runnable"])]
        if fmt == "json":
            print(json.dumps(callable_only, indent=2, default=str))
        elif fmt in ("mcp", "openai", "anthropic"):
            schema = _emit_all(callable_only, fmt)
            print(json.dumps(schema, indent=2, default=str))
        else:
            print(f"✗  Unknown format: {fmt}")
            print("   Available formats: text, json, mcp, openai, anthropic")
            sys.exit(1)


def cmd_serve(args: list[str]) -> None:
    """Start the MCP stdio server for local runnables."""
    from runspec.parser import parse as _parse
    from runspec.serve import serve

    # parse() against the bundled CLI spec validates args + handles --help.
    # serve takes no parameters; everything is discovered via importlib.metadata.
    _parse(script_name="runspec", argv=["serve"] + args, config_path=_CLI_CONFIG)
    serve()


def cmd_jump(args: list[str]) -> None:
    """List tools on a remote host or run a tool via SSH+MCP."""
    from runspec.parser import parse as _parse

    parsed = _parse(script_name="runspec", argv=["jump"] + args, config_path=_CLI_CONFIG)

    host: str | None = parsed.host.value
    if host is None:
        print("✗  A host is required")
        print("   Usage: runspec jump <host> [<tool>] [-- tool-args...]")
        print("   Example: runspec jump user@prod.example.com")
        sys.exit(1)

    fmt = str(parsed.format)
    bin_flag: str | None = parsed.bin.value
    tool_name: str | None = parsed.tool.value
    tool_argv: list[str] = parsed.tool_args.value or []

    from runspec.jump import _resolve_bin, call_tool, list_tools

    bin_path = _resolve_bin(bin_flag)

    if tool_name is None:
        tools = list_tools(host, bin_path)
        if not tools:
            print(f"No tools found on {host}.")
            return
        if fmt == "json":
            print(json.dumps(tools, indent=2))
            return
        print(f"Tools on {host}:\n")
        for t in tools:
            desc = t.get("description") or ""
            print(f"  {t['name']:<24} {desc}")
        return

    call_tool(host, bin_path, tool_name, tool_argv)


def cmd_env(args: list[str]) -> None:
    """Show the resolved .runspec_env file path and its contents."""
    from runspec.parser import parse as _parse

    parsed = _parse(script_name="runspec", argv=["env"] + args, config_path=_CLI_CONFIG)
    runnable_name: str | None = parsed.runnable.value

    from runspec.env import _parse_dotenv, resolve_env_path_with_source
    from runspec.finder import find_config
    from runspec.loader import load_raw

    raw: dict[str, Any] = {}
    try:
        config_path = find_config(Path.cwd())
        raw = load_raw(config_path)
    except FileNotFoundError:
        pass

    path, source = resolve_env_path_with_source(raw, runnable_name or "")

    if runnable_name:
        print(f"Resolved .runspec_env for '{runnable_name}':")
    else:
        print("Resolved .runspec_env (default):")

    print(f"  Path:   {path}")
    print(f"  Source: {source}")

    if not path.exists():
        print("  Status: not found")
        return

    values = _parse_dotenv(path)

    if not values:
        print("  Status: found (empty)")
        return

    print()
    pad = max(len(k) for k in values)
    for key, value in values.items():
        print(f"  {key:<{pad}} = {value}")


def cmd_init(args: list[str]) -> None:
    """Scaffold a new runnable — config and code stub."""
    from runspec.parser import parse as _parse

    parsed = _parse(script_name="runspec", argv=["init"] + args, config_path=_CLI_CONFIG)

    name_flag: str | None = parsed.name.value
    lang_flag = str(parsed.lang)
    example = bool(parsed.example)
    write_project = bool(parsed.write_project)
    project_dir_arg: str | None = parsed.project_dir.value
    force = bool(parsed.force)

    cwd = Path.cwd()
    pkg_name = _sanitize_name(cwd.name)
    runspec_toml = cwd / "runspec.toml"

    # Safety check: runspec.toml belongs *inside* a package directory, not at
    # the project root. If cwd already has a pyproject.toml, the developer is
    # almost certainly in the wrong place. --force opts out.
    if (cwd / "pyproject.toml").exists() and not force:
        print(f"✗  {cwd}/pyproject.toml already exists.")
        print("   `runspec init` scaffolds inside a package directory, not at the project root.")
        print("   cd into your package directory first (e.g. `cd mypackage/` then re-run).")
        print("   Use --force to scaffold here anyway.")
        sys.exit(1)

    if example:
        if name_flag:
            print("  ℹ  --name ignored with --example (fixed names: clean, scan)")
        runnable_name = "clean"
    else:
        runnable_name = name_flag or pkg_name

    _init_runspec_toml(runspec_toml, runnable_name, example=example)
    _init_code_stub(cwd, runnable_name, lang_flag, example=example)

    if write_project:
        project_root = (cwd / (project_dir_arg or "..")).resolve()
        _init_package_init(cwd)
        _init_pyproject(project_root, runnable_name, pkg_name, example=example)
        _init_gitignore(project_root)
        _init_claude_md(project_root, pkg_name)
        _print_next_steps(install_from=project_dir_arg or "..", example=example)
    else:
        if example:
            _print_pyproject_snippet_example(pkg_name)
        else:
            _print_pyproject_snippet(runnable_name, pkg_name)
        _print_next_steps(install_from=None, example=example)


def _sanitize_name(raw: str) -> str:
    """Convert a directory name into a valid TOML key."""
    import re

    s = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    return s or "myscript"


def _init_runspec_toml(path: Path, name: str, example: bool = False) -> None:
    if path.exists():
        print(f"✗  {path.name} already exists — already initialized")
        print(f"   Edit {path.name} directly to add more runnables.")
        sys.exit(1)

    schema_line = "#:schema https://raw.githubusercontent.com/JasonFinestone/runspec/main/schema/runspec.schema.json\n\n"
    content = schema_line + (
        _build_example_toml()
        if example
        else (f'[{name}]\ndescription = "Describe what {name} does"\nautonomy    = "confirm"\n\n[{name}.args]\n# example = {{type = "str", description = "An example argument"}}\n')
    )
    _write_and_verify(path, content, None)
    if example:
        print("  ✓  Created runspec.toml with [clean] and [scan] runnables")
    else:
        print(f"  ✓  Created runspec.toml with [{name}] runnable")


def _build_example_toml() -> str:
    return (
        "# Jump hosts — `runspec jump <alias>` opens SSH+MCP to that host and runs\n"
        "# a tool there. Add one entry per host. Optional fields:\n"
        "#   user, port, ssh-key, bin   — direct overrides (bin env: RUNSPEC_JUMP_BIN)\n"
        "#   use-ssh-config = false      — ignore ~/.ssh/config (passes -F /dev/null)\n"
        '#   ssh-options = ["X=Y", ...]  — extra -o KEY=VALUE options passed to ssh\n'
        "#\n"
        "# Note: SSH runs commands in a non-login shell, so it doesn't source\n"
        "# ~/.bashrc / ~/.profile. If `runspec` isn't on the remote's default\n"
        "# PATH (e.g. you installed into a venv), set bin to the full path.\n"
        "[config.jump-hosts.localhost]\n"
        'host = "localhost"\n'
        '# bin = "/path/to/.venv/bin/runspec"   # set when runspec isn\'t on the remote shell PATH\n'
        "\n"
        "# `meta = {...}` on args is a pass-through table for project-defined\n"
        "# metadata — runspec never interprets it. Use it for unit hints, impact\n"
        "# tags, owner, runbook URLs, anything your tools or UI want to surface.\n"
        "\n"
        "[clean]\n"
        'description = "Find and optionally delete stale temporary files in a directory"\n'
        'autonomy    = "confirm"\n'
        'serve       = ["remote"]   # only exposed when runspec serve is running on a remote host\n'
        "\n"
        "[clean.args]\n"
        'directory  = {type = "path",   description = "Directory to scan",                            default = "."}\n'
        'pattern    = {type = "str",    description = "Glob pattern to match",                        default = "*.tmp"}\n'
        'older_than = {type = "int",    description = "Only match files older than N days",           default = 7,     meta = {unit = "days"}}\n'
        'format     = {type = "choice", description = "Output format", options = ["text", "json"],    default = "text"}\n'
        'delete     = {type = "flag",   description = "Delete matched files",                         default = false, meta = {impact = "destructive"}}\n'
        "\n"
        "[scan]\n"
        'description = "Scan for stale temporary files and report what clean would delete"\n'
        'autonomy    = "autonomous"\n'
        'output      = "json"\n'
        'serve       = ["remote"]   # only exposed when runspec serve is running on a remote host\n'
        "\n"
        "[scan.args]\n"
        'directory  = {type = "path",   description = "Directory to scan",                  default = "."}\n'
        'pattern    = {type = "str",    description = "Glob pattern to match",              default = "*.tmp"}\n'
        'older_than = {type = "int",    description = "Only match files older than N days", default = 7, meta = {unit = "days"}}\n'
        'format     = {type = "choice", description = "Output format", options = ["text", "json"], default = "json"}\n'
    )


_EXAMPLE_PYTHON_STUB = """\
import json
import time

from runspec import parse


def main():
    args = parse()

    cutoff = time.time() - args.older_than * 86400
    matches = [p for p in args.directory.glob(args.pattern) if p.is_file() and p.stat().st_mtime < cutoff]

    if not matches:
        print(f"No '{args.pattern}' files older than {args.older_than} days found in {args.directory}.")
        return

    if args.format == "json":
        data = [
            {"path": str(p), "size": p.stat().st_size, "days_old": int((time.time() - p.stat().st_mtime) / 86400)}
            for p in matches
        ]
        print(json.dumps(data, indent=2))
    else:
        print(f"Found {len(matches)} file(s) matching '{args.pattern}' older than {args.older_than} days:")
        print()
        for p in matches:
            days = int((time.time() - p.stat().st_mtime) / 86400)
            print(f"  {p}  ({p.stat().st_size:,} bytes, {days}d old)")

    if args.delete:
        # Agents must declare autonomy='autonomous' to invoke a destructive
        # action. Human invocation (--delete typed on the CLI) is the human's
        # explicit confirmation; agents need the equivalent declared in the spec.
        if args.runspec_agent and args.runspec_autonomy != "autonomous":
            raise SystemExit("✗ --delete requires autonomy='autonomous' for agent invocation")
        for p in matches:
            p.unlink()
        print()
        print(f"Deleted {len(matches)} file(s).")


if __name__ == "__main__":
    main()
"""

_SCAN_PYTHON_STUB = """\
import json
import time

from runspec import parse


def main():
    args = parse()

    cutoff = time.time() - args.older_than * 86400
    matches = [p for p in args.directory.glob(args.pattern) if p.is_file() and p.stat().st_mtime < cutoff]

    if args.format == "json":
        data = [
            {"path": str(p), "size": p.stat().st_size, "days_old": int((time.time() - p.stat().st_mtime) / 86400)}
            for p in matches
        ]
        print(json.dumps(data, indent=2))
    else:
        if not matches:
            print(f"No '{args.pattern}' files older than {args.older_than} days found in {args.directory}.")
            return
        print(f"Found {len(matches)} file(s) matching '{args.pattern}' older than {args.older_than} days:")
        print()
        for p in matches:
            days = int((time.time() - p.stat().st_mtime) / 86400)
            print(f"  {p}  ({p.stat().st_size:,} bytes, {days}d old)")


if __name__ == "__main__":
    main()
"""

_CODE_STUB_TEMPLATES: dict[str, tuple[str, str]] = {
    "python": (
        ".py",
        'from runspec import parse\n\n\ndef main():\n    args = parse()\n    # your logic here\n\n\nif __name__ == "__main__":\n    main()\n',
    ),
    "typescript": (
        ".ts",
        "import { parse } from 'runspec';\n\nfunction main(): void {\n  const args = parse();\n  // your logic here\n}\n\nmain();\n",
    ),
    "javascript": (
        ".js",
        "const { parse } = require('runspec');\n\nfunction main() {\n  const args = parse();\n  // your logic here\n}\n\nmain();\n",
    ),
}


def _init_code_stub(directory: Path, name: str, lang: str, example: bool = False) -> None:
    if example and lang != "python":
        print(f"  ℹ  --example is only available for Python — using minimal stub for {lang}")
        example = False

    if lang not in _CODE_STUB_TEMPLATES:
        print(f"✗  Unknown --lang: {lang}")
        print("   Supported: python, typescript, javascript")
        sys.exit(1)

    if example:
        for stub_name, content in [("clean", _EXAMPLE_PYTHON_STUB), ("scan", _SCAN_PYTHON_STUB)]:
            stub_path = directory / (stub_name + ".py")
            if stub_path.exists():
                print(f"  ℹ  {stub_path.name} already exists — skipped")
            else:
                stub_path.write_text(content, encoding="utf-8")
                print(f"  ✓  Created {stub_path.name}")
    else:
        ext, content = _CODE_STUB_TEMPLATES[lang]
        stub_path = directory / (name + ext)
        if stub_path.exists():
            print(f"  ℹ  {stub_path.name} already exists — skipped")
        else:
            stub_path.write_text(content, encoding="utf-8")
            print(f"  ✓  Created {stub_path.name}")


def _init_package_init(directory: Path) -> None:
    init_path = directory / "__init__.py"
    if init_path.exists():
        print("  ℹ  __init__.py already exists — skipped")
    else:
        init_path.write_text("", encoding="utf-8")
        print("  ✓  Created __init__.py")


def _init_gitignore(project_root: Path) -> None:
    path = project_root / ".gitignore"
    if path.exists():
        print("  ℹ  .gitignore already exists — skipped")
        return
    path.write_text(
        "# Python\n__pycache__/\n*.py[cod]\n*.egg-info/\n*.egg\ndist/\nbuild/\n.venv/\nvenv/\n.env\n.env.*\n\n# Editor\n.vscode/\n.idea/\n*.swp\n*.swo\n*~\n\n# OS\n.DS_Store\nThumbs.db\n",
        encoding="utf-8",
    )
    print("  ✓  Created .gitignore")


def _init_claude_md(project_root: Path, pkg_name: str) -> None:
    path = project_root / "CLAUDE.md"
    if path.exists():
        print("  ℹ  CLAUDE.md already exists — skipped")
        return
    path.write_text(
        "# AI Context\n"
        "\n"
        "This project uses [runspec](https://github.com/JasonFinestone/runspec) for CLI interfaces.\n"
        "\n"
        "## Key files\n"
        f"- `{pkg_name}/runspec.toml` — defines runnables: args, types, autonomy levels\n"
        f"- `{pkg_name}/<name>.py` — one module per runnable, each with a `main()` entry point\n"
        "- `pyproject.toml` — wires entry points under `[project.scripts]`\n"
        "\n"
        "## Adding a runnable\n"
        f"1. Add `[name]` section to `{pkg_name}/runspec.toml` with args and autonomy\n"
        f"2. Create `{pkg_name}/name.py` with `main()` calling `runspec.parse()`\n"
        '3. Add `name = "{pkg}.name:main"` to `[project.scripts]` in `pyproject.toml`\n'
        "4. Run `pip install -e .` then `runspec local` to validate\n"
        "\n"
        "## Autonomy levels\n"
        "| Level | Use for |\n"
        "|---|---|\n"
        "| `confirm` | Destructive operations — agent confirms with human before running |\n"
        "| `autonomous` | Read-only operations — agent runs freely |\n"
        "| `supervised` | Agent runs, human reviews output before it is acted on |\n",
        encoding="utf-8",
    )
    print("  ✓  Created CLAUDE.md")


def _init_pyproject(project_root: Path, runnable_name: str, pkg_name: str, example: bool = False) -> None:
    pyproject = project_root / "pyproject.toml"

    if pyproject.exists():
        print(f"  ℹ  {pyproject} already exists — add this entry manually:")
        print("       [project.scripts]")
        if example:
            print(f'       clean = "{pkg_name}.clean:main"')
            print(f'       scan  = "{pkg_name}.scan:main"')
        else:
            print(f'       {runnable_name} = "{pkg_name}.{runnable_name}:main"')
    else:
        pyproject.write_text(_build_pyproject(runnable_name, pkg_name, example=example), encoding="utf-8")
        print(f"  ✓  Created {pyproject}")


def _build_pyproject(runnable_name: str, pkg_name: str, example: bool = False) -> str:
    scripts = f'clean = "{pkg_name}.clean:main"\nscan  = "{pkg_name}.scan:main"\n' if example else f'{runnable_name} = "{pkg_name}.{runnable_name}:main"\n'
    return (
        f"[project]\n"
        f'name            = "{runnable_name}"\n'
        f'version         = "0.1.0"\n'
        f'description     = ""\n'
        f'requires-python = ">=3.10"\n'
        f'dependencies    = ["runspec"]\n'
        f"\n"
        f"[project.optional-dependencies]\n"
        f'dev = ["pytest", "ruff"]\n'
        f"\n"
        f"[project.scripts]\n"
        f"{scripts}"
        f"\n"
        f"[tool.hatch.build.targets.wheel]\n"
        f'packages = ["{pkg_name}"]\n'
        f"\n"
        f"[build-system]\n"
        f'requires      = ["hatchling"]\n'
        f'build-backend = "hatchling.build"\n'
    )


def _print_pyproject_snippet(runnable_name: str, pkg_name: str) -> None:
    entry_point = f"{pkg_name}.{runnable_name}:main"
    print()
    print("  To register this runnable, add to your pyproject.toml:")
    print()
    print("    [project.scripts]")
    print(f'    {runnable_name} = "{entry_point}"')


def _print_pyproject_snippet_example(pkg_name: str) -> None:
    print()
    print("  To register these runnables, add to your pyproject.toml:")
    print()
    print("    [project.scripts]")
    print(f'    clean = "{pkg_name}.clean:main"')
    print(f'    scan  = "{pkg_name}.scan:main"')


def _print_next_steps(install_from: str | None, example: bool = False) -> None:
    print()
    print("  Next steps:")
    if install_from is None:
        print("    1. Move both files inside your package directory before publishing")
        print("    2. Install before running:")
        print("         pip install -e .")
        print("         uv sync              # uv")
        print("         poetry install       # poetry")
        print("    3. Run 'runspec local' to validate your setup")
    else:
        print("    1. Install before running:")
        print(f"         pip install -e {install_from}")
        print(f"         uv sync              # uv — run from {install_from}")
        print(f"         poetry install       # poetry — run from {install_from}")
        print("    2. Run 'runspec local' to validate your setup")

    if example:
        print()
        print("  Demo (stage some stale files first):")
        print("    touch -t 202401010000 report.tmp cache.tmp session.tmp")
        print()
        print("    scan                    # read-only — lists stale files")
        print("    scan --format json      # agent-ready output")
        print("    clean --delete          # destructive — refused unless agent has autonomy='autonomous'")


def _write_and_verify(path: Path, content: str, original: str | None) -> None:
    """Write content, verify it parses as valid TOML, restore on failure."""
    path.write_text(content, encoding="utf-8")
    try:
        _load_toml_file(path)
    except Exception as e:
        if original is not None:
            path.write_text(original, encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        print("✗  Generated invalid TOML — this is a bug, please report it")
        print(f"   {e}")
        sys.exit(1)


def _load_toml_file(path: Path) -> dict[str, Any]:
    """Read a TOML file using the stdlib or tomli fallback."""
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib  # type: ignore[no-redef]
    with open(path, "rb") as f:
        return tomllib.load(f)


# ── Schema builders ───────────────────────────────────────────────────────────


def _build_schema(name: str, script: dict[str, Any], fmt: str) -> dict[str, Any]:
    """Build a tool schema for a script in the requested format."""
    properties: dict[str, Any] = {}
    required_args: list[str] = []

    for arg_name, arg in script.get("args", {}).items():
        prop = _arg_to_json_schema(arg)
        properties[arg_name] = prop
        if arg.get("required"):
            required_args.append(arg_name)

    schema: dict[str, Any] = {
        "name": name,
        "description": script.get("description") or "",
        "x-autonomy": script.get("autonomy", "confirm"),
        "x-output": script.get("output", "text"),
        "inputSchema": {
            "type": "object",
            "properties": properties,
        },
    }

    if required_args:
        schema["inputSchema"]["required"] = required_args

    if script.get("autonomy_reason"):
        schema["x-autonomy-reason"] = script["autonomy_reason"]

    return schema


def _arg_to_json_schema(arg: dict[str, Any]) -> dict[str, Any]:
    """Convert a runspec arg spec to a JSON Schema property."""
    type_map = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "flag": "boolean",
        "path": "string",
        "choice": "string",
    }

    arg_type = arg.get("type", "str")

    # rest type: a list of strings captured after `--`
    if arg_type == "rest":
        prop: dict[str, Any] = {"type": "array", "items": {"type": "string"}}
    else:
        prop = {"type": type_map.get(arg_type, "string")}

    if arg.get("description"):
        prop["description"] = arg["description"]

    if arg.get("default") is not None:
        prop["default"] = arg["default"]

    if arg.get("options"):
        prop["enum"] = arg["options"]

    if arg.get("range"):
        min_val, max_val = arg["range"]
        prop["minimum"] = min_val
        prop["maximum"] = max_val

    if arg.get("multiple"):
        prop = {"type": "array", "items": prop}

    # Pass-through metadata — emit under x-meta to keep the user's dict intact
    # without colliding with built-in JSON Schema keywords.
    if arg.get("meta"):
        prop["x-meta"] = arg["meta"]

    return prop


# ── Discovery helpers ─────────────────────────────────────────────────────────


def _discover_installed() -> list[dict[str, Any]]:
    """Find runspec-aware packages in the current Python environment via importlib.metadata."""
    import importlib.metadata as meta

    discovered: list[dict[str, Any]] = []

    for dist in meta.distributions():
        if not _requires_runspec(dist):
            continue
        try:
            items = _check_dist_files(dist)
            if not items:
                items = _check_editable_source(dist)
            discovered.extend(items)
        except Exception:
            continue

    return discovered


def _requires_runspec(dist: Any) -> bool:
    """Return True if this distribution lists runspec as a dependency."""
    import re

    requires = dist.requires
    if not requires:
        return False
    for req in requires:
        name = re.split(r"[\s\[;>=<!]", req.strip())[0]
        if name.lower().replace("-", "_") == "runspec":
            return True
    return False


def _check_dist_files(dist: Any) -> list[dict[str, Any]]:
    """Strategy 1: look for runspec.toml shipped as package data."""
    from runspec.loader import load_raw

    if dist.files is None:
        return []

    for f in dist.files:
        if f.name == "runspec.toml":
            try:
                config_path = Path(str(f.locate())).resolve()
                raw = load_raw(config_path)
                if not raw["runnables"]:
                    return []
                config_autonomy = raw.get("config", {}).get("autonomy")
                return [{"source": str(config_path), "runnable": name, "spec": spec, "config_autonomy": config_autonomy} for name, spec in raw["runnables"].items()]
            except Exception:
                pass

    return []


def _check_editable_source(dist: Any) -> list[dict[str, Any]]:
    """Strategy 2: editable install — find source via direct_url.json."""
    import json as _json
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    from runspec.loader import load_raw

    if dist.files is None:
        return []

    direct_url_file = next((f for f in dist.files if f.name == "direct_url.json"), None)
    if direct_url_file is None:
        return []

    try:
        data = _json.loads(direct_url_file.locate().read_text(encoding="utf-8"))
    except Exception:
        return []

    if not data.get("dir_info", {}).get("editable"):
        return []

    url = data.get("url", "")
    if not url.startswith("file://"):
        return []

    source_dir = Path(url2pathname(urlparse(url).path)).resolve()
    if not source_dir.is_dir():
        return []

    discovered: list[dict[str, Any]] = []
    try:
        for subdir in sorted(source_dir.iterdir()):
            if not subdir.is_dir() or subdir.name.startswith("."):
                continue
            candidate = subdir / "runspec.toml"
            if not candidate.exists():
                continue
            try:
                raw = load_raw(candidate)
                config_autonomy = raw.get("config", {}).get("autonomy")
                for name, spec in raw["runnables"].items():
                    discovered.append({"source": str(candidate), "runnable": name, "spec": spec, "config_autonomy": config_autonomy})
            except Exception:
                continue
    except PermissionError:
        pass

    return discovered


def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicate (resolved_source_path, runnable_name) pairs."""
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = (str(Path(item["source"]).resolve()), item["runnable"])
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _emit_all(discovered: list[dict[str, Any]], fmt: str) -> dict[str, Any]:
    """Emit all discovered runnables as a unified schema."""
    tools = [_build_schema(item["runnable"], item["spec"], fmt) for item in discovered]
    if fmt == "mcp":
        return {"tools": tools}
    return {tool["name"]: tool for tool in tools}


def _print_local_text(discovered: list[dict[str, Any]]) -> None:
    """Print discovered runnables with inline validation warnings."""
    from pathlib import Path

    bin_dir = Path(sys.executable).parent
    by_source: dict[str, list[dict[str, Any]]] = {}
    for item in discovered:
        by_source.setdefault(item["source"], []).append(item)

    errors: list[str] = []
    warnings: list[str] = []

    print(f"Found {len(discovered)} runspec runnable(s):\n")
    for source, items in by_source.items():
        print(f"  {source}")
        for item in items:
            name = item["runnable"]
            runnable = item["spec"]
            desc = runnable.get("description") or ""
            autonomy = runnable.get("autonomy") or "confirm"

            has_ep = (bin_dir / name).exists() or (bin_dir / f"{name}.exe").exists()
            callable_marker = "" if has_ep else "  [not callable]"
            print(f"    {name:<24} {desc[:48]:<50}  [{autonomy}]{callable_marker}")

            if not has_ep:
                errors.append(f"'{name}' entry point not registered — add to [project.scripts] in pyproject.toml and re-run pip install")

            if not runnable.get("description"):
                warnings.append(f"'{name}' has no description — agents won't know what it does")
            if not runnable.get("autonomy"):
                warnings.append(f"'{name}' autonomy not declared — defaulting to 'confirm'")
            for arg_name, arg in runnable.get("args", {}).items():
                if not arg.get("description") and arg.get("required"):
                    warnings.append(f"'{name}.{arg_name}' is required but has no description")
            run_as = runnable.get("run_as")
            if isinstance(run_as, dict):
                from runspec.serve import _validate_run_as_patterns

                for err in _validate_run_as_patterns(run_as):
                    errors.append(f"'{name}' run_as: {err}")
        print()

    if warnings or errors:
        print("Issues:\n")
        for msg in warnings:
            print(f"  ℹ  {msg}")
        for msg in errors:
            print(f"  ✗  {msg}")
        print()

    print("Run 'runspec local --format mcp' to emit MCP tool schemas.")

    if errors:
        sys.exit(1)
