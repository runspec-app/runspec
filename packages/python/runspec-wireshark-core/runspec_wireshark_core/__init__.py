"""runspec-wireshark-core — pure-Python helpers over Wireshark's tshark.

This package is stdlib only — it has **no dependency on runspec** and ships **no
runspec.toml and no entry points**, so installing it exposes the helpers for
import without surfacing any runnables (it is invisible to ``runspec local`` /
``runspec serve`` discovery). ``runspec-wireshark`` depends on it and wraps each
helper as a verb of the ``pcap`` runnable.

The verbs follow the open-source Wireshark MCP servers (mcp-wireshark, WireMCP,
sharkline, wireshark-mcp), ported onto tshark's own command line: every helper
runs ``tshark`` / ``capinfos`` / ``mergecap`` with an argument list and returns
compact plain data. Every operation takes a :class:`WiresharkConfig` first
argument (which tshark, a TLS key log, decode-as rules, a timeout), so a
private wrapper package can bake in its own defaults. Failures raise
:class:`WiresharkError` (:class:`ToolNotFoundError` when tshark isn't
installed); bad arguments raise ``ValueError``.
"""

from runspec_wireshark_core.apps import dns, http, tls
from runspec_wireshark_core.client import (
    DEFAULT_TIMEOUT,
    ToolNotFoundError,
    WiresharkConfig,
    WiresharkError,
    config_from_env,
    find_tool,
)
from runspec_wireshark_core.follow import FOLLOW_PROTOCOLS, follow, parse_follow
from runspec_wireshark_core.info import check, file_info, list_interfaces, parse_capinfos, parse_interfaces
from runspec_wireshark_core.packets import fields, packet, packets, parse_verbose, top
from runspec_wireshark_core.stats import CONVERSATION_TYPES, SEVERITY_VALUE, conversations, endpoints, expert, io, parse_phs, protocols, stats, summary
from runspec_wireshark_core.write import EXPORT_PROTOCOLS, capture, default_capture_path, export_objects, merge, save_filtered

__all__ = [
    # client / config
    "WiresharkConfig",
    "WiresharkError",
    "ToolNotFoundError",
    "config_from_env",
    "find_tool",
    "DEFAULT_TIMEOUT",
    # install / files
    "check",
    "list_interfaces",
    "file_info",
    # packets
    "packets",
    "packet",
    "fields",
    "top",
    # statistics
    "summary",
    "protocols",
    "conversations",
    "endpoints",
    "expert",
    "io",
    "stats",
    # streams / applications
    "follow",
    "dns",
    "http",
    "tls",
    # writes
    "capture",
    "save_filtered",
    "merge",
    "export_objects",
    "default_capture_path",
    # parsers (pure, for tests and wrappers)
    "parse_capinfos",
    "parse_interfaces",
    "parse_verbose",
    "parse_phs",
    "parse_follow",
    # constants
    "CONVERSATION_TYPES",
    "EXPORT_PROTOCOLS",
    "FOLLOW_PROTOCOLS",
    "SEVERITY_VALUE",
]
