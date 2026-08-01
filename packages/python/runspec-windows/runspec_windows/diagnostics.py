"""collect-diagnostics — one structured connectivity report the agent turns into
a Windows Desktop-Services ticket email.

Composes the read-only ``netdiag`` collectors (proxy, VPN, DNS, route,
connectivity, adapters, applied GPO) into a single JSON document. Each sub-probe
is isolated: a failure becomes ``{"error": ...}`` under its own key instead of
aborting the whole bundle, so a locked-down box where one probe is blocked still
returns everything else. Content-only — it never sends anything; drafting the
email is the agent's job.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone

import runspec as rs

from runspec_windows import _platform, netdiag, network

# The probes collect-diagnostics can run, in report order.
_PROBES: dict[str, Callable[[str], dict]] = {}


def _adapters(_target: str) -> dict:
    result = _platform.run_cmd(["ipconfig", "/all"])
    return {"adapters": network.parse_ipconfig(result.stdout)}


def _connectivity(target: str) -> dict:
    return netdiag.collect_test_connection(target)


_PROBES = {
    "proxy": lambda _t: netdiag.collect_proxy(),
    "vpn": lambda _t: netdiag.collect_vpn(),
    "dns": lambda _t: netdiag.collect_dns(),
    "route": lambda _t: netdiag.collect_route(),
    "connectivity": _connectivity,
    "adapters": _adapters,
    "gpo": lambda _t: netdiag.collect_gpo(),
}


def collect_diagnostics(target: str, include: list[str] | None = None) -> dict:
    """Run each selected probe, isolating failures per key. ``include`` scopes the
    probes (default: all). ``target`` is the host the connectivity probe tests."""
    selected = include or list(_PROBES)
    report: dict = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
    }
    for name in selected:
        probe = _PROBES.get(name)
        if probe is None:
            report[name] = {"error": f"unknown probe: {name}"}
            continue
        try:
            report[name] = probe(target)
        except Exception as e:  # one blocked probe must not sink the bundle
            report[name] = {"error": str(e)}
    return report


def main_collect_diagnostics() -> None:
    spec = rs.parse("collect-diagnostics")
    _platform.ensure_windows()
    target = str(spec.target.value)
    include = spec.include.value or None
    try:
        print(json.dumps(collect_diagnostics(target, include)))
    except Exception as e:
        _platform.fail(str(e), target=target)
