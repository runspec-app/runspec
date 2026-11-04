"""Settings → Credentials helper: which credentials the runnables need, and what
each saved credential is used by (``credguide``)."""

from __future__ import annotations

from typing import Any

from runspec_console import credguide
from runspec_console.bridge import Bridge


def _arg(name: str, env: Any = None, type: str = "str", description: str = "") -> dict:
    a: dict[str, Any] = {"name": name, "type": type, "description": description}
    if env is not None:
        a["env"] = env
    return a


JIRA = {
    "name": "jira",
    "args": [
        _arg("env", "JIRA_ENV", "choice"),
        _arg(
            "jira-pat",
            ["JIRA_PERSONAL_TOKEN", "JIRA_PAT", "JIRA_TOKEN"],
            "password",
            "Jira Personal Access Token (Server/DC). Optional; overrides JIRA_PERSONAL_TOKEN.",
        ),
        _arg("jira-url", "JIRA_URL"),
        _arg("jira-user", ["JIRA_USERNAME", "JIRA_USER"]),
        _arg(
            "jira-password",
            ["JIRA_API_TOKEN", "JIRA_PASSWORD"],
            "password",
            "Basic-auth password.",
        ),
    ],
    "commands": {"get-issue": {"args": [_arg("key")]}},
}
GRAFANA = {
    "name": "grafana",
    "args": [
        _arg("url", "GRAFANA_URL"),
        _arg(
            "grafana-token",
            [
                "GRAFANA_SERVICE_ACCOUNT_TOKEN",
                "GRAFANA_TOKEN",
                "GRAFANA_PAT",
                "GRAFANA_API_KEY",
            ],
            "password",
            "Service account token.",
        ),
    ],
}
DEPLOY = {
    "name": "deploy",
    "args": [
        _arg("user"),
        _arg("password", type="password", description="Deploy password."),
    ],
}
PLAIN = {"name": "disk-usage", "args": [_arg("path")]}


def _on(host: str, *recs: dict) -> list[dict]:
    return [{**r, "host": host} for r in recs]


RUNNABLES = _on("local", JIRA, GRAFANA, PLAIN) + _on("web-01", JIRA, DEPLOY)


def _need(result: dict, prefix: str) -> dict:
    return next(n for n in result["needs"] if n["prefix"] == prefix)


def test_needs_one_row_per_service_with_matching_kinds():
    out = credguide.needs(RUNNABLES, [])
    assert {n["prefix"] for n in out["needs"]} == {"JIRA", "GRAFANA"}

    jira = _need(out, "JIRA")
    assert jira["label"] == "Jira"
    assert jira["runnables"] == [{"name": "jira", "hosts": ["local", "web-01"]}]
    kinds = {o["kind"]: o["matches"] for o in jira["options"]}
    assert kinds == {
        "token": ["JIRA_TOKEN", "JIRA_PAT"],
        "userpass": ["JIRA_USERNAME", "JIRA_USER", "JIRA_PASSWORD"],
    }
    token = next(o for o in jira["options"] if o["kind"] == "token")
    assert token["hint"] == "Jira Personal Access Token (Server/DC)."
    # URL / env selector are read too, but a credential doesn't set them.
    assert jira["other_env"] == ["JIRA_ENV", "JIRA_URL"]
    assert "JIRA_API_TOKEN" in jira["reads"] and "JIRA_PERSONAL_TOKEN" in jira["reads"]
    assert jira["covered_by"] == []

    # GRAFANA_SERVICE_ACCOUNT_TOKEN doesn't make a second "Grafana Service Account" row.
    grafana = _need(out, "GRAFANA")
    assert [o["kind"] for o in grafana["options"]] == ["token"]
    assert "GRAFANA_API_KEY" in grafana["reads"]


def test_password_arg_without_env_is_a_binding():
    out = credguide.needs(RUNNABLES, [])
    assert out["bindings"] == [
        {
            "runnable": "deploy",
            "hosts": ["web-01"],
            "arg": "password",
            "username_arg": "user",
            "hint": "Deploy password.",
            "label": "Deploy",
            "covered_by": [],
        }
    ]


def test_covered_by_saved_credentials_and_uncovered_sort_first():
    creds = [
        {"id": "j", "label": "Jira", "kind": "token"},
        {"id": "d", "label": "Deploy", "kind": "password", "secret_arg": "password"},
    ]
    out = credguide.needs(RUNNABLES, creds, secrets={"j"})
    assert [n["prefix"] for n in out["needs"]] == ["GRAFANA", "JIRA"]
    assert _need(out, "JIRA")["covered_by"] == [
        {"id": "j", "label": "Jira", "has_secret": True}
    ]
    assert out["bindings"][0]["covered_by"] == [
        {"id": "d", "label": "Deploy", "has_secret": False}
    ]


def test_applies_to_says_why():
    jira = {"label": "Jira", "kind": "userpass"}
    assert credguide.applies_to(jira, RUNNABLES) == [
        {
            "name": "jira",
            "hosts": ["local", "web-01"],
            "via": "env",
            "envs": ["JIRA_PASSWORD", "JIRA_USER", "JIRA_USERNAME"],
        },
    ]
    scoped = {"label": "Ops", "kind": "token", "scope_hosts": ["web-*"]}
    assert [
        (r["name"], r["hosts"], r["via"])
        for r in credguide.applies_to(scoped, RUNNABLES)
    ] == [
        ("deploy", ["web-01"], "scope"),
        ("jira", ["web-01"], "scope"),
    ]
    bound = {"label": "Whatever", "kind": "password", "secret_arg": "password"}
    assert [(r["name"], r["via"]) for r in credguide.applies_to(bound, RUNNABLES)] == [
        ("deploy", "arg")
    ]
    assert credguide.applies_to({"label": "Jira", "kind": "ssh-key"}, RUNNABLES) == []
    assert credguide.applies_to({"label": "Jira PAT", "kind": "token"}, RUNNABLES) == []


def test_label_hint_for_near_misses():
    needs = credguide.needs(RUNNABLES, [])["needs"]
    h = credguide.label_hint({"label": "Jira PAT", "kind": "token"}, needs)
    assert h and h["label"] == "Jira" and h["kind"] == "token"
    h = credguide.label_hint({"label": "Grafana", "kind": "userpass"}, needs)
    assert h and h["kind"] == "token"
    assert credguide.label_hint({"label": "Anthropic", "kind": "token"}, needs) is None


def test_suggested_label_round_trips_to_prefix():
    from runspec_console.credentials import env_prefix

    for p in ("JIRA", "JSM", "GRAFANA_PROD", "SNOW"):
        assert env_prefix(credguide.suggested_label(p)) == p


def test_split_env():
    assert credguide.split_env("JIRA_USERNAME") == ("JIRA", "USERNAME")
    assert credguide.split_env("JIRA_API_TOKEN") == ("JIRA_API", "TOKEN")
    assert credguide.split_env("GRAFANA_API_KEY") is None
    assert credguide.split_env("TOKEN") is None


# ── the bridge methods ──────────────────────────────────────────────────────────


class _Stub:
    credential_guide = Bridge.credential_guide
    credential_matches = Bridge.credential_matches
    _credential_feature_refs = Bridge._credential_feature_refs

    def __init__(self, creds: list[dict], runnables: list[dict]):
        import threading

        self._credentials = creds
        self._runnables_cache = runnables
        self._lock = threading.Lock()

    def _reload_credentials(self) -> None:
        pass


def test_credential_guide_reports_usage_and_feature_refs(monkeypatch):
    from runspec_console import config as config_mod
    from runspec_console import credentials as cred

    monkeypatch.setattr(cred, "has_secret", lambda cid: cid == "jira")
    monkeypatch.setattr(config_mod, "read_sync", lambda: {"credential_id": "jira"})
    monkeypatch.setattr(
        config_mod, "read_memory_sync", lambda: {"credential_id": "gone"}
    )
    monkeypatch.setattr(config_mod, "read_http", lambda: {})
    monkeypatch.setattr(config_mod, "read_marketplace", lambda: {"repos": []})
    monkeypatch.setattr(
        config_mod,
        "read_mcp",
        lambda: {
            "servers": [
                {
                    "name": "atl",
                    "credential": "",
                    "header_credentials": [{"header": "X-Jira", "credential": "pat"}],
                }
            ]
        },
    )
    monkeypatch.setattr(
        config_mod, "read_telemetry", lambda: (_ for _ in ()).throw(RuntimeError("bad"))
    )
    from runspec_console import repos, savedtransfers

    monkeypatch.setattr(repos, "read_repos", lambda: [])
    monkeypatch.setattr(savedtransfers, "load_saved_transfers", lambda: [])

    stub = _Stub(
        [
            {"id": "jira", "label": "Jira", "kind": "token"},
            {"id": "pat", "label": "Jira PAT", "kind": "token"},
            {"id": "odd", "label": "Jira PAT 2", "kind": "token"},
        ],
        RUNNABLES,
    )
    g = stub.credential_guide()
    assert g["hosts"] == ["local", "web-01"]
    assert g["features"] == [
        {
            "feature": "Config Sync",
            "tab": "configSync",
            "credential": "jira",
            "status": "ok",
        },
        {
            "feature": "Memory Sync",
            "tab": "memorySync",
            "credential": "gone",
            "status": "missing",
        },
        {
            "feature": "MCP server atl (X-Jira header)",
            "tab": "mcp",
            "credential": "pat",
            "status": "no_secret",
        },
    ]
    assert [r["name"] for r in g["usage"]["jira"]["runnables"]] == ["jira"]
    assert g["usage"]["jira"]["hint"] is None
    # Used by a feature, so no "rename it" hint even though no runnable reads it.
    assert g["usage"]["pat"]["runnables"] == [] and g["usage"]["pat"]["hint"] is None
    assert g["usage"]["odd"]["hint"]["label"] == "Jira"

    assert [
        r["name"]
        for r in stub.credential_matches({"label": "Grafana", "kind": "token"})
    ] == ["grafana"]
