"""Is a person reading stdout? The one check for text-vs-JSON output.

After ``parse()``, runspec's logging replaces ``sys.stdout`` with a tee that
copies printed lines into the run log, and the tee's ``isatty()`` is always
``False``. So ``sys.stdout.isatty()`` can't tell a terminal from a pipe once a
runnable has parsed its args. These helpers ask the real stdout
(``sys.__stdout__``) instead.

Most runnables want :attr:`runspec.RunSpec.runspec_text_output`, which also
says no when an agent called the runnable.
"""

from __future__ import annotations

import sys


def stdout_is_terminal() -> bool:
    """True when the process's real stdout is a terminal.

    Works before and after ``parse()``: it reads ``sys.__stdout__``, which the
    logging tee never replaces. False when stdout is a pipe, a file, closed or
    missing (a windowed app on Windows has no stdout at all).
    """
    stream = sys.__stdout__
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (ValueError, OSError):  # closed, or a detached handle
        return False
