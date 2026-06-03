"""On a non-Windows host, the OS-specific runnables must emit a JSON error and
exit 1 (not raise a traceback). Skipped on Windows, where they really run."""

from __future__ import annotations

import json
import sys

import pytest

from runspec_windows import network, sessions, software, system_info, tasks

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="guard only applies off-Windows")

# Runnables with no required args, so rs.parse() succeeds and we reach ensure_windows().
CASES = [
    ("system-info", system_info.main_system_info),
    ("uptime", system_info.main_uptime),
    ("check-memory", system_info.main_check_memory),
    ("disk-usage", system_info.main_disk_usage),
    ("list-adapters", network.main_list_adapters),
    ("flush-dns", network.main_flush_dns),
    ("who", sessions.main_who),
    ("current-user", sessions.main_current_user),
    ("installed-software", software.main_installed_software),
    ("list-scheduled-tasks", tasks.main_list_scheduled_tasks),
]


@pytest.mark.parametrize("name,fn", CASES, ids=[c[0] for c in CASES])
def test_runnable_errors_cleanly_off_windows(name, fn, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [name])
    with pytest.raises(SystemExit) as exc:
        fn()
    assert exc.value.code == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "requires Windows" in payload["error"]
