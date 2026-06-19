"""comments.py — add public or internal comments to a request (servicedeskapi)."""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs

from runspec_jsm._cli import flag, opt
from runspec_jsm.client import JsmConfig, JsmError, config_from_env, jsm_request


def add_comment(config: JsmConfig, issue_key: str, body: str, public: bool = True) -> dict[str, Any]:
    """Add a comment to a request. ``public=False`` makes it an internal note."""
    payload = {"body": body, "public": public}
    return jsm_request("POST", f"/rest/servicedeskapi/request/{issue_key}/comment", config=config, json_body=payload)


def main_add_comment() -> None:
    spec = rs.parse("jsm-add-comment")
    try:
        config = config_from_env(env=opt(spec.env))
        public = not flag(spec.internal)
        result = add_comment(config, issue_key=str(spec.issue.value), body=str(spec.body.value), public=public)
        print(json.dumps({"issue": str(spec.issue.value), "commentId": result.get("id"), "public": public}))
    except JsmError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
