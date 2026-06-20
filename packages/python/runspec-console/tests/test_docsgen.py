"""
test_docsgen.py — the deterministic runbook-wiki generator + the bridge wrapper.

render_wiki is pure (skills + hosts in → {path: markdown} out). The properties
that matter: every skill gets a page, tags group the index, params render, skills
cross-link the runnables/hosts their steps name, and — the one the commit-back
loop depends on — the same input yields byte-identical output.
"""

from __future__ import annotations

import runspec_console.bridge as bridge_mod
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


def test_render_wiki_emits_expected_pages():
    out = docsgen.render_wiki(SKILLS, HOSTS)
    assert set(out) == {
        "README.md",
        "skills/restart-web.md",
        "skills/rotate-logs.md",
        "hosts.md",
    }


def test_index_groups_by_tag_and_untagged():
    readme = docsgen.render_wiki(SKILLS, HOSTS)["README.md"]
    assert "### outage" in readme
    assert "### web" in readme
    assert "### Untagged" in readme  # the tag-less skill
    # a skill links to its page
    assert "[Restart the web tier](skills/restart-web.md)" in readme


def test_skill_page_has_params_and_cross_links():
    page = docsgen.render_wiki(SKILLS, HOSTS)["skills/restart-web.md"]
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
    assert "_No skills defined yet._" in out["README.md"]
    assert "_No hosts defined._" in out["hosts.md"]


# ── bridge.generate_docs ─────────────────────────────────────────────────────────


class _DocsBridge:
    generate_docs = Bridge.generate_docs
    _atomic_write_bytes = Bridge._atomic_write_bytes

    def __init__(self, hosts):
        self._hosts = hosts


def test_generate_docs_writes_files(monkeypatch, tmp_path):
    import runspec_console.memory as memory

    monkeypatch.setattr(memory, "load_skills", lambda: SKILLS)
    res = _DocsBridge(HOSTS).generate_docs(str(tmp_path / "docs"))

    assert res["ok"] is True
    assert (tmp_path / "docs" / "README.md").exists()
    assert (tmp_path / "docs" / "skills" / "restart-web.md").exists()
    assert set(res["written"]) == {
        "README.md",
        "hosts.md",
        "skills/restart-web.md",
        "skills/rotate-logs.md",
    }
    # rerun is byte-identical (clean diff)
    before = (tmp_path / "docs" / "README.md").read_bytes()
    _DocsBridge(HOSTS).generate_docs(str(tmp_path / "docs"))
    assert (tmp_path / "docs" / "README.md").read_bytes() == before


def test_generate_docs_defaults_to_app_dir(monkeypatch, tmp_path):
    import runspec_console.memory as memory

    monkeypatch.setattr(memory, "load_skills", lambda: [])
    monkeypatch.setattr(bridge_mod, "read_docs_dir", lambda: "")
    monkeypatch.setattr(bridge_mod, "app_dir", lambda: tmp_path)
    res = _DocsBridge([]).generate_docs()
    assert res["ok"] is True
    assert res["dir"] == str(tmp_path / "docs")
    assert (tmp_path / "docs" / "README.md").exists()
