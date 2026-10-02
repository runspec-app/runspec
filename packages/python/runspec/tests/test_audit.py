"""Default-on logging + the append-only audit ledger (runspec.audit / runspec audit)."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

import runspec
import runspec.logging_setup as ls
from runspec import auditlog
from runspec.parser import _is_core_cli

SPEC_NO_LOGGING = """
[greet]
description = "Say hello"

[greet.args]
name = {type = "str", default = "world"}
"""


@pytest.fixture(autouse=True)
def _venv(monkeypatch, tmp_path):
    """A test-local venv root, no OS log, and a clean logging state after."""
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(ls.atexit, "register", lambda fn, *a, **k: fn)
    monkeypatch.setattr(auditlog, "_os_log", lambda record, facility="auth": False)
    monkeypatch.setattr(ls, "_run_context", {})
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(getattr(h, "formatter", None), (ls._JsonFormatter, ls._ConsoleFormatter)) or isinstance(h, ls._RunSummaryCounter):
            root.removeHandler(h)
            h.close()
    for name in ls._FILE_ONLY_LOGGERS:
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()
        lg.propagate = True
    if isinstance(sys.stdout, ls._StdoutTee):
        sys.stdout = sys.stdout._original
    if ls._excepthook_installed:
        sys.excepthook = sys.__excepthook__
        ls._excepthook_installed = False
    ls._configured = False
    ls._summary_state = None
    ls._debug = False


def _ledger(tmp_path: Path) -> Path:
    return tmp_path / "logs" / "audit.jsonl"


# ── default-on resolution ────────────────────────────────────────────────────


class TestResolveLoggingConfig:
    @pytest.mark.audit_on
    def test_absent_section_means_defaults(self):
        cfg = ls.resolve_logging_config(None)
        assert cfg is not None and cfg["enabled"] is True and cfg["summary"] is True

    def test_audit_off_env_restores_opt_in(self):
        assert ls.resolve_logging_config(None) is None  # conftest sets RUNSPEC_AUDIT=off

    @pytest.mark.audit_on
    def test_implicit_false_restores_opt_in(self):
        assert ls.resolve_logging_config(None, implicit=False) is None

    def test_declared_section_always_honoured(self):
        declared = {"enabled": False, "rotate": "midnight", "keep": 7, "summary": True, "summary_console": False, "store": "single"}
        assert ls.resolve_logging_config(declared) is declared

    def test_core_cli_is_exempt(self):
        assert _is_core_cli(Path(runspec.__file__).parent / "runspec.toml")


@pytest.mark.audit_on
class TestDefaultOn:
    def test_runnable_without_logging_section_gets_debug_and_a_run_record(self, tmp_path):
        spec = tmp_path / "runspec.toml"
        spec.write_text(SPEC_NO_LOGGING)
        args = runspec.parse("greet", argv=["--debug"], config_path=spec)
        assert args.name.value == "world"
        ls._emit_run_summary()
        [log] = list((tmp_path / "logs").glob("greet*.log"))
        events = [json.loads(ln).get("extra", {}).get("event") for ln in log.read_text().splitlines()]
        assert "run_summary" in events

    def test_harness_keeps_the_opt_in_behaviour(self, tmp_path):
        from runspec.testing import ParseHarness

        with ParseHarness(script_name="greet", toml=SPEC_NO_LOGGING) as h:
            h.expect_ok([])
        assert not ls._configured


class TestAuditOnly:
    """[config.logging] enabled = false — the developer owns logging."""

    def _cfg(self):
        return {"enabled": False, "rotate": "midnight", "keep": 7, "summary": True, "summary_console": False, "store": "single"}

    def test_leaves_root_logger_and_stdout_alone(self):
        root = logging.getLogger()
        before = list(root.handlers)
        stdout = sys.stdout
        ls.configure_logging(self._cfg(), runnable_name="mine")
        assert root.handlers == before
        assert sys.stdout is stdout
        assert logging.getLogger(ls._RUN_SUMMARY_LOGGER).propagate is False

    def test_run_record_still_written(self, tmp_path):
        ls.configure_logging(self._cfg(), runnable_name="mine")
        logging.getLogger("app").warning("developer's own message")  # not ours to capture
        ls._emit_run_summary()
        lines = [json.loads(ln) for ln in (tmp_path / "logs" / "mine.log").read_text().splitlines()]
        assert [r["extra"]["event"] for r in lines] == ["run_summary"]

    def test_existing_excepthook_still_called(self, monkeypatch):
        seen = []
        monkeypatch.setattr(sys, "excepthook", lambda *a: seen.append(a[0]))
        ls.configure_logging(self._cfg(), runnable_name="mine")
        try:
            raise KeyError("x")
        except KeyError:
            sys.excepthook(*sys.exc_info())
        assert seen == [KeyError]
        assert ls._summary_state["exception"]["type"] == "KeyError"


# ── runspec.audit() ──────────────────────────────────────────────────────────


@pytest.mark.audit_on
class TestAudit:
    def test_appends_a_chained_ledger(self, tmp_path):
        a = runspec.audit("payment.refund", "succeeded", target={"order": "A-1"})
        b = runspec.audit("payment.refund", "failed", target={"order": "A-2"})
        assert a["prev"] == auditlog.GENESIS
        assert b["prev"] == a["hash"]
        res = auditlog.verify(_ledger(tmp_path))
        assert res["ok"] and res["records"] == 2 and res["last_hash"] == b["hash"]

    def test_verify_catches_an_edit(self, tmp_path):
        runspec.audit("a.one", "ok")
        runspec.audit("a.two", "ok")
        p = _ledger(tmp_path)
        p.write_text(p.read_text().replace('"a.one"', '"a.xxx"'))
        res = auditlog.verify(p)
        assert not res["ok"] and res["first_bad"] == 1 and "edited" in res["reason"]

    def test_verify_catches_a_deleted_line(self, tmp_path):
        for n in range(3):
            runspec.audit(f"a.n{n}", "ok")
        p = _ledger(tmp_path)
        lines = p.read_text().splitlines()
        p.write_text("\n".join([lines[0], lines[2]]) + "\n")
        res = auditlog.verify(p)
        assert not res["ok"] and res["first_bad"] == 2 and "chain broken" in res["reason"]

    def test_secrets_are_redacted(self):
        rec = runspec.audit("deploy.start", target={"host": "web-1", "token": "abc"}, detail={"note": "password=hunter2"})
        assert rec["target"]["token"] == "[REDACTED]"
        assert "hunter2" not in rec["detail"]["note"]

    def test_bad_event_name_raises(self):
        with pytest.raises(ValueError):
            runspec.audit("Not Valid")

    def test_strict_raises_when_nothing_took_it(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("")
        with pytest.raises(runspec.AuditError):
            runspec.audit("x.y", strict=True, ledger_dir=blocker / "sub", syslog=False)

    def test_off_env_records_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUNSPEC_AUDIT", "off")
        assert runspec.audit("x.y") is None
        assert not _ledger(tmp_path).exists()

    def test_in_a_run_the_event_is_tied_to_it_and_lands_in_the_run_log(self, tmp_path):
        ls.configure_logging(
            {"enabled": True, "rotate": "midnight", "keep": 7, "summary": False, "summary_console": False, "store": "single"},
            runnable_name="refunds",
            command_path=["issue"],
        )
        rec = runspec.audit("payment.refund", "succeeded")
        assert rec["runnable"] == "refunds" and rec["command"] == "issue" and rec["run_id"]
        run_log = [json.loads(ln) for ln in (tmp_path / "logs" / "refunds.log").read_text().splitlines()]
        assert any(r["logger"] == "runspec.audit" and r["extra"]["audit"]["id"] == rec["id"] for r in run_log)

    def test_an_inbox_writer_leaves_the_ledger_to_the_sweep(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ls, "_run_context", {"run_id": "r1", "runnable": "x", "inbox": True})
        rec = runspec.audit("x.y", "ok")
        assert "hash" not in rec
        assert not _ledger(tmp_path).exists()

    def test_sweep_fold_attributes_to_the_file_owner(self, tmp_path):
        claimed = {"event": "x.y", "outcome": "ok", "actor": "mallory", "id": "1"}
        lines = [
            json.dumps({"logger": "runspec.audit", "extra": {"audit": claimed}}),
            json.dumps({"logger": "app", "extra": {"audit": claimed}}),  # not ours — ignored
        ]
        assert auditlog.fold_inbox_lines(tmp_path / "logs", lines, os_user="alice") == 1
        [rec] = list(auditlog.read_ledger(_ledger(tmp_path)))
        assert rec["actor"] == "alice" and rec["claimed_actor"] == "mallory" and rec["via"] == "inbox"
        assert auditlog.verify(_ledger(tmp_path))["ok"]


# ── runspec audit CLI ────────────────────────────────────────────────────────


@pytest.mark.audit_on
class TestCli:
    def _run(self, monkeypatch, *argv):
        from runspec import cli

        monkeypatch.setattr(sys, "argv", ["runspec", "audit", *argv])
        cli.main()

    def test_record_list_verify(self, tmp_path, monkeypatch, capsys):
        self._run(monkeypatch, "record", "deploy.start", "--outcome", "started", "-t", "host=web-1")
        self._run(monkeypatch, "list", "--json")
        out = capsys.readouterr().out.splitlines()
        rec = json.loads(out[-1])
        assert rec["event"] == "deploy.start" and rec["target"] == {"host": "web-1"}
        self._run(monkeypatch, "verify")
        assert "hash chain intact" in capsys.readouterr().out

    def test_verify_exits_1_on_tamper(self, tmp_path, monkeypatch):
        runspec.audit("a.b", "ok")
        p = _ledger(tmp_path)
        p.write_text(p.read_text().replace('"ok"', '"no"'))
        with pytest.raises(SystemExit) as exc:
            self._run(monkeypatch, "verify")
        assert exc.value.code == 1

    def test_deny_change_is_audited(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RUNSPEC_DENYLIST_FILE", str(tmp_path / "deny.toml"))
        from runspec import cli

        monkeypatch.setattr(sys, "argv", ["runspec", "deny", "add", "--runnable", "deploy"])
        cli.main()
        [rec] = list(auditlog.read_ledger(_ledger(tmp_path)))
        assert rec["event"] == "denylist.change" and rec["detail"]["action"] == "add"
