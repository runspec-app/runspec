"""Graph tests — pure formatters plus mocked auth/client paths (no real network)."""

from __future__ import annotations

import pytest

from runspec_windows.graph import GraphAuthError, GraphDepError, GraphError, account, auth, calendar, client, files, outlook, teams

# ── pure formatters ────────────────────────────────────────────────────────────


def test_outlook_format_messages():
    data = {
        "value": [
            {
                "subject": "Hello",
                "from": {"emailAddress": {"name": "Ada", "address": "ada@x.com"}},
                "receivedDateTime": "2026-06-03T08:00:00Z",
                "isRead": False,
                "bodyPreview": "hi there",
                "webLink": "https://outlook/1",
            }
        ]
    }
    rows = outlook.format_messages(data)
    assert rows == [
        {
            "subject": "Hello",
            "from": "Ada",
            "from_address": "ada@x.com",
            "received": "2026-06-03T08:00:00Z",
            "is_read": False,
            "preview": "hi there",
            "web_link": "https://outlook/1",
        }
    ]


def test_outlook_format_messages_handles_missing_from():
    rows = outlook.format_messages({"value": [{"subject": "no sender"}]})
    assert rows[0]["from"] is None and rows[0]["from_address"] is None


def test_outlook_format_folders():
    data = {"value": [{"displayName": "Inbox", "unreadItemCount": 3, "totalItemCount": 50, "id": "abc"}]}
    assert outlook.format_folders(data) == [{"name": "Inbox", "unread": 3, "total": 50, "id": "abc"}]


def test_teams_format_chats_topic_and_members():
    data = {
        "value": [
            {"id": "1", "chatType": "group", "topic": "Project X", "lastUpdatedDateTime": "t1"},
            {"id": "2", "chatType": "oneOnOne", "members": [{"displayName": "Ada"}, {"displayName": "Bob"}], "lastUpdatedDateTime": "t2"},
        ]
    }
    rows = teams.format_chats(data)
    assert rows[0]["topic"] == "Project X"
    assert rows[1]["topic"] == "Ada, Bob"


def test_teams_format_messages():
    data = {"value": [{"from": {"user": {"displayName": "Ada"}}, "createdDateTime": "t", "messageType": "message", "body": {"content": "hi"}}]}
    assert teams.format_messages(data) == [{"from": "Ada", "created": "t", "type": "message", "body": "hi"}]


def test_calendar_format_events():
    data = {
        "value": [
            {
                "subject": "Standup",
                "start": {"dateTime": "2026-06-03T09:00:00", "timeZone": "UTC"},
                "end": {"dateTime": "2026-06-03T09:15:00", "timeZone": "UTC"},
                "organizer": {"emailAddress": {"name": "Ada", "address": "ada@x.com"}},
                "location": {"displayName": "Room 1"},
                "isOnlineMeeting": True,
                "onlineMeeting": {"joinUrl": "https://teams/meet/1"},
                "webLink": "https://outlook/cal/1",
            }
        ]
    }
    rows = calendar.format_events(data)
    assert rows == [
        {
            "subject": "Standup",
            "start": "2026-06-03T09:00:00",
            "end": "2026-06-03T09:15:00",
            "organizer": "Ada",
            "location": "Room 1",
            "is_online": True,
            "join_url": "https://teams/meet/1",
            "web_link": "https://outlook/cal/1",
        }
    ]


def test_calendar_format_events_handles_missing():
    rows = calendar.format_events({"value": [{"subject": "bare"}]})
    assert rows[0]["start"] is None and rows[0]["organizer"] is None and rows[0]["join_url"] is None


def test_files_format_items():
    data = {
        "value": [
            {
                "name": "Budget.xlsx",
                "size": 20480,
                "lastModifiedDateTime": "2026-06-01T12:00:00Z",
                "webUrl": "https://onedrive/Budget.xlsx",
                "id": "01ABC",
                "file": {"mimeType": "application/vnd.ms-excel"},
                "parentReference": {"path": "/drive/root:/Documents"},
            },
            {
                "name": "Reports",
                "id": "01FLD",
                "folder": {"childCount": 3},
                "parentReference": {"path": "/drive/root:"},
            },
        ]
    }
    rows = files.format_items(data)
    assert rows[0] == {
        "name": "Budget.xlsx",
        "is_folder": False,
        "size": 20480,
        "last_modified": "2026-06-01T12:00:00Z",
        "web_url": "https://onedrive/Budget.xlsx",
        "item_id": "01ABC",
        "path": "/drive/root:/Documents",
    }
    assert rows[1]["is_folder"] is True and rows[1]["name"] == "Reports"


def test_account_whoami():
    me = {"displayName": "Ada L", "userPrincipalName": "ada@x.com", "mail": "ada@x.com", "jobTitle": "Eng", "id": "42"}
    assert account._whoami(me)["display_name"] == "Ada L"
    assert account._whoami(me)["job_title"] == "Eng"


# ── auth ─────────────────────────────────────────────────────────────────────


class _FakeCache:
    has_state_changed = False


class _FakeApp:
    def __init__(self, accounts, silent=None):
        self._accounts = accounts
        self._silent = silent

    def get_accounts(self):
        return self._accounts

    def acquire_token_silent(self, scopes, account):
        return self._silent


def test_get_token_no_accounts_raises(monkeypatch):
    monkeypatch.setattr(auth, "_load_cache", lambda: _FakeCache())
    monkeypatch.setattr(auth, "_save_cache", lambda cache: None)
    monkeypatch.setattr(auth, "_build_app", lambda cache: _FakeApp(accounts=[]))
    with pytest.raises(GraphAuthError):
        auth.get_token()


def test_get_token_silent_success(monkeypatch):
    monkeypatch.setattr(auth, "_load_cache", lambda: _FakeCache())
    monkeypatch.setattr(auth, "_save_cache", lambda cache: None)
    monkeypatch.setattr(auth, "_build_app", lambda cache: _FakeApp(accounts=["acc"], silent={"access_token": "TOK"}))
    assert auth.get_token() == "TOK"


def test_client_id_missing_raises(monkeypatch):
    monkeypatch.delenv("RUNSPEC_GRAPH_CLIENT_ID", raising=False)
    monkeypatch.setattr(auth, "_read_graph_toml", lambda: {})
    from runspec_windows.graph import GraphConfigError

    with pytest.raises(GraphConfigError):
        auth.client_id()


# ── client ─────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeHttpx:
    def __init__(self, resp):
        self._resp = resp
        self.last = None

    def get(self, url, headers=None, params=None, timeout=None):
        self.last = {"url": url, "headers": headers, "params": params}
        return self._resp


def test_graph_get_success(monkeypatch):
    fake = _FakeHttpx(_Resp(200, {"value": [1, 2]}))
    monkeypatch.setattr(client, "get_token", lambda scopes=None: "TOK")
    monkeypatch.setattr(client, "httpx", fake)
    out = client.graph_get("/me/messages", params={"$top": 5})
    assert out == {"value": [1, 2]}
    assert fake.last["url"].endswith("/me/messages")
    assert fake.last["headers"]["Authorization"] == "Bearer TOK"


def test_graph_get_search_sets_consistency_header(monkeypatch):
    fake = _FakeHttpx(_Resp(200, {"value": []}))
    monkeypatch.setattr(client, "get_token", lambda scopes=None: "TOK")
    monkeypatch.setattr(client, "httpx", fake)
    client.graph_get("/me/messages", params={"$search": '"hi"'})
    assert fake.last["headers"]["ConsistencyLevel"] == "eventual"


def test_graph_get_error_raises(monkeypatch):
    fake = _FakeHttpx(_Resp(403, {"error": {"message": "Forbidden"}}))
    monkeypatch.setattr(client, "get_token", lambda scopes=None: "TOK")
    monkeypatch.setattr(client, "httpx", fake)
    with pytest.raises(GraphError) as exc:
        client.graph_get("/me")
    assert "Forbidden" in str(exc.value)


def test_graph_get_missing_httpx(monkeypatch):
    monkeypatch.setattr(client, "httpx", None)
    with pytest.raises(GraphDepError):
        client.graph_get("/me")
