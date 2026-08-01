"""The ``fs`` runnable folded into runspec-linux.

The filesystem is part of the operating system, so the Linux pack ships the
filesystem runnable directly (backed by the dependency-free ``runspec-fs-core``)
rather than leaving it to the separate ``runspec-fs`` package. Installing
runspec-linux therefore gives a runspec-console agent local *and* remote
filesystem access on every fleet host — one verb per Filesystem MCP tool,
flattened by ``runspec serve`` into leaf ``fs_<verb>`` tools.

The code mirrors ``runspec-fs`` (``cli`` + runspec-free ``_dispatch`` /
``_worker``); all logic lives in ``runspec_fs_core``.
"""
