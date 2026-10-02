"""
savedruns.py — read/validate ``runspec_saved_runs.toml``, the saved-run registry.

A **saved run** is a deterministic, reusable invocation: a predefined runnable +
target(s) + fixed args, plus optional *open parameters* the operator (or agent)
fills at run time. It is the declarative sibling of a runbook — a runbook is
agent instructions the model follows; a saved run involves no model at all. The
Forms tab lists them, the agent can run them (gated by the underlying runnable's
autonomy), and a schedule can fire them (``action = "saved_run"``).

A saved-run entry:

  [[saved_run]]
  id             = "restart-web"      # unique identity (Config Sync merges by id)
  title          = "Restart web tier"
  description    = "Rolling restart"  # optional
  runnable       = "service-restart"
  command        = []                 # optional subcommand path
  group          = "Production"       # exactly one of host/group/hosts/selected_hosts/profile
  credential_ids = []                 # console credential ids — never secrets

  [saved_run.args]                    # fixed args, baked in
  service = "nginx"

  [saved_run.params]                  # open params — the runspec arg grammar
  reason = { required = true }

Targeting mirrors a runnable-action schedule exactly: ``host`` (one host),
``group`` (fan out across a host group), ``hosts`` (candidate list — the run
lands on the first connected one), ``selected_hosts`` (explicit fan-out list).
Saved runs add a fifth mode, ``profile`` (fan out across every host tagged with
that host profile — resolved at run time, so the preset follows the profile's
membership as hosts are added or removed).

``params`` is a runspec args table (the same grammar as a runnable's args and a
runbook's ``[runbook.args]``) parsed/validated via :mod:`runbookargs`, so a
param behaves exactly like a runnable arg in the run form. A param may not
shadow a fixed arg — the merge is deterministic, values fill *missing* args only.

The file **rides Config Sync** (in ``KNOWN_FILES``), merged by ``id`` like
runbooks — so fixed args must never carry secrets; those ride
``credential_ids`` (keychain-backed, injected at run time by the bridge).
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

import tomli_w

from . import config as config_mod
from . import runbookargs

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

# Serialises read-modify-write of the registry (re-entrant, mirrors memory.py).
_LOCK = threading.RLock()


@dataclass(frozen=True)
class SavedRun:
    """A parsed ``[[saved_run]]`` entry."""

    id: str
    title: str = ""
    description: str = ""
    runnable: str = ""
    command: list[str] = field(default_factory=list)
    # Exactly one of host / group / hosts / selected_hosts / profile is set (the
    # runnable-action ScheduleJob modes, plus a host profile).
    host: str = ""
    group: str = ""
    hosts: list[str] = field(default_factory=list)
    selected_hosts: list[str] = field(default_factory=list)
    profile: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    # Raw runspec args table for the open parameters (may be empty).
    params: dict[str, Any] = field(default_factory=dict)
    credential_ids: list[str] = field(default_factory=list)

    def target_summary(self) -> str:
        """One-line human description of where this saved run lands."""
        if self.host:
            return self.host
        if self.group:
            return f"group {self.group}"
        if self.hosts:
            return "first connected of " + ", ".join(self.hosts)
        if self.profile:
            return f"profile {self.profile}"
        return "all of " + ", ".join(self.selected_hosts)


def _norm(name: str) -> str:
    return str(name).replace("-", "_")


def parse_saved_run(data: dict[str, Any]) -> SavedRun:
    """Validate and parse one ``[[saved_run]]`` table.

    Raises ``ValueError`` with a human-first message on bad config: missing
    id/runnable, not-exactly-one targeting mode, a malformed ``params`` table,
    or a param that shadows a fixed arg.
    """
    sid = str(data.get("id") or "").strip()
    if not sid:
        raise ValueError("saved run is missing an 'id'")
    runnable = str(data.get("runnable") or "").strip()
    if not runnable:
        raise ValueError(f"saved run '{sid}' is missing 'runnable'")

    host = str(data.get("host") or "").strip()
    group = str(data.get("group") or "").strip()
    hosts = [str(h).strip() for h in (data.get("hosts") or []) if str(h).strip()]
    selected_hosts = [
        str(h).strip() for h in (data.get("selected_hosts") or []) if str(h).strip()
    ]
    profile = str(data.get("profile") or "").strip()
    modes = sum(
        (bool(host), bool(group), bool(hosts), bool(selected_hosts), bool(profile))
    )
    if modes != 1:
        raise ValueError(
            f"saved run '{sid}' needs exactly one of 'host' (a single host), "
            "'group' (a host group to fan out across), 'hosts' (a candidate list "
            "to run on the first connected one), 'selected_hosts' (an explicit "
            "list to run on all of them), or 'profile' (a host profile to fan out "
            "across)"
        )

    command_raw = data.get("command") or []
    if isinstance(command_raw, str):
        command = [command_raw.strip()] if command_raw.strip() else []
    else:
        command = [str(c).strip() for c in command_raw if str(c).strip()]

    args = dict(data.get("args") or {})
    params = dict(data.get("params") or {})
    if params:
        try:
            runbookargs.parse_runbook_args(params)
        except Exception as exc:
            raise ValueError(
                f"saved run '{sid}' has a bad params table: {exc}"
            ) from exc
        fixed = {_norm(k) for k in args}
        clash = sorted(p for p in params if _norm(p) in fixed)
        if clash:
            raise ValueError(
                f"saved run '{sid}' declares param(s) {', '.join(clash)} that "
                "shadow a fixed arg — a param may only fill an arg the saved run "
                "leaves open"
            )

    return SavedRun(
        id=sid,
        title=str(data.get("title") or "").strip(),
        description=str(data.get("description") or "").strip(),
        runnable=runnable,
        command=command,
        host=host,
        group=group,
        hosts=hosts,
        selected_hosts=selected_hosts,
        profile=profile,
        args=args,
        params=params,
        credential_ids=[
            str(c).strip() for c in (data.get("credential_ids") or []) if str(c).strip()
        ],
    )


def merged_args(entry: SavedRun, values: dict[str, Any] | None) -> dict[str, Any]:
    """The effective args for a run: fixed ``args`` + validated ``values``.

    ``values`` are checked against the entry's ``params`` table (required /
    choice / range / pattern / type, via the core runspec validator); a value
    for an undeclared param is rejected so the merge stays deterministic.
    Raises ``ValueError`` listing every problem.
    """
    values = dict(values or {})
    declared = {_norm(p) for p in entry.params}
    unknown = sorted(v for v in values if _norm(v) not in declared)
    if unknown:
        raise ValueError(
            f"saved run '{entry.id}' has no param(s) named: {', '.join(unknown)}"
        )
    errors = runbookargs.validate_runbook_values(entry.params, values)
    if errors:
        raise ValueError("; ".join(errors))
    return {**entry.args, **values}


# ── registry I/O ──────────────────────────────────────────────────────────────


def load_saved_runs() -> list[dict[str, Any]]:
    """All raw ``[[saved_run]]`` entries ([] when the file is absent/unreadable)."""
    path = config_mod.saved_runs_path()
    with _LOCK:
        if not path.exists():
            return []
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            return []
        rows = data.get("saved_run")
        return [dict(r) for r in rows] if isinstance(rows, list) else []


def save_saved_runs(rows: list[dict[str, Any]]) -> None:
    """Write the whole registry (atomic-ish: temp file + replace)."""
    path = config_mod.saved_runs_path()
    with _LOCK:
        text = tomli_w.dumps({"saved_run": rows})
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def load_saved_run(sid: str) -> dict[str, Any] | None:
    """One raw entry by id, or None."""
    for row in load_saved_runs():
        if str(row.get("id") or "") == sid:
            return row
    return None
