"""The ``fs`` runnable folded into runspec-windows.

The filesystem is part of the operating system, so the Windows pack ships the
filesystem runnable directly (backed by the dependency-free ``runspec-fs-core``)
rather than leaving it to the separate ``runspec-fs`` package. Installing
runspec-windows therefore gives a runspec-console agent local *and* remote
filesystem access on every fleet host — one verb per Filesystem MCP tool,
flattened by ``runspec serve`` into leaf ``fs_<verb>`` tools.

The two inherited ``--run-as`` / ``--become-method`` global args are POSIX-only
(sudo/su); on Windows ``escalation_supported()`` is False and a ``--run-as``
request raises a clear error — the verbs run in-process as the logged-in user.
"""
