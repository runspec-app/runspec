"""Installed-code integrity (runspec/integrity.py) — SPEC → Installed-Code Integrity."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import sys
from pathlib import Path

import pytest

import runspec.logging_setup as ls
from runspec import integrity, runsdb
from runspec.logging_setup import _ConsoleFormatter, _emit_run_summary, _JsonFormatter, _RunSummaryCounter, _SensitiveFilter, configure_logging

# ── A fake site-packages with installed distributions ────────────────────────


def _hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def make_dist(site: Path, name: str, files: dict[str, str], *, version: str = "1.0.0", requires: list[str] | None = None, direct_url: dict | None = None, unhashed: tuple[str, ...] = ()) -> Path:
    """Install ``files`` (relative path → text) into ``site`` with a RECORD the
    way pip writes one. Paths in ``unhashed`` are recorded without a hash."""
    norm = name.replace("-", "_")
    info = site / f"{norm}-{version}.dist-info"
    info.mkdir(parents=True)
    meta = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"] + [f"Requires-Dist: {r}" for r in requires or []]
    (info / "METADATA").write_text("\n".join(meta) + "\n")
    rows = []
    for rel, text in files.items():
        p = site / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode())
        rows.append(f"{rel},{'' if rel in unhashed else _hash(text.encode())},{'' if rel in unhashed else len(text.encode())}")
    if direct_url is not None:
        (info / "direct_url.json").write_text(json.dumps(direct_url))
        rows.append(f"{info.name}/direct_url.json,,")
    rows.append(f"{info.name}/METADATA,{_hash((info / 'METADATA').read_bytes())},")
    rows.append(f"{info.name}/RECORD,,")
    (info / "RECORD").write_text("\n".join(rows) + "\n")
    return info


@pytest.fixture
def site(tmp_path, monkeypatch):
    s = tmp_path / "site-packages"
    s.mkdir()
    monkeypatch.syspath_prepend(str(s))
    monkeypatch.delenv(integrity.INTEGRITY_ENV, raising=False)
    return s


def _demo(site: Path, **kw) -> Path:
    make_dist(site, "demo-core", {"demo_core/__init__.py": "X = 1\n"})
    make_dist(
        site,
        "demo-tools",
        {"demo_tools/__init__.py": "", "demo_tools/cli.py": "def main(): pass\n", "demo_tools/runspec.toml": "[demo]\n"},
        requires=["demo-core>=1", "not-installed", "demo-extra-only; extra == 'x'", 'demo-core ; python_version >= "3"'],
        **kw,
    )
    return site / "demo_tools" / "runspec.toml"


# ── Checking ─────────────────────────────────────────────────────────────────


def test_clean_index_install_has_nothing_to_report(site):
    report = integrity.check_runnable(_demo(site))
    assert report is not None
    assert [d.name for d in report.dists] == ["demo-tools", "demo-core"]  # own dist first, then direct deps
    assert all(d.status == "ok" and d.source == "index" for d in report.dists)
    assert report.token is None


def test_edited_file_is_modified(site):
    cfg = _demo(site)
    (site / "demo_tools" / "cli.py").write_text("def main(): print(open('/secret').read())\n")
    report = integrity.check_runnable(cfg)
    assert report.token == "modified"
    tools = report.dists[0]
    assert tools.status == "modified" and tools.modified == ["demo_tools/cli.py"]
    assert report.files() == ["demo-tools: demo_tools/cli.py"]


def test_edit_in_a_direct_requirement_is_caught(site):
    cfg = _demo(site)
    (site / "demo_core" / "__init__.py").write_text("X = 2\n")
    report = integrity.check_runnable(cfg)
    assert report.token == "modified"
    assert [d.name for d in report.modified] == ["demo-core"]


def test_edited_runspec_toml_is_modified(site):
    cfg = _demo(site)
    cfg.write_text("[demo]\nautonomy = 'autonomous'\n")
    assert integrity.check_runnable(cfg).token == "modified"


def test_deleted_file_is_missing(site):
    cfg = _demo(site)
    (site / "demo_tools" / "cli.py").unlink()
    tools = integrity.check_runnable(cfg).dists[0]
    assert tools.status == "modified" and tools.missing == ["demo_tools/cli.py"]


def test_unhashed_files_are_not_checked(site):
    make_dist(site, "pyc-only", {"pyc_only/runspec.toml": "[x]\n", "pyc_only/__pycache__/m.cpython-312.pyc": "a"}, unhashed=("pyc_only/__pycache__/m.cpython-312.pyc",))
    (site / "pyc_only" / "__pycache__" / "m.cpython-312.pyc").write_text("changed")
    report = integrity.check_runnable(site / "pyc_only" / "runspec.toml")
    assert report.dists[0].status == "ok" and report.dists[0].checked == 2  # runspec.toml + METADATA


def test_editable_install_is_skipped(site):
    cfg = _demo(site, direct_url={"url": "file:///src/demo", "dir_info": {"editable": True}})
    (site / "demo_tools" / "cli.py").write_text("edited\n")
    report = integrity.check_runnable(cfg)
    assert report.dists[0].status == "skipped"
    assert report.token is None


@pytest.mark.parametrize(
    ("direct_url", "source"),
    [
        ({"url": "file:///src/demo", "dir_info": {}}, "folder"),
        ({"url": "file:///tmp/demo-1.0.0-py3-none-any.whl", "archive_info": {}}, "archive"),
        ({"url": "https://git.example/demo.git", "vcs_info": {"vcs": "git", "commit_id": "abc"}}, "vcs"),
    ],
)
def test_installed_from_outside_an_index_is_local(site, direct_url, source):
    report = integrity.check_runnable(_demo(site, direct_url=direct_url))
    assert report.dists[0].source == source and report.dists[0].status == "ok"
    assert report.token == "local"


def test_modified_wins_over_local(site):
    cfg = _demo(site, direct_url={"url": "file:///src/demo", "dir_info": {}})
    (site / "demo_tools" / "cli.py").write_text("edited\n")
    assert integrity.check_runnable(cfg).token == "modified"


def test_spec_outside_any_distribution_is_not_checked(site, tmp_path):
    cfg = tmp_path / "project" / "runspec.toml"
    cfg.parent.mkdir()
    cfg.write_text("[demo]\n")
    assert integrity.check_runnable(cfg) is None


def test_switched_off_by_env(site, monkeypatch):
    cfg = _demo(site)
    (site / "demo_tools" / "cli.py").write_text("edited\n")
    monkeypatch.setenv(integrity.INTEGRITY_ENV, "off")
    assert integrity.check_runnable(cfg) is None


def test_rebranded_distribution_is_found(site):
    # The import package name doesn't match the distribution name (runspec rebrand).
    make_dist(site, "mycorp-tools", {"demo_pkg/__init__.py": "", "demo_pkg/runspec.toml": "[d]\n"})
    (site / "demo_pkg" / "__init__.py").write_text("edited\n")
    report = integrity.check_runnable(site / "demo_pkg" / "runspec.toml")
    assert report is not None and report.dists[0].name == "mycorp-tools" and report.token == "modified"


# ── Reporting ────────────────────────────────────────────────────────────────


@pytest.fixture
def audits(monkeypatch):
    seen: list[dict] = []
    from runspec import auditlog

    monkeypatch.setattr(auditlog, "audit", lambda event, outcome="", **kw: seen.append({"event": event, "outcome": outcome, **kw}))
    return seen


def test_report_warns_and_audits_once_per_change(site, tmp_path, audits, capsys):
    cfg = _demo(site)
    state = tmp_path / "state.json"
    cli = site / "demo_tools" / "cli.py"
    cli.write_text("edited\n")

    integrity.report_run("demo", integrity.check_runnable(cfg), state_path=state)
    err = capsys.readouterr().err
    assert "demo: 1 installed file differs from the package (demo-tools 1.0.0): demo_tools/cli.py" in err
    assert len(audits) == 1
    assert audits[0]["event"] == "runnable.modified" and audits[0]["outcome"] == "detected"
    assert audits[0]["target"] == {"distribution": "demo-tools", "version": "1.0.0"}
    assert audits[0]["detail"]["files"] == ["demo_tools/cli.py"] and audits[0]["detail"]["source"] == "index"

    # Same change again: warned (every run), not audited again.
    integrity.report_run("demo", integrity.check_runnable(cfg), state_path=state)
    assert "differs" in capsys.readouterr().err
    assert len(audits) == 1

    # A further edit is a new change.
    cli.write_text("edited again\n")
    integrity.report_run("demo", integrity.check_runnable(cfg), state_path=state)
    assert len(audits) == 2


def test_restored_then_edited_again_is_audited_again(site, tmp_path, audits):
    cfg = _demo(site)
    state = tmp_path / "state.json"
    cli = site / "demo_tools" / "cli.py"
    original = cli.read_text()
    cli.write_text("edited\n")
    integrity.report_run("demo", integrity.check_runnable(cfg), state_path=state)
    cli.write_text(original)
    integrity.report_run("demo", integrity.check_runnable(cfg), state_path=state)
    assert json.loads(state.read_text()) == {}
    cli.write_text("edited\n")
    integrity.report_run("demo", integrity.check_runnable(cfg), state_path=state)
    assert len(audits) == 2


def test_clean_report_is_silent(site, tmp_path, audits, capsys):
    integrity.report_run("demo", integrity.check_runnable(_demo(site)), state_path=tmp_path / "s.json")
    assert capsys.readouterr().err == "" and audits == []
    integrity.report_run("demo", None)
    assert audits == []


def test_unwritable_state_still_audits(site, tmp_path, audits):
    cfg = _demo(site)
    (site / "demo_tools" / "cli.py").write_text("edited\n")
    missing_dir = tmp_path / "nope" / "state.json"
    integrity.report_run("demo", integrity.check_runnable(cfg), state_path=missing_dir)
    integrity.report_run("demo", integrity.check_runnable(cfg), state_path=missing_dir)
    assert len(audits) == 2


# ── parse() and the run records ──────────────────────────────────────────────


def test_parse_hands_the_result_to_logging(site, monkeypatch, audits):
    import runspec.parser as parser

    cfg = _demo(site)
    (site / "demo_tools" / "cli.py").write_text("edited\n")
    seen: dict[str, object] = {}
    monkeypatch.setattr(parser, "configure_logging", lambda *a, **kw: seen.update(kw))
    monkeypatch.setattr(integrity, "_state_path", lambda: site / "state.json")
    parser.parse(script_name="demo", argv=[], config_path=cfg)
    assert seen["integrity"] == "modified"
    assert seen["integrity_files"] == ["demo-tools: demo_tools/cli.py"]
    assert len(audits) == 1


def test_load_spec_does_not_check(site, monkeypatch):
    import runspec.parser as parser

    calls: list[Path] = []
    monkeypatch.setattr(integrity, "check_runnable", lambda p: calls.append(p))
    parser.load_spec(script_name="demo", config_path=_demo(site))
    assert calls == []


@pytest.fixture
def logging_env(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(ls.atexit, "register", lambda fn, *args, **kwargs: fn)
    yield tmp_path
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(getattr(h, "formatter", None), (_JsonFormatter, _ConsoleFormatter)) or isinstance(h, _RunSummaryCounter):
            root.removeHandler(h)
            h.close()
    for f in list(root.filters):
        if isinstance(f, _SensitiveFilter):
            root.removeFilter(f)
    ls._configured = False
    ls._summary_state = None
    ls._debug = False


_CFG = {"rotate": "midnight", "keep": 7, "summary": True, "summary_console": False}


def _summary(prefix: Path, name: str) -> dict:
    lines = [json.loads(line) for line in (prefix / "logs" / f"{name}.log").read_text().strip().splitlines()]
    return next(obj["extra"] for obj in lines if obj["extra"].get("event") == "run_summary")


def test_run_summary_and_index_carry_modified(logging_env):
    configure_logging(_CFG, runnable_name="demo", history_cfg={"db": True}, integrity="modified", integrity_files=["demo-tools: demo_tools/cli.py"])
    _emit_run_summary()
    s = _summary(logging_env, "demo")
    assert s["integrity"] == "modified" and s["integrity_files"] == ["demo-tools: demo_tools/cli.py"]
    assert runsdb.list_runs(logging_env / "logs", live=False)[0]["integrity"] == "modified"


def test_local_run_has_no_file_list(logging_env):
    configure_logging(_CFG, runnable_name="demo", history_cfg={"db": True}, integrity="local")
    _emit_run_summary()
    s = _summary(logging_env, "demo")
    assert s["integrity"] == "local" and "integrity_files" not in s
    assert runsdb.list_runs(logging_env / "logs", live=False)[0]["integrity"] == "local"


def test_clean_run_records_nothing(logging_env):
    configure_logging(_CFG, runnable_name="demo", history_cfg={"db": True})
    _emit_run_summary()
    assert "integrity" not in _summary(logging_env, "demo")
    assert runsdb.list_runs(logging_env / "logs", live=False)[0]["integrity"] is None


def test_upsert_keeps_integrity(tmp_path):
    con = runsdb._connect(runsdb.db_path(tmp_path))
    runsdb._upsert(con, runsdb._normalise_row({"run_id": "r1", "runnable": "demo", "integrity": "modified"}))
    runsdb._upsert(con, runsdb._normalise_row({"run_id": "r1", "runnable": "demo", "status": "completed"}))
    con.commit()
    assert con.execute("SELECT integrity FROM runs WHERE run_id='r1'").fetchone()[0] == "modified"
    con.close()


# ── runspec verify ───────────────────────────────────────────────────────────


def _verify(site: Path, *args: str) -> tuple[int, str]:
    import os
    import subprocess

    env = {**os.environ, "PYTHONPATH": str(site), "RUNSPEC_AUDIT": "off"}
    proc = subprocess.run([sys.executable, "-c", "from runspec.cli import main; main()", "verify", *args], capture_output=True, text=True, env=env)
    return proc.returncode, proc.stdout


def test_verify_lists_changes_and_exits_1(site):
    _demo(site)
    (site / "demo_tools" / "cli.py").write_text("edited\n")
    code, out = _verify(site, "demo-tools", "--json")
    assert code == 1
    data = json.loads(out)
    by_name = {d["name"]: d for d in data["dists"]}
    assert by_name["demo-tools"]["status"] == "modified" and by_name["demo-tools"]["modified"] == ["demo_tools/cli.py"]
    assert by_name["demo-core"]["status"] == "ok"  # direct requirement included


def test_verify_clean_exits_0(site):
    _demo(site)
    code, out = _verify(site, "demo-tools")
    assert code == 0
    assert "No installed files differ" in out


def test_verify_unknown_name(site):
    code, out = _verify(site, "no-such-thing")
    assert code == 1 and "no installed distribution or runnable" in out
