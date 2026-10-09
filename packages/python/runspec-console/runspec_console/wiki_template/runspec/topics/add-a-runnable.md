---
type: "Topic"
title: "add a runnable"
description: "To add a new runnable: pin down its purpose, name, args, subcommands, autonomy and side effects; add a [name] section to the package's runspec.toml; write a thin main() that calls runspec.parse() and…"
tags: [runspec, topic]
status: stable
sources:
  - id: S1
    resource: "https://runspec.app/authoring/"
  - id: S2
    resource: "https://runspec.app/skills/add-runnable.md"
generated:
  by: runspec-console-seed/27a536625a1f
  at: "2026-10-09"
---
# add a runnable

## How to

To add a new runnable: pin down its purpose, name, args, subcommands, autonomy and side effects; add a [name] section to the package's runspec.toml; write a thin main() that calls runspec.parse() and hands plain values to a worker function; wire an entry point with exactly the same name; pip install -e .; then run runspec local and runspec test. Guide: https://runspec.app/authoring/

In runnable code, parse() returns a RunSpec and args.name is an Arg: read args.name.value for the value (if args.flag: raises on purpose). Test with runspec.testing.ParseHarness rather than faking args, and let errors propagate instead of wrapping parse() in try/except.

Claude Code can scaffold a runnable for you with the add-runnable skill (download: https://runspec.app/skills/add-runnable.md); it interviews you for the interface, then writes the TOML section, code stub, entry point and a test. mcp-to-runnable ports an existing MCP server's tools into a runnable package.

To add an existing runnable package rather than write one, open the Marketplace tab (or ask the agent to search_marketplace), then install it into the console's venv or a host's venvs; on the command line it is just pip install <package> into the venv. New runnables appear after the next discovery.

## See also

- [runspec](/runspec/apps/runspec.md)
- [marketplace](/runspec/features/marketplace.md)
- [claude code](/runspec/topics/claude-code.md)
