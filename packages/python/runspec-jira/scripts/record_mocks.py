"""Re-record runspec-jira's shipped mock responses (runspec_jira/mocks/jira.json).

Runs a fake Jira Data Center that serves made-up issues, then records each call
below with ``runspec mock record`` — so the responses come from the real
runspec-jira code, and no real Jira (or real data) is involved.

    # from packages/python/runspec-jira, in a venv with runspec + runspec-jira installed editable
    python scripts/record_mocks.py

Delete ``runspec_jira/mocks/jira.json`` first for a clean set; otherwise each
call replaces its existing entry. Afterwards the fake server's address is
replaced with ``https://jira.example.com`` and the keys in ``WIDEN`` are
loosened, so e.g. a comment with any text on OPS-101 gets the recorded answer.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from typing import Any
from urllib.parse import urlparse

CALLS = [
    ["get-issue", "--key", "OPS-101"],
    ["get-issue", "--key", "OPS-102"],
    ["get-issue", "--key", "OPS-103"],
    ["get-issue", "--key", "OPS-404"],
    ["search", "--jql", "project = OPS AND status != Done"],
    ["list-transitions", "--key", "OPS-101"],
    ["transition-issue", "--key", "OPS-101", "--status", "In Progress"],
    ["add-comment", "--key", "OPS-101", "--body", "Cleared old logs under /var/log/app; /var is now at 61%."],
    ["get-all-projects"],
]

# Recorded call → the args its entry answers. Fewer args = matches more calls.
WIDEN = {
    ("get-issue", (("key", "OPS-404"),)): ({}, "any other key: not found"),
    ("search", (("jql", "project = OPS AND status != Done"),)): ({}, "any JQL: the three open OPS incidents"),
    ("add-comment", (("body", "Cleared old logs under /var/log/app; /var is now at 61%."), ("key", "OPS-101"))): ({"key": "OPS-101"}, "any comment text"),
}
MOCK_FILE = os.path.join(os.path.dirname(__file__), "..", "runspec_jira", "mocks", "jira.json")
PUBLIC_URL = "https://jira.example.com"


def _person(name: str, display: str) -> dict[str, Any]:
    return {"name": name, "key": name, "displayName": display, "emailAddress": f"{name}@example.com", "active": True}


SRE = _person("asmith", "Alex Smith")
OPS = _person("jlee", "Jordan Lee")
BOT = _person("monitoring", "Monitoring Bot")


def _status(name: str, category: str) -> dict[str, Any]:
    return {"name": name, "statusCategory": {"key": category, "name": category}}


def _comment(cid: str, who: dict[str, Any], body: str, when: str) -> dict[str, Any]:
    return {"id": cid, "author": who, "body": body, "created": when, "updated": when}


def _issue(
    key: str,
    summary: str,
    status: dict[str, Any],
    priority: str,
    assignee: Any,
    created: str,
    updated: str,
    desc: str,
    comments: list[Any],
    links: list[Any] | None = None,
    labels: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "key": key,
        "id": str(10000 + int(key.split("-")[1])),
        "fields": {
            "summary": summary,
            "status": status,
            "issuetype": {"name": "Incident"},
            "priority": {"name": priority},
            "assignee": assignee,
            "reporter": BOT,
            "created": created,
            "updated": updated,
            "resolution": None,
            "labels": list(labels),
            "components": [{"name": "Infrastructure"}],
            "fixVersions": [],
            "parent": None,
            "description": desc,
            "comment": {"comments": comments, "total": len(comments), "maxResults": 50, "startAt": 0},
            "issuelinks": links or [],
            "subtasks": [],
            "attachment": [],
        },
    }


ISSUES = {
    "OPS-101": _issue(
        "OPS-101",
        "Disk full on web-01: /var at 97%",
        _status("Open", "new"),
        "High",
        None,
        "2026-10-17T06:12:00.000+0000",
        "2026-10-17T06:40:00.000+0000",
        "Alert: filesystem /var on web-01 is at 97% (threshold 90%).\nStarted 06:10 UTC. Most of the growth is under /var/log/app.",
        [_comment("2001", BOT, "Still firing: /var at 97% on web-01.", "2026-10-17T06:40:00.000+0000")],
        labels=("disk", "web-01"),
    ),
    "OPS-102": _issue(
        "OPS-102",
        "Nightly backup failed on db-02",
        _status("In Progress", "indeterminate"),
        "Medium",
        SRE,
        "2026-10-16T02:30:00.000+0000",
        "2026-10-17T09:05:00.000+0000",
        "backup-db exited 2 on db-02 at 02:15 UTC: 'pg_dump: connection to server failed: timeout'.",
        [_comment("2002", SRE, "The 02:00 maintenance window overlapped the backup. Re-running tonight with --retry 3.", "2026-10-17T09:05:00.000+0000")],
        links=[{"type": {"name": "Relates", "outward": "relates to"}, "outwardIssue": {"key": "OPS-99", "fields": {"summary": "Move the db maintenance window", "status": {"name": "Done"}}}}],
        labels=("backup", "db-02"),
    ),
    "OPS-103": _issue(
        "OPS-103",
        "TLS certificate for api.example.com expires in 7 days",
        _status("Open", "new"),
        "High",
        OPS,
        "2026-10-17T08:00:00.000+0000",
        "2026-10-17T08:00:00.000+0000",
        "cert-check reports that the api.example.com leaf certificate expires on 2026-10-24.",
        [],
        labels=("tls",),
    ),
}
TRANSITIONS = {
    "transitions": [
        {"id": "11", "name": "Start Progress", "to": {"name": "In Progress"}},
        {"id": "21", "name": "Resolve", "to": {"name": "Resolved"}},
        {"id": "31", "name": "Close", "to": {"name": "Closed"}},
    ]
}
PROJECTS = [
    {"id": "10100", "key": "OPS", "name": "Operations", "projectTypeKey": "software"},
    {"id": "10101", "key": "HELP", "name": "IT Help Desk", "projectTypeKey": "service_desk"},
]


class FakeJira(http.server.BaseHTTPRequestHandler):
    def _send(self, code: int, obj: Any = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if obj is not None:
            self.wfile.write(json.dumps(obj).encode())

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        path = urlparse(self.path).path
        m = re.fullmatch(r"/rest/api/2/issue/([A-Z]+-\d+)(/\w+)?", path)
        if m:
            key, sub = m.group(1), m.group(2)
            if key not in ISSUES:
                return self._send(404, {"errorMessages": ["Issue Does Not Exist"], "errors": {}})
            if sub is None and self.command == "GET":
                return self._send(200, ISSUES[key])
            if sub == "/transitions":
                return self._send(200, TRANSITIONS) if self.command == "GET" else self._send(204)
            if sub == "/comment" and self.command == "POST":
                return self._send(201, _comment("2101", SRE, body["body"], "2026-10-17T10:00:00.000+0000"))
        if path == "/rest/api/2/search":
            issues = [{"key": k, "id": v["id"], "fields": v["fields"]} for k, v in ISSUES.items()]
            return self._send(200, {"startAt": 0, "maxResults": 25, "total": len(issues), "issues": issues})
        if path == "/rest/api/2/project":
            return self._send(200, PROJECTS)
        self._send(404, {"errorMessages": [f"no fake for {self.command} {path}"]})

    do_GET = do_POST = do_PUT = do_DELETE = _handle

    def log_message(self, *args: Any) -> None:
        pass


def main() -> None:
    server = http.server.HTTPServer(("127.0.0.1", 0), FakeJira)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if not k.startswith("JIRA_")}
    env.update({"JIRA_URL": f"http://127.0.0.1:{server.server_port}", "JIRA_PERSONAL_TOKEN": "fake", "RUNSPEC_AUDIT": "off"})
    runspec = shutil.which("runspec") or "runspec"
    failed = 0
    for call in CALLS:
        rc = subprocess.call([runspec, "mock", "record", "jira", "--to", "package", "--", *call], env=env)
        failed += rc != 0
    server.shutdown()
    _tidy(f"http://127.0.0.1:{server.server_port}")
    sys.exit(1 if failed else 0)


def _tidy(base_url: str) -> None:
    with open(MOCK_FILE, encoding="utf-8") as f:
        data = json.loads(f.read().replace(base_url, PUBLIC_URL))
    for entry in data["entries"]:
        key = (entry["command"][0], tuple(sorted(entry["args"].items())))
        if key in WIDEN:
            entry["args"], entry["note"] = WIDEN[key]
    with open(MOCK_FILE, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
