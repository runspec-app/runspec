"""Tests for run-playbook.

The pure helpers are tested directly. The end-to-end path is exercised
in-process with a stubbed ``run_ansible`` (so no Ansible install is needed):
``rs.parse`` still reads the bundled runspec.toml because the finder walks up
from the calling module's directory.
"""

import json

import pytest

from runspec_playbook import run_playbook

# ── pure helpers ──────────────────────────────────────────────────────────────


def test_compose_package_spec_plain() -> None:
    assert run_playbook.compose_package_spec("myapp", "1.4.2") == "myapp==1.4.2"


def test_compose_package_spec_preserves_extras() -> None:
    assert run_playbook.compose_package_spec("myapp[postgres,redis]", "1.4.2") == "myapp[postgres,redis]==1.4.2"


def test_compose_package_spec_requires_package() -> None:
    with pytest.raises(ValueError):
        run_playbook.compose_package_spec("", "1.0.0")


def test_parse_extra_vars_splits_on_first_equals() -> None:
    assert run_playbook.parse_extra_vars(["venv_path=/opt/x/venv", "url=https://h/p?a=b"]) == {
        "venv_path": "/opt/x/venv",
        "url": "https://h/p?a=b",
    }


def test_parse_extra_vars_rejects_bare_token() -> None:
    with pytest.raises(ValueError):
        run_playbook.parse_extra_vars(["novalue"])


def test_summarize_stats_totals_across_hosts() -> None:
    stats = {
        "ok": {"localhost": 3},
        "changed": {"localhost": 2},
        "failures": {"localhost": 1},
        "dark": {},
    }
    assert run_playbook.summarize_stats(stats) == {
        "ok": 3,
        "changed": 2,
        "failed": 1,
        "unreachable": 0,
        "skipped": 0,
    }


# ── end-to-end (stubbed Ansible) ────────────────────────────────────────────────


def _stub_runner(monkeypatch, captured, *, status="successful", rc=0, stats=None):
    def fake_run_ansible(playbook_name, extravars, *, check, verbosity, tags, skip_tags, envvars, sent=None):
        captured.update(
            sent=sent,
            envvars=envvars,
            playbook_name=playbook_name,
            extravars=extravars,
            check=check,
            verbosity=verbosity,
            tags=tags,
            skip_tags=skip_tags,
        )
        return {"status": status, "rc": rc, "stats": stats if stats is not None else {"changed": {"localhost": 1}, "ok": {"localhost": 2}}}

    monkeypatch.setattr(run_playbook, "run_ansible", fake_run_ansible)


def test_main_builds_spec_and_reports_change(monkeypatch, capsys) -> None:
    captured: dict = {}
    _stub_runner(monkeypatch, captured)
    monkeypatch.setattr("sys.argv", ["run-playbook", "myapp[pg]", "1.2.3", "--check", "--extra-var", "venv_path=/opt/x/venv"])

    with pytest.raises(SystemExit) as exc:
        run_playbook.main_run_playbook()
    assert exc.value.code == 0

    out = json.loads(capsys.readouterr().out)
    assert out["package"] == "myapp[pg]==1.2.3"
    assert out["changed"] is True
    assert out["check"] is True
    assert out["status"] == "successful"
    assert out["stats"]["changed"] == 1

    assert captured["extravars"]["deploy_package_spec"] == "myapp[pg]==1.2.3"
    assert captured["extravars"]["deploy_package"] == "myapp[pg]"
    assert captured["extravars"]["venv_path"] == "/opt/x/venv"
    assert captured["check"] is True


def test_main_propagates_failure_exit_code(monkeypatch, capsys) -> None:
    captured: dict = {}
    _stub_runner(monkeypatch, captured, status="failed", rc=2, stats={"failures": {"localhost": 1}})
    monkeypatch.setattr("sys.argv", ["run-playbook", "myapp", "9.9.9"])

    with pytest.raises(SystemExit) as exc:
        run_playbook.main_run_playbook()
    assert exc.value.code == 2

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "failed"
    assert out["changed"] is False
    assert out["stats"]["failed"] == 1


def test_main_reports_bad_extra_var(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["run-playbook", "myapp", "1.0.0", "--extra-var", "oops"])
    with pytest.raises(SystemExit) as exc:
        run_playbook.main_run_playbook()
    assert exc.value.code == 2
    out = json.loads(capsys.readouterr().out)
    assert "key=value" in out["error"]


# ── a repo sent by the console ────────────────────────────────────────────────

import base64  # noqa: E402
import gzip  # noqa: E402
import io  # noqa: E402

from runspec_playbook import source  # noqa: E402

# Shared test vector — the console's playbook_repo tests assert the same hash, so
# the two sides can't drift apart on the content-hash format.
VECTOR_FILES = {"site.yml": "LSBob3N0czogYWxsCg==", "roles/web/tasks/main.yml": "LSBwaW5nOgo="}
VECTOR_SHA = "48adc37d996b7944c747d03fd5c97d26b03a7baa14ea736027ea75249341cd6f"


def _payload(files_b64=None, **extra) -> tuple[bytes, str]:
    files_b64 = VECTOR_FILES if files_b64 is None else files_b64
    doc = {"v": 1, "repo": "homelab", "commit": "abc123def4567890", "dirty": False, "files": files_b64, **extra}
    payload = base64.b64encode(gzip.compress(json.dumps(doc).encode()))
    return payload, f"stdin:{len(payload)}:{source.content_hash(files_b64)}"


def test_content_hash_matches_shared_vector() -> None:
    assert source.content_hash(VECTOR_FILES) == VECTOR_SHA


def test_receive_decodes_files_and_inventory() -> None:
    payload, header = _payload(host="web-01", groups=["web", "bad group", "home"])
    sent = source.receive(header, io.BytesIO(payload + b"trailing"))
    assert sent.repo == "homelab"
    assert sent.files["site.yml"] == b"- hosts: all\n"
    assert sent.files["roles/web/tasks/main.yml"] == b"- ping:\n"
    assert sent.host == "web-01"
    assert sent.groups == ("web", "home")  # unsafe name dropped


def test_receive_rejects_tampered_content() -> None:
    payload, header = _payload()
    bad = header.rsplit(":", 1)[0] + ":" + "0" * 64
    with pytest.raises(source.SourceError, match="sha256"):
        source.receive(bad, io.BytesIO(payload))


def test_receive_rejects_short_stdin() -> None:
    payload, header = _payload()
    with pytest.raises(source.SourceError, match="cut short"):
        source.receive(header, io.BytesIO(payload[:10]))


@pytest.mark.parametrize("rel", ["../evil.yml", "/etc/passwd", "a/../../b", "C:/x", "./a"])
def test_unsafe_paths_rejected(rel) -> None:
    files = {rel: "eA=="}
    payload, header = _payload(files)
    with pytest.raises(source.SourceError):
        source.receive(header, io.BytesIO(payload))


def test_write_files(tmp_path) -> None:
    payload, header = _payload()
    sent = source.receive(header, io.BytesIO(payload))
    source.write_files(sent, tmp_path)
    assert (tmp_path / "roles/web/tasks/main.yml").read_bytes() == b"- ping:\n"


def test_local_inventory_uses_console_name_and_groups() -> None:
    payload, header = _payload(host="web-01", groups=["web"])
    sent = source.receive(header, io.BytesIO(payload))
    inv = run_playbook.local_inventory(sent)
    assert inv["all"]["hosts"] == {"web-01": {"ansible_connection": "local"}}
    assert inv["all"]["children"] == {"web": {"hosts": {"web-01": {}}}}
    assert run_playbook.local_inventory(None) == {"all": {"hosts": {"localhost": {"ansible_connection": "local"}}}}


class _Stdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = io.BytesIO(data)

    def isatty(self) -> bool:
        return False


def test_main_runs_a_sent_repo(monkeypatch, capsys) -> None:
    captured: dict = {}
    _stub_runner(monkeypatch, captured)
    payload, header = _payload()
    monkeypatch.setenv(source.ENV_VAR, header)
    monkeypatch.setattr("sys.stdin", _Stdin(payload))
    monkeypatch.setattr("sys.argv", ["run-playbook", "--repo", "homelab", "--playbook", "site.yml", "--check"])

    with pytest.raises(SystemExit) as exc:
        run_playbook.main_run_playbook()
    assert exc.value.code == 0

    out = json.loads(capsys.readouterr().out)
    assert out["repo"] == "homelab"
    assert out["commit"] == "abc123def456"
    assert out["dirty"] is False
    assert "package" not in out
    assert captured["sent"].files["site.yml"] == b"- hosts: all\n"
    assert captured["extravars"] == {}
    # the header never reaches Ansible's environment
    assert source.ENV_VAR not in captured["envvars"]


def test_main_repo_without_sent_payload_is_refused(monkeypatch, capsys) -> None:
    monkeypatch.delenv(source.ENV_VAR, raising=False)
    monkeypatch.setattr("sys.argv", ["run-playbook", "--repo", "homelab", "--playbook", "site.yml"])
    with pytest.raises(SystemExit) as exc:
        run_playbook.main_run_playbook()
    assert exc.value.code == 2
    assert "Repos tab" in json.loads(capsys.readouterr().out)["error"]


def test_main_repo_name_mismatch_is_refused(monkeypatch, capsys) -> None:
    payload, header = _payload()
    monkeypatch.setenv(source.ENV_VAR, header)
    monkeypatch.setattr("sys.stdin", _Stdin(payload))
    monkeypatch.setattr("sys.argv", ["run-playbook", "--repo", "other", "--playbook", "site.yml"])
    with pytest.raises(SystemExit) as exc:
        run_playbook.main_run_playbook()
    assert exc.value.code == 2
    assert "other" in json.loads(capsys.readouterr().out)["error"]


def test_main_bundled_deploy_needs_package(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["run-playbook"])
    with pytest.raises(SystemExit) as exc:
        run_playbook.main_run_playbook()
    assert exc.value.code == 2
    assert "package" in json.loads(capsys.readouterr().out)["error"]
