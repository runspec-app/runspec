"""runspec-wireshark — packet captures as a runspec runnable (the ``pcap`` runnable).

The verbs follow the open-source Wireshark MCP servers, which all wrap
Wireshark's ``tshark``. The tshark logic lives in ``runspec-wireshark-core``
(importable with **no** runspec dependency); this package wraps each helper as
a verb of the ``pcap`` runnable and is what ``runspec local`` / ``runspec
serve`` discover.

The core helpers are re-exported here for convenience, so a caller that already
depends on ``runspec-wireshark`` can ``from runspec_wireshark import summary``
without also naming ``runspec-wireshark-core``.
"""

from runspec_wireshark_core import *  # noqa: F403
from runspec_wireshark_core import __all__  # noqa: F401
