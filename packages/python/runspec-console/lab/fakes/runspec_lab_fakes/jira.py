"""Fake Jira runnables — deterministic API-shaped payloads, no network."""

from __future__ import annotations

import hashlib
import json
import os

import runspec

_STATUSES = ["To Do", "In Progress", "Waiting for support", "Done"]
_PEOPLE = ["alice", "bob", "carol", "dave", "erin", "frank", "grace"]
_FILLER = (
    "Checked the host again; the mount is still at 97% and the rotation job "
    "did not run overnight. Attaching the df output and the cron log excerpt. "
)


def _n(seed: str, mod: int) -> int:
    return int(hashlib.sha256(seed.encode()).hexdigest(), 16) % mod


def _user(name: str) -> dict:
    return {
        "self": f"https://jira.example.com/rest/api/2/user?username={name}",
        "name": name,
        "key": f"JIRAUSER{_n(name, 99999)}",
        "emailAddress": f"{name}@example.com",
        "avatarUrls": {
            s: f"https://jira.example.com/secure/useravatar?size={s}&ownerId={name}"
            for s in ("48x48", "24x24", "16x16", "32x32")
        },
        "displayName": name.title(),
        "active": True,
        "timeZone": "Europe/London",
    }


def raw_issue(key: str, comments: int) -> dict:
    status = _STATUSES[_n(key, len(_STATUSES))]
    assignee = _PEOPLE[_n(key + "a", len(_PEOPLE))]
    return {
        "expand": "renderedFields,names,schema,operations,editmeta,changelog",
        "id": str(10000 + _n(key, 90000)),
        "self": f"https://jira.example.com/rest/api/2/issue/{key}",
        "key": key,
        "fields": {
            "summary": f"Disk usage alert on web-{_n(key, 20):02d}",
            "status": {
                "self": "https://jira.example.com/rest/api/2/status/3",
                "description": "",
                "iconUrl": "https://jira.example.com/images/icons/statuses/inprogress.png",
                "name": status,
                "id": "3",
                "statusCategory": {
                    "id": 4,
                    "key": "indeterminate",
                    "colorName": "yellow",
                },
            },
            "assignee": _user(assignee),
            "reporter": _user("monitoring"),
            "priority": {"name": "High", "id": "2"},
            "labels": ["disk", "web", "on-call"],
            "updated": "2026-10-15T09:12:44.000+0000",
            "created": "2026-10-01T07:00:00.000+0000",
            "description": _FILLER * 6,
            **{f"customfield_{10000 + i}": None for i in range(60)},
            "comment": {
                "comments": [
                    {
                        "self": f"https://jira.example.com/rest/api/2/issue/{key}/comment/{i}",
                        "id": str(i),
                        "author": _user(_PEOPLE[i % len(_PEOPLE)]),
                        "body": f"#{i}: " + _FILLER * 2,
                        "updateAuthor": _user(_PEOPLE[i % len(_PEOPLE)]),
                        "created": f"2026-10-{1 + i % 14:02d}T08:00:00.000+0000",
                        "updated": f"2026-10-{1 + i % 14:02d}T08:00:00.000+0000",
                    }
                    for i in range(comments)
                ],
                "maxResults": comments,
                "total": comments,
                "startAt": 0,
            },
        },
    }


def compact_issue(raw: dict, include: set[str]) -> dict:
    f = raw["fields"]
    out: dict = {
        "key": raw["key"],
        "summary": f["summary"],
        "status": f["status"]["name"],
        "assignee": f["assignee"]["name"],
        "priority": f["priority"]["name"],
        "updated": f["updated"],
    }
    comments = f["comment"]["comments"]
    if "all" in include:
        return raw
    if "description" in include:
        out["description"] = f["description"]
    shown = comments if "comments" in include else comments[-3:]
    out["comments"] = [
        {"author": c["author"]["name"], "created": c["created"], "body": c["body"]}
        for c in shown
    ]
    out["comment_count"] = len(comments)
    if len(shown) < len(comments):
        out["truncated"] = "comments: pass --include comments for all"
    return out


def _shape() -> str:
    return os.environ.get("FAKE_JIRA_SHAPE", "raw")


def issue_main() -> None:
    spec = runspec.parse("fake-jira-issue")
    key = spec.key.value
    raw = raw_issue(key, int(spec.comments.value))
    if _shape() == "compact":
        include = {
            s.strip() for s in (spec.include.value or "").split(",") if s.strip()
        }
        print(json.dumps(compact_issue(raw, include)))
    else:
        print(json.dumps(raw))


def search_main() -> None:
    spec = runspec.parse("fake-jira-search")
    keys = [f"OPS-{100 + i}" for i in range(int(spec.limit.value))]
    issues = [raw_issue(k, 5) for k in keys]
    if _shape() == "compact":
        issues = [compact_issue(i, set()) for i in issues]
        for i in issues:
            i.pop("comments", None)
            i.pop("truncated", None)
    print(json.dumps({"total": len(issues), "issues": issues}))
