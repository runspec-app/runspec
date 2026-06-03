"""Unit tests for the pure output parsers — cross-platform, no Windows needed."""

from __future__ import annotations

from runspec_windows.eventlog import normalize_events
from runspec_windows.network import parse_ipconfig, parse_netstat
from runspec_windows.processes import parse_tasklist_csv
from runspec_windows.services import normalize_services
from runspec_windows.sessions import parse_query_user
from runspec_windows.tasks import parse_schtasks_csv


def test_parse_tasklist_csv():
    text = '"System Idle Process","0","Services","0","8 K"\n"chrome.exe","4321","Console","1","123,456 K"\n"notepad.exe","99","Console","1","2,048 K"\n'
    rows = parse_tasklist_csv(text)
    assert len(rows) == 3
    assert rows[1] == {"name": "chrome.exe", "pid": 4321, "session": "Console", "mem_kb": 123456}
    assert rows[0]["mem_kb"] == 8


def test_parse_tasklist_csv_skips_malformed():
    assert parse_tasklist_csv('"bad","row"\n') == []


def test_parse_netstat_all_and_filters():
    text = (
        "\nActive Connections\n\n"
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       900\n"
        "  TCP    192.168.1.5:55000      52.1.2.3:443           ESTABLISHED     4321\n"
        "  UDP    0.0.0.0:500            *:*                                    700\n"
    )
    rows = parse_netstat(text, "all")
    assert len(rows) == 3
    assert rows[0] == {"proto": "TCP", "local": "0.0.0.0:135", "peer": "0.0.0.0:0", "state": "LISTENING", "pid": 900}
    assert rows[2] == {"proto": "UDP", "local": "0.0.0.0:500", "peer": "*:*", "state": "", "pid": 700}

    est = parse_netstat(text, "established")
    assert [r["pid"] for r in est] == [4321]

    listen = parse_netstat(text, "listening")
    assert [r["pid"] for r in listen] == [900]


def test_parse_ipconfig():
    text = (
        "Windows IP Configuration\n\n"
        "   Host Name . . . . . . . . . . . . : MYPC\n\n"
        "Ethernet adapter Ethernet:\n\n"
        "   Description . . . . . . . . . . . : Intel(R) Ethernet Connection\n"
        "   Physical Address. . . . . . . . . : 00-11-22-33-44-55\n"
        "   IPv4 Address. . . . . . . . . . . : 192.168.1.5(Preferred)\n"
        "   Default Gateway . . . . . . . . . : 192.168.1.1\n\n"
        "Wireless LAN adapter Wi-Fi:\n\n"
        "   Description . . . . . . . . . . . : Wireless-AC\n"
        "   Physical Address. . . . . . . . . : AA-BB-CC-DD-EE-FF\n"
    )
    adapters = parse_ipconfig(text)
    names = [a["adapter"] for a in adapters]
    assert names == ["Ethernet adapter Ethernet", "Wireless LAN adapter Wi-Fi"]
    eth = adapters[0]
    assert eth["description"] == "Intel(R) Ethernet Connection"
    assert eth["mac"] == "00-11-22-33-44-55"
    assert eth["ipv4"] == "192.168.1.5"
    assert eth["gateway"] == "192.168.1.1"
    assert adapters[1]["ipv4"] is None


def test_parse_schtasks_csv():
    text = (
        '"HostName","TaskName","Next Run Time","Status","Task To Run"\n'
        '"MYPC","\\Microsoft\\Backup","6/4/2026 2:00:00 AM","Ready","C:\\backup.exe"\n'
        '"HostName","TaskName","Next Run Time","Status","Task To Run"\n'
        '"MYPC","\\GameUpdate","N/A","Disabled","C:\\update.exe"\n'
    )
    rows = parse_schtasks_csv(text)
    assert len(rows) == 2
    assert rows[0] == {"name": "\\Microsoft\\Backup", "status": "Ready", "next_run": "6/4/2026 2:00:00 AM", "command": "C:\\backup.exe"}

    filtered = parse_schtasks_csv(text, "game")
    assert [r["name"] for r in filtered] == ["\\GameUpdate"]


def test_parse_query_user():
    text = (
        " USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME\n"
        ">jdoe                  console             1  Active      none   6/3/2026 8:00 AM\n"
        " admin                 rdp-tcp#1           2  Disc       1:23    6/2/2026 5:00 PM\n"
    )
    rows = parse_query_user(text)
    assert len(rows) == 2
    assert rows[0]["username"] == "jdoe"
    assert rows[0]["current"] is True
    assert rows[0]["state"] == "Active"
    assert rows[1]["username"] == "admin"
    assert rows[1]["current"] is False


def test_normalize_services_single_and_filter():
    single = {"Name": "Spooler", "DisplayName": "Print Spooler", "status": "Running", "start_type": "Automatic"}
    rows = normalize_services(single)
    assert rows == [{"name": "Spooler", "display_name": "Print Spooler", "status": "Running", "start_type": "Automatic"}]

    many = [
        {"Name": "A", "DisplayName": "A", "status": "Running", "start_type": "Automatic"},
        {"Name": "B", "DisplayName": "B", "status": "Stopped", "start_type": "Manual"},
    ]
    assert [r["name"] for r in normalize_services(many, "running")] == ["A"]
    assert [r["name"] for r in normalize_services(many, "stopped")] == ["B"]
    assert len(normalize_services(many, "all")) == 2


def test_normalize_events_truncates_and_shapes():
    data = [{"time": "2026-06-03T08:00:00", "level": "Error", "Id": 7000, "ProviderName": "SCM", "message": "x" * 800}]
    rows = normalize_events(data)
    assert rows[0]["id"] == 7000
    assert rows[0]["provider"] == "SCM"
    assert rows[0]["message"].endswith("…")
    assert len(rows[0]["message"]) == 501
