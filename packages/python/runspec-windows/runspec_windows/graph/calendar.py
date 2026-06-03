"""Calendar runnables (Microsoft Graph): calendar-upcoming, calendar-today, next-meeting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import runspec as rs

from runspec_windows.graph._emit import run_graph
from runspec_windows.graph.client import graph_get

_EVENT_SELECT = "subject,start,end,organizer,location,onlineMeeting,isOnlineMeeting,webLink"


def format_events(data: dict) -> list[dict]:
    """Reshape a Graph calendarView response (``{"value": [...]}``) into rows."""
    rows = []
    for ev in data.get("value", []):
        organizer = ((ev.get("organizer") or {}).get("emailAddress") or {}).get("name")
        online = ev.get("onlineMeeting") or {}
        rows.append(
            {
                "subject": ev.get("subject"),
                "start": (ev.get("start") or {}).get("dateTime"),
                "end": (ev.get("end") or {}).get("dateTime"),
                "organizer": organizer,
                "location": (ev.get("location") or {}).get("displayName"),
                "is_online": ev.get("isOnlineMeeting"),
                "join_url": online.get("joinUrl"),
                "web_link": ev.get("webLink"),
            }
        )
    return rows


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _calendar_view(start: datetime, end: datetime, count: int) -> list[dict]:
    return format_events(
        graph_get(
            "/me/calendarView",
            params={
                "startDateTime": _iso(start),
                "endDateTime": _iso(end),
                "$select": _EVENT_SELECT,
                "$orderby": "start/dateTime",
                "$top": count,
            },
        )
    )


def main_calendar_upcoming() -> None:
    spec = rs.parse("calendar-upcoming")
    days = int(spec.days)
    count = int(spec.count)
    now = datetime.now(timezone.utc)
    run_graph(lambda: _calendar_view(now, now + timedelta(days=days), count))


def main_calendar_today() -> None:
    rs.parse("calendar-today")
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    run_graph(lambda: _calendar_view(start, start + timedelta(days=1), 50))


def main_next_meeting() -> None:
    rs.parse("next-meeting")
    now = datetime.now(timezone.utc)

    def produce() -> dict:
        events = _calendar_view(now, now + timedelta(days=30), 1)
        return events[0] if events else {"message": "No upcoming meetings in the next 30 days"}

    run_graph(produce)
