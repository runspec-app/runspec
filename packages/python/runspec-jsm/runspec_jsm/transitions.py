"""transitions.py — list and apply workflow transitions (core Jira API)."""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs

from runspec_jsm._cli import opt
from runspec_jsm.client import JsmConfig, JsmError, config_from_env, jsm_request


def list_transitions(config: JsmConfig, issue_key: str) -> list[dict[str, Any]]:
    """Return the transitions currently available on an issue."""
    body = jsm_request("GET", f"/rest/api/2/issue/{issue_key}/transitions", config=config)
    return list(body.get("transitions", [])) if isinstance(body, dict) else []


def resolve_transition_id(transitions_payload: Any, wanted: str) -> str:
    """Resolve a transition by id or case-insensitive name to its id.

    ``transitions_payload`` is the value from :func:`list_transitions` (or the
    raw ``{transitions: [...]}`` response). Raises :class:`JsmError` listing the
    available transitions when ``wanted`` matches none.
    """
    transitions = transitions_payload.get("transitions", []) if isinstance(transitions_payload, dict) else list(transitions_payload)
    for t in transitions:
        if str(t.get("id")) == wanted:
            return str(t["id"])
    for t in transitions:
        if str(t.get("name", "")).lower() == wanted.lower():
            return str(t["id"])
    available = ", ".join(f"{t.get('name')} (id {t.get('id')})" for t in transitions) or "none"
    raise JsmError(f"Transition {wanted!r} not available. Available: {available}")


def transition_issue(config: JsmConfig, issue_key: str, transition: str, comment: str | None = None) -> str:
    """Apply a transition (by id or name) to an issue, optionally commenting.

    Returns the resolved transition id. The transition endpoint returns 204, so
    there is no response body.
    """
    tid = resolve_transition_id(list_transitions(config, issue_key), transition)
    payload: dict[str, Any] = {"transition": {"id": tid}}
    if comment:
        payload["update"] = {"comment": [{"add": {"body": comment}}]}
    jsm_request("POST", f"/rest/api/2/issue/{issue_key}/transitions", config=config, json_body=payload)
    return tid


def main_list_transitions() -> None:
    spec = rs.parse("jsm-list-transitions")
    try:
        config = config_from_env(env=opt(spec.env))
        transitions = list_transitions(config, issue_key=str(spec.issue.value))
        print(json.dumps([{"id": t.get("id"), "name": t.get("name"), "to": (t.get("to") or {}).get("name")} for t in transitions]))
    except JsmError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main_transition_issue() -> None:
    spec = rs.parse("jsm-transition-issue")
    try:
        config = config_from_env(env=opt(spec.env))
        tid = transition_issue(config, issue_key=str(spec.issue.value), transition=str(spec.transition.value), comment=opt(spec.comment))
        print(json.dumps({"issue": str(spec.issue.value), "transitionId": tid, "transitioned": True}))
    except JsmError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
