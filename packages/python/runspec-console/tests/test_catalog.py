"""
test_catalog.py — building a queue's self-service catalog from the discovery
cache: audience normalisation, the self-service filter, the allowlist knob, and
the runnable → catalog-entry (JSON Schema) conversion. Pure, no I/O.
"""

from __future__ import annotations

from runspec_console.catalog import (
    build_catalog,
    discoverable_audiences,
    is_self_service,
    runnable_to_catalog_entry,
)


def _runnable(name: str, discoverable=None, **over):
    base = {
        "name": name,
        "host": "web01",
        "group": "venv",
        "description": f"{name} desc",
        "autonomy": "confirm",
        "args": [],
        "requireCommand": False,
        "rawSpec": {"discoverable": discoverable},
    }
    base.update(over)
    return base


# ── audiences ──────────────────────────────────────────────────────────────────


def test_audience_normalisation_matches_core():
    assert discoverable_audiences(None) == ["operator"]
    assert discoverable_audiences(True) == ["operator"]
    assert discoverable_audiences(False) == []
    assert discoverable_audiences(["operator", "self-service"]) == [
        "operator",
        "self-service",
    ]


def test_is_self_service_is_opt_in_only():
    assert not is_self_service(_runnable("a"))  # absent → operator-only
    assert not is_self_service(_runnable("a", discoverable=True))
    assert not is_self_service(_runnable("a", discoverable=False))
    assert is_self_service(_runnable("a", discoverable=["self-service"]))
    assert is_self_service(_runnable("a", discoverable=["operator", "self-service"]))
    # An old runspec on the host → no rawSpec key at all → never visitor-facing.
    assert not is_self_service({"name": "a", "rawSpec": {}})
    assert not is_self_service({"name": "a"})


# ── entry conversion ───────────────────────────────────────────────────────────


def test_entry_carries_schema_in_visitor_contract():
    runnable = _runnable(
        "request-vm",
        discoverable=["self-service"],
        args=[
            {"name": "size", "type": "choice", "required": True, "options": ["s", "l"]},
            {
                "name": "count",
                "type": "int",
                "required": False,
                "default": 1,
                "range": [1, 5],
            },
            {"name": "force", "type": "flag", "required": False},
            {"name": "token", "type": "password", "required": False},  # must vanish
        ],
    )
    entry = runnable_to_catalog_entry(runnable)
    assert entry["name"] == "request-vm"
    assert entry["x-autonomy"] == "confirm"
    schema = entry["inputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["size"]
    assert schema["properties"]["size"] == {"type": "string", "enum": ["s", "l"]}
    assert schema["properties"]["count"] == {
        "type": "integer",
        "default": 1,
        "minimum": 1,
        "maximum": 5,
    }
    assert schema["properties"]["force"] == {"type": "boolean"}
    assert "token" not in schema["properties"]  # secrets never cross the boundary


# ── build_catalog ──────────────────────────────────────────────────────────────


def test_build_filters_to_self_service_only():
    tools, index = build_catalog(
        [
            _runnable("reset-password", discoverable=["self-service"]),
            _runnable("rotate-root-key", discoverable=False),
            _runnable("list-tickets"),  # default operator-only
        ]
    )
    assert [t["name"] for t in tools] == ["reset-password"]
    assert set(index) == {"reset-password"}
    assert index["reset-password"]["host"] == "web01"  # enactment mapping


def test_build_skips_subcommand_runnables():
    tools, _ = build_catalog(
        [_runnable("db", discoverable=["self-service"], requireCommand=True)]
    )
    assert tools == []  # the visitor form is flat in v1


def test_build_applies_allowlist():
    runnables = [
        _runnable("reset-password", discoverable=["self-service"]),
        _runnable("request-vm", discoverable=["self-service"]),
    ]
    tools, _ = build_catalog(runnables, allowlist=["request-vm"])
    assert [t["name"] for t in tools] == ["request-vm"]
    # None means "all marked" (the default), [] means "none".
    assert len(build_catalog(runnables, allowlist=None)[0]) == 2
    assert build_catalog(runnables, allowlist=[])[0] == []


def test_build_dedupes_across_hosts_first_wins():
    tools, index = build_catalog(
        [
            _runnable("reset-password", discoverable=["self-service"], host="web01"),
            _runnable("reset-password", discoverable=["self-service"], host="web02"),
        ]
    )
    assert len(tools) == 1
    assert index["reset-password"]["host"] == "web01"
