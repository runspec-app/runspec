"""cli.py — ``runspec-tui``: open the Forms view for this venv's runnables."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="runspec-tui",
        description="Fill in and run the runspec runnables installed in this venv, in a terminal form.",
    )
    parser.add_argument("--all", action="store_true", help="also list runnables marked discoverable = false")
    parser.add_argument("--schedules", action="store_true", help="open on the Schedules screen (needs runspec-scheduler in this venv)")
    parser.add_argument(
        "--theme",
        default=os.environ.get("RUNSPEC_TUI_THEME", ""),
        help="a Textual theme name, e.g. nord, gruvbox, textual-light (default: textual-dark; env RUNSPEC_TUI_THEME)",
    )
    parser.add_argument("--version", action="version", version=f"runspec-tui {__version__}")
    args = parser.parse_args(argv)

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.exit(2, "runspec-tui needs an interactive terminal. To run a runnable from a script, use `runspec exec <name> -- …`.\n")

    from . import schedules
    from .app import RunspecTUI

    binary = schedules.scheduler_binary()
    caller = None
    if binary is not None:
        owner, user = schedules.owner_and_user()
        caller = schedules.Caller(binary, owner, user)
    elif args.schedules:
        parser.exit(2, "runspec-scheduler isn't installed in this venv (pip install runspec-scheduler).\n")

    app = RunspecTUI(include_hidden=args.all, scheduler=caller, open_schedules=args.schedules)
    if args.theme:
        if args.theme not in app.available_themes:
            parser.exit(2, f"unknown theme {args.theme!r}; available: {', '.join(sorted(app.available_themes))}\n")
        app.theme = args.theme
    app.run()
