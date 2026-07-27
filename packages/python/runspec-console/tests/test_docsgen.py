"""
test_docsgen.py — the deterministic runbook-wiki generator + the bridge wrapper.

render_wiki is pure (runbooks + hosts in → {path: markdown} out). The properties
that matter: every runbook gets a page, tags group the index, params render, runbooks
cross-link the runnables/hosts their steps name, and — the one the commit-back
loop depends on — the same input yields byte-identical output.
"""

from __future__ import annotations

import runspec_console.docsgen as docsgen
from runspec_console.bridge import Bridge

SKILLS = [
    {
        "id": "restart-web",
        "title": "Restart the web tier",
        "symptom": "Web is 502ing",
        "steps": "1. Check `web1` status\n2. Run web1__restart-nginx to bounce it.",
        "tags": ["outage", "web"],
        "args": {"host": {"type": "str", "description": "Target host", "ui": "host"}},
    },
    {
        "id": "rotate-logs",
        "title": "Rotate logs",
        "symptom": "",
        "steps": "Call db1__rotate-logs.",
        "tags": [],
    },
]

HOSTS = [
    {"name": "web1", "ssh": "ops@web1", "tags": ["web"]},
    {"name": "db1", "ssh": "ops@db1"},
]

RUNNABLES = [
    {
        "name": "restart-nginx",
        "host": "web1",
        "group": "ops",
        "description": "Bounce the nginx service",
        "autonomy": "confirm",
        "args": [
            {
                "name": "force",
                "type": "flag",
                "required": False,
                "description": "Skip drain",
            },
        ],
    },
    {
        "name": "backup",
        "host": "db1",
        "group": "",
        "description": "Back up a database",
        "autonomy": "autonomous",
        "runAs": "postgres",
        "requireCommand": True,
        "args": [],
        "commands": {
            "full": {
                "name": "full",
                "host": "db1",
                "group": "",
                "description": "Full dump",
                "autonomy": "autonomous",
                "args": [
                    {"name": "keep", "type": "int", "required": False, "default": 7},
                ],
            }
        },
    },
]


def test_render_wiki_emits_expected_pages():
    out = docsgen.render_wiki(SKILLS, HOSTS, RUNNABLES)
    assert set(out) == {
        "README.md",
        "runbooks/restart-web.md",
        "runbooks/rotate-logs.md",
        "hosts.md",
        "runnables.md",
        "runnables/web1.md",
        "runnables/db1.md",
        "maintaining-docs.md",
    }


def test_runnables_catalogue_has_detail_and_subcommands():
    out = docsgen.render_wiki(SKILLS, HOSTS, RUNNABLES)
    idx = out["runnables.md"]
    assert "[`web1`](runnables/web1.md)" in idx
    page = out["runnables/db1.md"]
    assert "# Runnables on `db1`" in page
    assert "`backup`" in page
    assert "Back up a database" in page
    assert "autonomous" in page  # autonomy surfaced
    assert "`postgres`" in page  # run-as surfaced
    assert "Subcommands of `backup`" in page
    assert "`full`" in page  # the subcommand
    assert "`keep`" in page  # the subcommand's arg
    # hosts.md links to each host's catalogue page with a count
    assert "[1](runnables/web1.md)" in out["hosts.md"]
    # README summarises the catalogue
    assert "[runnables.md](runnables.md)" in out["README.md"]


def test_runnables_optional_empty_when_omitted():
    out = docsgen.render_wiki(SKILLS, HOSTS)  # no runnables arg
    assert "runnables.md" in out
    assert "_No runnables discovered yet" in out["runnables.md"]
    # no per-host runnable pages when there's no catalogue
    assert not any(k.startswith("runnables/") for k in out)


def test_maintaining_page_carries_pasteable_default_runbook():
    page = docsgen.render_wiki(SKILLS, HOSTS)["maintaining-docs.md"]
    assert "```toml" in page
    # the paste-able block is a valid [[runbook]] entry with the default runbook's id
    assert "[[runbook]]" in page
    assert 'id = "regenerate-runbook-docs"' in page
    # the README points at it
    readme = docsgen.render_wiki(SKILLS, HOSTS)["README.md"]
    assert "[maintaining-docs.md](maintaining-docs.md)" in readme


def test_default_runbook_toml_round_trips():
    import tomllib

    block = docsgen._runbook_to_toml(docsgen.DEFAULT_RUNBOOK)
    parsed = tomllib.loads(block)
    assert parsed["runbook"][0]["id"] == "regenerate-runbook-docs"
    assert parsed["runbook"][0]["tags"] == ["docs", "self-service"]


def test_default_runbook_toml_emits_step_array():
    """The paste-able wiki example now teaches the [[runbook.step]] form."""
    block = docsgen._runbook_to_toml(docsgen.DEFAULT_RUNBOOK)
    assert "[[runbook.step]]" in block
    assert "steps = " not in block  # not the legacy string key


def test_structured_runbook_page_renders_numbered_prose_and_cross_links():
    """A [[runbook.step]] runbook renders numbered ## Steps prose and still resolves
    the runnable/host tokens named inside a step."""
    runbooks = [
        {
            "id": "web-triage",
            "title": "Web triage",
            "symptom": "502s",
            "step": [
                {"text": "Check web1 status."},
                {"title": "Bounce", "text": "Run web1__restart-nginx to bounce it."},
            ],
            "tags": ["web"],
        }
    ]
    page = docsgen.render_wiki(runbooks, HOSTS)["runbooks/web-triage.md"]
    assert "1. Check web1 status." in page
    assert "2. Bounce: Run web1__restart-nginx to bounce it." in page
    assert "`web1__restart-nginx`" in page  # runnable token from a step
    assert "[`web1`](../hosts.md)" in page  # host referenced in a step


def test_index_groups_by_tag_and_untagged():
    readme = docsgen.render_wiki(SKILLS, HOSTS)["README.md"]
    assert "### outage" in readme
    assert "### web" in readme
    assert "### Untagged" in readme  # the tag-less runbook
    # a runbook links to its page
    assert "[Restart the web tier](runbooks/restart-web.md)" in readme


def test_runbook_page_has_params_and_cross_links():
    page = docsgen.render_wiki(SKILLS, HOSTS)["runbooks/restart-web.md"]
    assert "# Restart the web tier" in page
    assert "**When to use:** Web is 502ing" in page
    assert "## Parameters" in page
    assert "`host`" in page  # the param row
    # cross-links derived deterministically from the steps text
    assert "`web1__restart-nginx`" in page  # runnable token
    assert "[`web1`](../hosts.md)" in page  # host referenced in steps


def test_hosts_page_reverse_maps_runbooks():
    page = docsgen.render_wiki(SKILLS, HOSTS)["hosts.md"]
    assert "| `web1` |" in page
    assert "ops@web1" in page
    # web1 is named in the restart-web steps → listed under "Used by runbooks"
    assert "Restart the web tier" in page


def test_output_is_byte_identical_across_runs():
    a = docsgen.render_wiki(SKILLS, HOSTS)
    b = docsgen.render_wiki(list(reversed(SKILLS)), list(reversed(HOSTS)))
    # order of inputs must not change the output (sorted internally) — this is the
    # determinism the commit-back loop relies on.
    assert a == b


def test_empty_inputs_render_without_error():
    out = docsgen.render_wiki([], [])
    assert "_No runbooks defined yet._" in out["README.md"]
    assert "_No hosts defined._" in out["hosts.md"]


# ── bridge.generate_docs ─────────────────────────────────────────────────────────


class _DocsBridge:
    generate_docs = Bridge.generate_docs
    _atomic_write_bytes = Bridge._atomic_write_bytes
    get_runnables = Bridge.get_runnables

    def __init__(self, hosts, runnables=None):
        self._hosts = hosts
        self._runnables_cache = list(runnables or [])
        import threading

        self._lock = threading.RLock()


def test_generate_docs_writes_files(monkeypatch, tmp_path):
    import runspec_console.memory as memory

    monkeypatch.setattr(memory, "load_runbooks", lambda: SKILLS)
    res = _DocsBridge(HOSTS, RUNNABLES).generate_docs(str(tmp_path / "docs"))

    assert res["ok"] is True
    assert (tmp_path / "docs" / "README.md").exists()
    assert (tmp_path / "docs" / "runbooks" / "restart-web.md").exists()
    assert (tmp_path / "docs" / "runnables" / "web1.md").exists()
    assert set(res["written"]) == {
        "README.md",
        "hosts.md",
        "runnables.md",
        "runnables/web1.md",
        "runnables/db1.md",
        "maintaining-docs.md",
        "runbooks/restart-web.md",
        "runbooks/rotate-logs.md",
    }
    # rerun with the same inputs is byte-identical (clean diff)
    before = (tmp_path / "docs" / "README.md").read_bytes()
    _DocsBridge(HOSTS, RUNNABLES).generate_docs(str(tmp_path / "docs"))
    assert (tmp_path / "docs" / "README.md").read_bytes() == before


def test_generate_docs_defaults_to_app_dir(monkeypatch, tmp_path):
    import runspec_console.memory as memory

    monkeypatch.setattr(memory, "load_runbooks", lambda: [])
    monkeypatch.setattr("runspec_console.config.read_docs_dir", lambda: "")
    monkeypatch.setattr("runspec_console.config.app_dir", lambda: tmp_path)
    res = _DocsBridge([]).generate_docs()
    assert res["ok"] is True
    assert res["dir"] == str(tmp_path / "docs")
    assert (tmp_path / "docs" / "README.md").exists()
