"""Webhooks with the real daemon, the real listener and real ``runspec exec``
runs, in a throwaway venv (see procenv.py). The full flow needs a non-root
account (webhooks refuse to run as root); as root only the refusal is checked."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import procenv as pe  # noqa: E402

from runspec_scheduler import hookconfig as hc  # noqa: E402

pytestmark = [pytest.mark.process, pytest.mark.skipif(sys.platform == "win32", reason="POSIX daemon")]

TOKEN = "proc-test-token"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def base(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return pe.make_venv(tmp_path_factory.mktemp("hookvenv"))


@pytest.fixture
def venv(base: Path) -> Any:
    pe.kill_leftovers(base)
    for name in ("runspec_scheduler.toml", "runspec_scheduler.lock"):
        (base / name).unlink(missing_ok=True)
    for name in ("logs", "hooks"):
        shutil.rmtree(base / name, ignore_errors=True)
    procs: list[pe.Proc] = []
    yield base, procs
    while procs:
        procs.pop().stop(5)
    pe.kill_leftovers(base)


def config(venv: Path, port: int) -> str:
    marks = venv / "marks"
    marks.mkdir(exist_ok=True)
    return f"""
    [scheduler]
    backend = "cron"

    [hooks]
    enabled = true
    port = {port}

    [[hook]]
    name = "alerts"
    token_sha256 = "{hc.sha256(TOKEN)}"
    split = "alerts"
    fields = {{ alertname = "labels.alertname", status = "status" }}

    [[hook.rule]]
    name = "resolved"
    match = {{ status = "^resolved$" }}
    skip = true

    [[hook.rule]]
    name = "safe"
    match = {{ alertname = "^(?P<kind>Disk)" }}
    run = "demo"
    args = {{ marker = "{marks}/{{{{ kind | lower }}}}" }}

    [[hook.rule]]
    name = "careful"
    match = {{ alertname = "^Careful" }}
    run = "demo"
    args = {{ careful = "{{{{ alertname }}}}", marker = "{marks}/careful" }}

    [[hook.rule]]
    name = "never"
    match = {{ alertname = "^Reckless" }}
    run = "demo"
    args = {{ reckless = "yes" }}
    """


def start(venv: Path, procs: list[pe.Proc]) -> pe.Proc:
    p = pe.Proc(venv)
    procs.append(p)
    pe.wait_for(lambda: (pe.state(venv) or {}).get("pid") == p.pid or p.p.poll() is not None, 20, what="the daemon to start")
    return p


def post(port: int, payload: Any, token: str = TOKEN) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/hook/alerts", body=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    resp = conn.getresponse()
    text = resp.read().decode()
    conn.close()
    return resp.status, text


def verb(venv: Path, *args: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    full = {k: v for k, v in os.environ.items() if not k.startswith("RUNSPEC_")}
    full.update(env or {})
    done = subprocess.run([str(venv / "bin" / "runspec-scheduler"), *args, "--json"], capture_output=True, text=True, env=full, cwd=str(venv), timeout=60, check=False)
    return json.loads(done.stdout)


def hook_runs(venv: Path) -> list[dict[str, Any]]:
    return [r for r in pe.runs(venv) if r.get("label") == "hook:alerts"]


@pytest.mark.skipif(not IS_ROOT, reason="checks the root refusal")
def test_refuses_as_root(venv: Any) -> None:
    path, procs = venv
    port = free_port()
    pe.write_config(path, config(path, port))
    start(path, procs)
    pe.wait_for(lambda: ((pe.state(path) or {}).get("hooks") or {}).get("error"), 20, what="the hooks error")
    assert "root" in pe.state(path)["hooks"]["error"]  # type: ignore[index]
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


@pytest.mark.skipif(IS_ROOT, reason="webhooks refuse to run as root")
def test_webhook_end_to_end(venv: Any) -> None:
    path, procs = venv
    port = free_port()
    pe.write_config(path, config(path, port))
    start(path, procs)
    pe.wait_for(lambda: ((pe.state(path) or {}).get("hooks") or {}).get("listening"), 20, what="the listener")

    assert post(port, {"alerts": []}, token="wrong")[0] == 401
    status, text = post(
        port,
        {
            "alerts": [
                {"status": "firing", "labels": {"alertname": "DiskFull"}},
                {"status": "firing", "labels": {"alertname": "CarefulThing"}},
                {"status": "resolved", "labels": {"alertname": "DiskFull"}},
                {"status": "firing", "labels": {"alertname": "RecklessThing"}},
            ]
        },
    )
    assert status == 202 and json.loads(text)["status"] == "queued"

    # the autonomous item runs at once, recorded like any run
    pe.wait_for(lambda: (path / "marks" / "disk").exists(), 30, what="the autonomous run")
    runs = pe.wait_for(lambda: [r for r in hook_runs(path) if r.get("status") != "running"], 30, what="the run row")
    assert runs[0]["runnable"] == "demo" and runs[0]["exit_code"] == 0

    listing = pe.wait_for(lambda: [d for d in verb(path, "hooks", "deliveries")["deliveries"] if d["state"] not in ("queued", "running", "waiting")] or None, 20, what="planned deliveries")
    by_rule = {d["rule"]: d for d in verb(path, "hooks", "deliveries")["deliveries"]}
    assert by_rule["careful"]["state"] == "pending" and by_rule["careful"]["autonomy"] == "confirm"
    assert by_rule["resolved"]["state"] == "skipped"
    assert by_rule["never"]["state"] == "refused" and "manual" in by_rule["never"]["reason"]
    assert listing

    # approving runs it, as the venv owner, recorded with the approver
    pending_id = by_rule["careful"]["id"]
    out = verb(path, "hooks", "approve", "--id", str(pending_id), "--note", "ok", env={"SUDO_USER": "ann"})
    assert out["won"] and out["delivery"]["decided_by"] == "ann"
    pe.wait_for(lambda: (path / "marks" / "careful").exists(), 30, what="the approved run")
    assert verb(path, "hooks", "deny", "--id", str(pending_id), env={"SUDO_USER": "bob"})["ok"] is False

    # a follower sees the whole story
    argv = [str(path / "bin" / "runspec-scheduler"), "hooks", "follow", "--since", "0", "--once", "--who", "ann"]
    follow = subprocess.run(argv, capture_output=True, text=True, cwd=str(path), timeout=60, check=False)
    kinds = [json.loads(line)["kind"] for line in follow.stdout.splitlines()]
    assert kinds[0] == "hello" and "approved" in kinds and "split" in kinds

    # status reports the listener
    status_out = verb(path, "status")
    assert status_out["hooks"]["daemon"]["listening"].startswith("http://127.0.0.1:")
