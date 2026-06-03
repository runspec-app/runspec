"""Account runnables (Microsoft Graph): graph-login, graph-whoami."""

from __future__ import annotations

import json

import runspec as rs

from runspec_windows import _platform
from runspec_windows.graph import GraphError
from runspec_windows.graph._emit import run_graph
from runspec_windows.graph.auth import device_login
from runspec_windows.graph.client import graph_get


def main_graph_login() -> None:
    rs.parse("graph-login")
    try:
        result = device_login()
        claims = result.get("id_token_claims", {})
        print(
            json.dumps(
                {
                    "signed_in": True,
                    "user": claims.get("preferred_username") or claims.get("name"),
                    "scopes": result.get("scope"),
                }
            )
        )
    except GraphError as e:
        _platform.fail(str(e), kind=type(e).__name__)
    except Exception as e:
        _platform.fail(str(e))


def main_graph_whoami() -> None:
    rs.parse("graph-whoami")
    run_graph(lambda: _whoami(graph_get("/me", params={"$select": "displayName,userPrincipalName,mail,jobTitle,id"})))


def _whoami(me: dict) -> dict:
    return {
        "display_name": me.get("displayName"),
        "user_principal_name": me.get("userPrincipalName"),
        "mail": me.get("mail"),
        "job_title": me.get("jobTitle"),
        "id": me.get("id"),
    }
