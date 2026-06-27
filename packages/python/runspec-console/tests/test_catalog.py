"""
test_catalog.py — building a queue's self-service catalog from the discovery
cache: the self-service filter (driven by the console's configured name set), the
allowlist knob, and the runnable → catalog-entry (JSON Schema) conversion. Pure,
no I/O.
"""

from __future__ import annotations

from runspec_console.catalog import (
    build_catalog,
    build_full_catalog,
    build_runbook_catalog,
    runnable_to_catalog_entry,
    runbook_audiences,
    runbook_to_catalog_entry,
)


def _runnable(name: str, **over):
    base = {
        "name": name,
        "host": "web01",
        "group": "venv",
        "description": f"{name} desc",
        "autonomy": "confirm",
        "args": [],
        "requireCommand": False,
    }
    base.update(over)
    return base


# ── entry conversion ───────────────────────────────────────────────────────────


def test_entry_carries_schema_in_visitor_contract():
    runnable = _runnable(
        "request-vm",
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
            _runnable("reset-password"),
            _runnable("rotate-root-key"),
            _runnable("list-tickets"),
        ],
        self_service_names={"reset-password"},
    )
    assert [t["name"] for t in tools] == ["reset-password"]
    assert set(index) == {"reset-password"}
    assert index["reset-password"]["host"] == "web01"  # enactment mapping


def test_build_skips_subcommand_runnables():
    tools, _ = build_catalog(
        [_runnable("db", requireCommand=True)],
        self_service_names={"db"},
    )
    assert tools == []  # the visitor form is flat in v1


def test_build_applies_allowlist():
    runnables = [_runnable("reset-password"), _runnable("request-vm")]
    names = {"reset-password", "request-vm"}
    tools, _ = build_catalog(
        runnables, self_service_names=names, allowlist=["request-vm"]
    )
    assert [t["name"] for t in tools] == ["request-vm"]
    # None means "all designated" (the default), [] means "none".
    assert (
        len(build_catalog(runnables, self_service_names=names, allowlist=None)[0]) == 2
    )
    assert build_catalog(runnables, self_service_names=names, allowlist=[])[0] == []


def test_build_dedupes_across_hosts_first_wins():
    tools, index = build_catalog(
        [
            _runnable("reset-password", host="web01"),
            _runnable("reset-password", host="web02"),
        ],
        self_service_names={"reset-password"},
    )
    assert len(tools) == 1
    assert index["reset-password"]["host"] == "web01"


def test_entry_carries_audience_and_kind_tags():
    entry = runnable_to_catalog_entry(_runnable("a"))
    assert entry["x-audience"] == "self-service" and entry["x-kind"] == "runnable"
    op = runnable_to_catalog_entry(_runnable("a"), audience="operator")
    assert op["x-audience"] == "operator"


# ── build_full_catalog (combined, audience-tagged channel) ──────────────────────


def test_full_catalog_tags_broadest_audience():
    tools, index = build_full_catalog(
        [
            _runnable("reset-password"),  # designated self-service
            _runnable("rotate-key"),  # operator (not designated)
            _runnable("list-tickets"),  # operator (not designated)
        ],
        self_service_names={"reset-password"},
    )
    by = {t["name"]: t["x-audience"] for t in tools}
    assert by == {
        "reset-password": "self-service",
        "rotate-key": "operator",
        "list-tickets": "operator",
    }
    # Every discovered runnable is at least operator-visible now.
    assert set(index) == {"reset-password", "rotate-key", "list-tickets"}
    # index is name → {host: discovered runnable}
    assert index["reset-password"]["web01"]["host"] == "web01"


def test_full_catalog_collapses_fleet_hosts_into_x_hosts():
    tools, index = build_full_catalog(
        [
            _runnable("check-disk", host="web01"),
            _runnable("check-disk", host="web02"),
            _runnable("check-disk", host="web01"),  # dup host
        ],
        self_service_names=set(),
    )
    # One entry listing every host it was discovered on (order-preserving, deduped).
    assert len(tools) == 1
    assert tools[0]["x-hosts"] == ["web01", "web02"]
    # The index maps the name to a per-host discovered record for enactment.
    assert set(index["check-disk"]) == {"web01", "web02"}


def test_full_catalog_single_host_still_lists_it():
    tools, _ = build_full_catalog([_runnable("check-disk")], self_service_names=set())
    assert tools[0]["x-hosts"] == ["web01"]


def test_full_catalog_allowlist_narrows_self_service_only():
    runnables = [_runnable("reset-password"), _runnable("deploy")]
    names = {"reset-password", "deploy"}
    tools, _ = build_full_catalog(
        runnables, self_service_names=names, self_service_allowlist=["deploy"]
    )
    by = {t["name"]: t["x-audience"] for t in tools}
    # 'deploy' is allowlisted → self-service (broadest); 'reset-password' is
    # designated but excluded by the visitor allowlist → falls back to operator.
    assert by == {"reset-password": "operator", "deploy": "self-service"}
    tools2, _ = build_full_catalog(
        runnables, self_service_names=names, self_service_allowlist=[]
    )
    by2 = {t["name"]: t["x-audience"] for t in tools2}
    # allowlist empty: both fall back to operator.
    assert by2 == {"reset-password": "operator", "deploy": "operator"}


def test_full_catalog_skips_subcommands():
    tools, _ = build_full_catalog(
        [_runnable("db", requireCommand=True)], self_service_names=set()
    )
    assert tools == []


# ── runbooks (published runbooks) ─────────────────────────────────────────────────


def _runbook(sid, discoverable=None, **over):
    base = {"id": sid, "title": f"{sid} title", "symptom": f"{sid} symptom"}
    if discoverable is not None:
        base["discoverable"] = discoverable
    base.update(over)
    return base


def test_runbook_audiences_is_opt_in():
    assert runbook_audiences(_runbook("a")) == []  # absent → not published
    assert runbook_audiences(_runbook("a", discoverable=False)) == []
    assert runbook_audiences(_runbook("a", discoverable=True)) == ["operator"]
    assert runbook_audiences(_runbook("a", discoverable=["operator"])) == ["operator"]


def test_runbook_entry_shape_and_schema():
    entry = runbook_to_catalog_entry(
        _runbook(
            "restart",
            discoverable=["operator"],
            args={"svc": {"options": ["nginx", "apache"]}},
        )
    )
    assert entry["name"] == "runbook:restart"  # namespaced wire name
    assert entry["x-kind"] == "runbook" and entry["x-audience"] == "operator"
    assert entry["x-title"] == "restart title"
    assert entry["inputSchema"]["properties"]["svc"] == {
        "type": "string",
        "enum": ["nginx", "apache"],
    }


def test_build_runbook_catalog_includes_only_opt_in():
    tools, index = build_runbook_catalog(
        [
            _runbook("restart", discoverable=["operator"]),
            _runbook("private"),  # not published
            _runbook("restart", discoverable=["operator"]),  # dup id → first wins
        ]
    )
    assert [t["name"] for t in tools] == ["runbook:restart"]
    assert index == {"runbook:restart": "restart"}


def test_build_runbook_catalog_tags_broadest_audience():
    tools, _ = build_runbook_catalog(
        [
            _runbook("reset-pw", discoverable=["self-service"]),  # visitor-facing
            _runbook(
                "both", discoverable=["operator", "self-service"]
            ),  # broadest wins
            _runbook("restart", discoverable=["operator"]),  # operator-only
        ]
    )
    by = {t["name"]: t["x-audience"] for t in tools}
    assert by == {
        "runbook:reset-pw": "self-service",
        "runbook:both": "self-service",
        "runbook:restart": "operator",
    }


def test_runbook_entry_explicit_self_service_audience():
    entry = runbook_to_catalog_entry(_runbook("reset-pw"), audience="self-service")
    assert entry["x-audience"] == "self-service" and entry["x-kind"] == "runbook"
