"""webhooks.py — the pure half of following a venv's webhooks (console 0.438.0).

A venv's runspec-scheduler daemon (0.11.0+) receives webhooks, keeps them in a
spool and streams every change with ``runspec-scheduler hooks follow``: first a
``hello`` line (what's waiting now, the mutes, the hooks), then one JSON line per
event. The console runs that over SSH (``sources/webhook.py`` owns the
connection, ``bridge_webhooks`` the wiring); this module folds the lines into
state the UI shows and decides which deliveries fire console triggers.

Which hooks a console follows is a host-entry setting, ``hooks = ["jira-deploy"]``
(or ``["*"]``), next to ``room``; Config Sync carries it.

Nothing here does I/O, so it is unit-tested directly.
"""

from __future__ import annotations

import re
import secrets as _secrets
import time
import tomllib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: Deliveries younger than this fire console triggers; older ones that arrive on
#: a (re)connect only show as "arrived while you were away".
DEFAULT_REPLAY = "15m"

#: How many settled deliveries each followed venv keeps for the UI.
RECENT_MAX = 50

#: States a person still acts on (or that are on their way to running).
OPEN_STATES = frozenset({"queued", "pending", "approved", "waiting", "running"})

#: States that are the end of a delivery.
SETTLED_STATES = frozenset(
    {
        "ran",
        "denied",
        "expired",
        "refused",
        "skipped",
        "event",
        "no-match",
        "duplicate",
        "error",
    }
)

_HOOK_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$|^\*$")
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]?)\s*$")


class WebhookError(ValueError):
    """Something the UI sent can't be done (the message is shown as is)."""


def parse_duration(text: str | int | None, default: int = 900) -> int:
    """``90``, ``90s``, ``15m``, ``6h``, ``1d`` → seconds; blank → ``default``."""
    if text is None or text == "":
        return default
    if isinstance(text, int) and not isinstance(text, bool):
        return max(0, text)
    m = _DURATION_RE.match(str(text))
    if not m:
        raise WebhookError(f"{text!r} isn't a duration: use seconds, or 15m, 6h or 1d.")
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def followed_hooks(entry: dict[str, Any]) -> list[str]:
    """The ``hooks`` list on a host entry (names, or ``*``). Anything that
    isn't a valid hook name is dropped."""
    raw = entry.get("hooks")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name and _HOOK_RE.match(name) and name not in out:
            out.append(name)
    return out


def validate_follow(hooks: Any) -> list[str]:
    """What the UI wants to follow on a host: a list of hook names or ``*``."""
    if not isinstance(hooks, list):
        raise WebhookError("hooks must be a list of hook names (or *)")
    out: list[str] = []
    for item in hooks:
        name = str(item or "").strip()
        if not _HOOK_RE.match(name):
            raise WebhookError(
                f"{name!r} isn't a hook name: lowercase letters, digits, '.', '_' or '-'."
            )
        if name not in out:
            out.append(name)
    return ["*"] if "*" in out else out


def follows(hooks: list[str], name: str) -> bool:
    return "*" in hooks or name in hooks


def _ts(iso: Any) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except ValueError:
        return None


@dataclass
class VenvHooks:
    """What one console knows about one followed venv's webhooks."""

    host: str
    venv: str
    hooks: list[str] = field(default_factory=list)  # what this console follows there
    connected: bool = False
    error: str = ""
    paused: str = ""  # why this console isn't counted as watching ("screen locked")
    cursor: int | None = None  # the last event id seen (None = never connected)
    who: str = ""  # who the host counts this console as
    settings: dict[str, Any] | None = None
    hook_list: list[dict[str, Any]] = field(default_factory=list)
    mutes: list[dict[str, Any]] = field(default_factory=list)
    open: dict[int, dict[str, Any]] = field(default_factory=dict)
    recent: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=RECENT_MAX)
    )
    dismissed: set[int] = field(default_factory=set)
    away: int = 0  # deliveries that arrived outside the replay window this connect

    @property
    def key(self) -> str:
        return f"{self.host}/{self.venv}"

    def wanted(self, delivery: dict[str, Any]) -> bool:
        return follows(self.hooks, str(delivery.get("hook") or ""))

    def hello(self, msg: dict[str, Any]) -> None:
        """A fresh connection's first line: replace what's waiting, mutes and hooks."""
        self.connected = True
        self.error = ""
        self.away = 0
        self.who = str(msg.get("who") or "")
        self.settings = (
            msg.get("settings") if isinstance(msg.get("settings"), dict) else None
        )
        self.hook_list = [h for h in msg.get("hooks") or [] if isinstance(h, dict)]
        self.mutes = [m for m in msg.get("mutes") or [] if isinstance(m, dict)]
        self.open = {}
        for d in msg.get("pending") or []:
            if isinstance(d, dict) and self.wanted(d) and isinstance(d.get("id"), int):
                self.open[d["id"]] = dict(d)
        if self.cursor is None or isinstance(msg.get("event"), int):
            self.cursor = int(msg.get("event") or 0)

    def apply(
        self, event: dict[str, Any], *, now: float | None = None, replay_s: int = 900
    ) -> dict[str, Any] | None:
        """Fold one event in. Returns the delivery when it is a new arrival that
        should fire console triggers (fresh enough, a hook this console follows),
        else ``None``."""
        eid = event.get("event")
        if isinstance(eid, int):
            self.cursor = max(self.cursor or 0, eid)
        kind = str(event.get("kind") or "")
        if kind == "ping":
            return None
        if kind == "mute":
            who, hook = event.get("who"), event.get("hook")
            self.mutes = [
                m
                for m in self.mutes
                if not (m.get("who") == who and m.get("hook") == hook)
            ]
            self.mutes.append(
                {k: event.get(k) for k in ("who", "hook", "until", "reason")}
            )
            return None
        if kind == "unmute":
            who, hook = event.get("who"), event.get("hook")
            every = bool(event.get("all"))
            self.mutes = [
                m
                for m in self.mutes
                if not (m.get("who") == who and (every or m.get("hook") == hook))
            ]
            return None
        did = event.get("id")
        if not isinstance(did, int) or not event.get("hook"):
            return None
        if not self.wanted(event):
            return None
        delivery = {k: v for k, v in event.items() if k not in ("event", "kind", "ts")}
        state = str(delivery.get("state") or "")
        previous = self.open.get(did)
        if previous is not None and "body" in previous and "body" not in delivery:
            delivery["body"] = previous["body"]
        if state in OPEN_STATES:
            self.open[did] = delivery
        else:
            self.open.pop(did, None)
            if state in SETTLED_STATES:
                self.recent = deque(
                    [r for r in self.recent if r.get("id") != did], maxlen=RECENT_MAX
                )
                self.recent.appendleft(delivery)
        if kind != "delivery":
            return None
        # The first planned look at a delivery: fire triggers when it's fresh.
        received = _ts(delivery.get("received"))
        clock = time.time() if now is None else now
        if received is not None and clock - received > replay_s:
            self.away += 1
            return None
        return delivery

    def mutes_now(self, now: float | None = None) -> list[dict[str, Any]]:
        clock = time.time() if now is None else now
        return [m for m in self.mutes if (_ts(m.get("until")) or 0) > clock]

    def snapshot(self) -> dict[str, Any]:
        """What the UI reads (``webhooks_status``)."""
        pending = sorted(self.open.values(), key=lambda d: int(d.get("id") or 0))
        return {
            "key": self.key,
            "host": self.host,
            "venv": self.venv,
            "follows": list(self.hooks),
            "connected": self.connected,
            "error": self.error,
            "paused": self.paused,
            "who": self.who,
            "settings": self.settings,
            "hooks": self.hook_list,
            "mutes": self.mutes_now(),
            "open": [d for d in pending if int(d.get("id") or 0) not in self.dismissed],
            "dismissed": len(
                [d for d in pending if int(d.get("id") or 0) in self.dismissed]
            ),
            "recent": list(self.recent),
            "away": self.away,
        }


def flatten(
    value: Any, prefix: str = "", out: dict[str, Any] | None = None, depth: int = 0
) -> dict[str, Any]:
    """A delivery's ``fields`` as one level of ``a.b`` keys (trigger ``match``
    and ``{{ field }}`` read one level only). Lists become ``a.0``…"""
    out = {} if out is None else out
    if depth > 6:
        out[prefix] = value
        return out
    if isinstance(value, dict):
        for k, v in value.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out, depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value[:50]):
            flatten(v, f"{prefix}.{i}" if prefix else str(i), out, depth + 1)
    elif prefix:
        out[prefix] = value
    return out


def trigger_payload(host: str, venv: str, delivery: dict[str, Any]) -> dict[str, Any]:
    """The ``webhook`` trigger event payload for a delivery: the hook's fields
    flattened to the top, then what the console adds (which never overwrite a
    field of the same name — the field wins, the console's key is still under
    ``webhook``)."""
    fields = delivery.get("fields") if isinstance(delivery.get("fields"), dict) else {}
    payload: dict[str, Any] = {
        k: v for k, v in flatten(fields).items() if not isinstance(v, (dict, list))
    }
    meta = {
        "hook": delivery.get("hook"),
        "id": delivery.get("id"),
        "received": delivery.get("received"),
        "body": delivery.get("body") or "",
        "state": delivery.get("state"),
        "runnable": delivery.get("runnable") or "",
        "rule": delivery.get("rule") or "",
        "autonomy": delivery.get("autonomy") or "",
        "host": host,
        "venv": venv,
    }
    for k, v in meta.items():
        payload.setdefault(k, v)
    payload["webhook"] = meta
    return payload


# ── runspec-scheduler argv (run as the venv owner by the bridge) ─────────────


def follow_argv(cursor: int | None, replay: str = DEFAULT_REPLAY) -> list[str]:
    """``hooks follow`` from where this console left off, else the replay window."""
    if cursor is None:
        return ["hooks", "follow", "--replay", replay or DEFAULT_REPLAY]
    return ["hooks", "follow", "--since", str(int(cursor))]


def decide_argv(verdict: str, delivery_id: Any, note: str = "") -> list[str]:
    if verdict not in ("approve", "deny"):
        raise WebhookError("verdict must be approve or deny")
    try:
        did = int(delivery_id)
    except (TypeError, ValueError):
        raise WebhookError("no delivery id") from None
    argv = ["hooks", verdict, "--id", str(did)]
    if note.strip():
        argv += ["--note", note.strip()[:500]]
    return argv


def mute_argv(hook: str, for_: str | int, note: str = "") -> list[str]:
    name = str(hook or "*").strip() or "*"
    if not _HOOK_RE.match(name):
        raise WebhookError(f"{name!r} isn't a hook name")
    seconds = parse_duration(for_, 3600)
    if seconds < 60:
        raise WebhookError("Mute for at least a minute.")
    argv = ["hooks", "mute", "--hook", name, "--for", f"{seconds}s"]
    if note.strip():
        argv += ["--reason", note.strip()[:200]]
    return argv


def unmute_argv(hook: str) -> list[str]:
    name = str(hook or "*").strip() or "*"
    if not _HOOK_RE.match(name):
        raise WebhookError(f"{name!r} isn't a hook name")
    return ["hooks", "unmute", "--hook", name]


#: ``[hooks]`` settings the editor may change (``hooks settings --set``).
SETTING_KEYS = (
    "enabled", "port", "bind", "base_path", "cert", "key", "max_body", "keep",
    "rate_per_minute", "trusted_proxies", "approve_within", "expect_claim_within",
    "max_runs",
)  # fmt: skip
SMTP_KEYS = ("relay", "port", "from", "to", "starttls")


def _setting_value(value: Any) -> str:
    """One ``key=value`` value as ``hooks settings`` parses it (TOML literal)."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_setting_value(str(v)) for v in value) + "]"
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def settings_argv(
    hooks: dict[str, Any] | None, smtp: dict[str, Any] | None
) -> list[str]:
    argv = ["hooks", "settings"]
    for key, value in (hooks or {}).items():
        if key not in SETTING_KEYS:
            raise WebhookError(f"unknown [hooks] setting {key!r}")
        argv += ["--set", f"{key}={_setting_value(value)}"]
    for key, value in (smtp or {}).items():
        if key not in SMTP_KEYS:
            raise WebhookError(f"unknown [smtp] setting {key!r}")
        argv += ["--smtp", f"{key}={_setting_value(value)}"]
    return argv


def hook_table(payload: dict[str, Any]) -> dict[str, Any]:
    """The hook the editor sent: ``hook`` (a table) or ``text`` (TOML, a bare
    table or one ``[[hook]]``)."""
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise WebhookError(f"That isn't valid TOML: {exc}") from None
        hooks = data.get("hook")
        if isinstance(hooks, list):
            if len(hooks) != 1 or not isinstance(hooks[0], dict):
                raise WebhookError("Give one [[hook]] at a time.")
            return dict(hooks[0])
        return data
    hook = payload.get("hook")
    return dict(hook) if isinstance(hook, dict) else {}


def save_argv() -> list[str]:
    """``hooks save``. A new token or password is made here (:func:`new_secrets`)
    and sent in the hook JSON on stdin, which the host hashes before saving, so
    no secret is ever printed on the host or lands in its run log."""
    return ["hooks", "save"]


def new_secrets(
    hook: dict[str, Any], *, token: bool, password: bool
) -> tuple[dict[str, Any], dict[str, str]]:
    """``hook`` with a fresh ``token`` and/or basic-auth ``password`` in it, and
    those secrets (shown once in the editor; the host keeps only hashes)."""
    out = dict(hook)
    made: dict[str, str] = {}
    if token:
        made["token"] = out["token"] = _secrets.token_urlsafe(32)
    if password:
        if not str(out.get("username") or "").strip():
            raise WebhookError("a basic-auth password needs a username")
        made["password"] = out["password"] = _secrets.token_urlsafe(24)
    return out, made


def remove_argv(name: str) -> list[str]:
    name = str(name or "").strip()
    if not _HOOK_RE.match(name) or name == "*":
        raise WebhookError(f"{name!r} isn't a hook name")
    return ["hooks", "remove", "--name", name]
