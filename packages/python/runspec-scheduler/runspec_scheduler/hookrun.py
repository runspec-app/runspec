"""hookrun.py — what a webhook delivery does on the host.

The listener only checks a request and spools it (:mod:`.listener`). The
daemon's hook worker then, for each queued delivery:

1. reads the body (JSON / XML / form / text) and splits it when the hook says
   so (``split = "alerts"`` — each alert its own delivery);
2. picks out the fields (``fields = { … }``, plus built-ins: ``hook``,
   ``delivery``, ``received``, ``source``, ``content_type``, ``body``,
   ``headers``), optionally via a transform runnable that prints JSON;
3. checks the rules in order — the first that matches decides: run a runnable
   with templated args, skip, or just tell the consoles (an ``event``);
4. drops a repeat (same ``dedup`` key within ``dedup_window``): a waiting
   approval counts the copies instead;
5. works out the run's autonomy like the console's gate does — the runnable's
   (or its command's) level, raised by any arg that declares a stricter one.
   Setting up the hook is the approval, as for a host schedule: anything but
   ``manual`` runs now; ``manual`` is refused. A hook or rule with
   ``require_approval = true`` waits for someone to approve each run on a
   console (``hooks approve``) instead.

Runs go through ``runspec exec`` (:mod:`.hookexec`) as the venv owner, label
``hook:<name>``, trigger ``webhook`` (``webhook:approved`` after an approval),
with the venv's ``.runspec_scheduler_env`` and the payload's values as
``RUNSPEC_<R>_ARG_<NAME>`` environment variables, never argv. The denylist
applies (exec's ``parse()`` checks it).

A waiting approval nobody picks up within ``expect_claim_within`` is mailed
through ``[smtp]`` — at once when no console follows the venv or everyone who
does has muted the hook — and expires after ``approve_within`` (an expired one
never runs).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import hooktemplate as tpl
from . import spool as sp
from .hookconfig import DEFAULT_RESPOND_MAX, HOOK_LABEL_PREFIX, TRIGGER_WEBHOOK, TRIGGER_WEBHOOK_APPROVED, Hook, HookConfig, HooksSettings, Rule, SmtpSettings
from .records import audit

logger = logging.getLogger("runspec_scheduler.hooks")

#: Headers kept with a delivery (and shown to rules as ``headers.<name>``) —
#: never the ones that carry credentials.
_SECRET_HEADERS = {"authorization", "cookie", "proxy-authorization", "x-runspec-token"}


#: Variables the daemon's own process has that a run mustn't inherit.
_DROP_ENV = (
    "RUNSPEC_RUN_ID", "RUNSPEC_RUN_LOG", "RUNSPEC_EXEC_UID", "RUNSPEC_LABEL", "RUNSPEC_TRIGGER", "RUNSPEC_CONFIG",
    "RUNSPEC_ENV_FILE", "RUNSPEC_RUNBOOK", "RUNSPEC_SCHEDULER_CONFIG", "RUNSPEC_AGENT",
)  # fmt: skip

TRANSFORM_TIMEOUT = 30


def who_am_i() -> str:
    """The person behind a verb: ``SUDO_USER`` (the console runs verbs as the
    venv owner with sudo), else this account."""
    sudo = (os.environ.get("SUDO_USER") or "").strip()
    if sudo and sudo != "root":
        return sudo
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:  # noqa: BLE001 — Windows / unknown uid
        return os.environ.get("USER", "") or "unknown"


def keep_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items() if k.lower() not in _SECRET_HEADERS and not k.lower().startswith("x-runspec-")}


# ── planning a delivery ───────────────────────────────────────────────────────


@dataclass
class Plan:
    """What one delivery (item) does."""

    state: str  # spool state it goes to next (pending / waiting / skipped / event / no-match / refused / error)
    fields: dict[str, Any] = field(default_factory=dict)
    rule: str = ""
    runnable: str = ""
    command: list[str] = field(default_factory=list)
    args: dict[str, Any] = field(default_factory=dict)
    autonomy: str = ""
    reason: str = ""
    dedup_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "fields": self.fields,
            "rule": self.rule,
            "runnable": self.runnable,
            "command": self.command,
            "args": self.args,
            "autonomy": self.autonomy,
            "reason": self.reason,
            "dedup_key": self.dedup_key,
        }


def items_of(hook: Hook, data: Any) -> list[Any]:
    """The items a body holds: the whole body, or each element at ``split``."""
    if not hook.split:
        return [data]
    value = tpl.get_path(data, hook.split, None)
    if isinstance(value, list):
        return value or []
    return [value] if value is not None else []


def builtin_fields(hook: Hook, row: dict[str, Any], body: bytes) -> dict[str, Any]:
    return {
        "hook": hook.name,
        "delivery": row.get("id"),
        "received": sp._iso(row.get("received_ts")),
        "source": row.get("source") or "",
        "content_type": row.get("content_type") or "",
        "body": body.decode("utf-8", errors="replace"),
        "headers": dict(row.get("headers") or {}),
    }


def plan_item(hook: Hook, fields: dict[str, Any], *, resolve: Any = None) -> Plan:
    """Rules → what this item does (autonomy worked out by ``resolve``:
    ``(runnable, command, args) -> (autonomy, problem)``; ``None`` in tests that
    don't care = autonomous)."""
    plan = Plan(state=sp.NO_MATCH, fields=dict(fields))
    if hook.dedup:
        try:
            plan.dedup_key = tpl._as_text(tpl.render(hook.dedup, fields))
        except tpl.TemplateError as exc:
            plan.state, plan.reason = sp.ERROR, f"dedup: {exc}"
            return plan
    for rule in hook.rules:
        captured = tpl.match_rule(rule.match, fields)
        if captured is None:
            continue
        fields = {**fields, **captured}
        plan.fields = fields
        plan.rule = rule.name
        return _rule_plan(hook, rule, plan, fields, resolve)
    if not hook.rules:
        plan.state, plan.reason = sp.EVENT, "the hook has no rules: consoles only"
    else:
        plan.reason = "no rule matched"
    return plan


def _rule_plan(hook: Hook, rule: Rule, plan: Plan, fields: dict[str, Any], resolve: Any) -> Plan:
    if rule.skip:
        plan.state, plan.reason = sp.SKIPPED, f"rule {rule.name!r} skips it"
        return plan
    if not rule.run:
        plan.state, plan.reason = sp.EVENT, f"rule {rule.name!r} runs nothing: consoles only"
        return plan
    plan.runnable, plan.command = rule.run, list(rule.command)
    try:
        args = tpl.render(rule.args, fields)
    except tpl.TemplateError as exc:
        plan.state, plan.reason = sp.ERROR, f"rule {rule.name!r}: {exc}"
        return plan
    plan.args = {k: v for k, v in args.items() if v is not None and v != ""}
    autonomy, problem = ("autonomous", "") if resolve is None else resolve(rule.run, tuple(rule.command), plan.args)
    if problem:
        plan.state, plan.reason = sp.REFUSED, problem
        return plan
    plan.autonomy = autonomy
    if autonomy == "manual":
        plan.state, plan.reason = sp.REFUSED, f"{rule.run} is manual: only a person runs it, never a webhook"
    elif hook.require_approval or rule.require_approval:
        # Opt-in per hook or rule: a person approves each run. Otherwise
        # setting up the hook was the approval (like a host schedule), so
        # confirm/supervised runnables run as autonomous ones do.
        plan.state, plan.reason = sp.PENDING, "the hook asks for an approval"
    else:
        plan.state = sp.WAITING
    return plan


def fields_for(hook: Hook, row: dict[str, Any], body: bytes, *, transform: Any = None) -> list[dict[str, Any]]:
    """The fields of each item in a delivery's body (one dict per item).
    Raises :class:`hooktemplate.TemplateError` when the body can't be read."""
    data = tpl.parse_body(body, row.get("content_type") or "")
    base = builtin_fields(hook, row, body)
    if transform is not None:
        extra = transform(hook, body, row)
        if extra:
            data = extra if data is None else data
            base = {**base, **{k: v for k, v in extra.items() if isinstance(k, str)}}
    out = []
    for item in items_of(hook, data):
        values = dict(base)
        if hook.split:
            values["item"] = item
        values.update(tpl.extract_fields(hook.fields, item, data))
        out.append(values)
    return out


def dry_run(hook: Hook, body: bytes, content_type: str, *, resolve: Any = None, transform: Any = None, source: str = "test") -> list[dict[str, Any]]:
    """What ``hooks test`` prints: per item, its fields, the rule and what
    would run (nothing is run or stored)."""
    row = {"id": 0, "received_ts": time.time(), "source": source, "content_type": content_type, "headers": {}}
    try:
        items = fields_for(hook, row, body, transform=transform)
    except tpl.TemplateError as exc:
        return [{"state": sp.ERROR, "reason": str(exc), "fields": {}}]
    if not items:
        return [{"state": sp.NO_MATCH, "reason": f"split {hook.split!r} found no items", "fields": {}}]
    out = []
    for values in items:
        plan = plan_item(hook, values, resolve=resolve).to_dict()
        plan["fields"] = {k: v for k, v in plan["fields"].items() if k not in ("body", "headers", "item")}
        out.append(plan)
    return out


# ── autonomy, like the console's gate ─────────────────────────────────────────


class Resolver:
    """``(runnable, command, args) -> (autonomy, problem)`` from the runnables
    installed in this venv (discovery cached for a minute)."""

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._at = 0.0
        self._records: dict[str, dict[str, Any]] = {}
        self._defaults: dict[str, str] = {}

    def _discover(self) -> None:
        if time.monotonic() - self._at < self.ttl and self._records:
            return
        from .discovery import discover

        self._records = discover()
        self._defaults = {}
        self._at = time.monotonic()

    def _config_default(self, source: str) -> str:
        if source not in self._defaults:
            try:
                from runspec.loader import load_raw

                self._defaults[source] = str(load_raw(Path(source))["config"].get("autonomy_default") or "confirm")
            except Exception:  # noqa: BLE001 — unreadable spec = the safe default
                self._defaults[source] = "confirm"
        return self._defaults[source]

    def __call__(self, runnable: str, command: tuple[str, ...], args: dict[str, Any]) -> tuple[str, str]:
        self._discover()
        record = self._records.get(runnable)
        if record is None:
            return "", f"{runnable} isn't installed in this venv"
        spec = record.get("spec") or {}
        from .discovery import check_invocation

        problem = check_invocation(spec, command, {k: v for k, v in args.items()})
        if problem and "password" not in problem:
            return "", problem
        return autonomy_for(spec, self._config_default(str(record.get("source") or "")), command, args), ""


def autonomy_for(spec: dict[str, Any], config_default: str, command: tuple[str, ...] | list[str], args: dict[str, Any]) -> str:
    """The level a call gates at: the command's own (inferred: its declared
    level, else the package default), raised by any provided arg declaring a
    stricter one."""
    from runspec.inference import effective_autonomy, infer_script

    from .invocation import effective_args

    inferred = infer_script(dict(spec), config_default)
    node = inferred
    for verb in command:
        node = (node.get("commands") or {}).get(verb) or {}
    level = str(node.get("autonomy") or config_default)
    eff, _ = effective_args(spec, command)
    specs = {declared: aspec for declared, aspec in eff.values()}
    provided = {}
    for key, value in args.items():
        declared = eff.get(str(key).replace("_", "-"), (key, {}))[0]
        provided[declared] = value
    return effective_autonomy(level, provided, specs)


def run_env(runnable: str, spec: dict[str, Any] | None, command: list[str], args: dict[str, Any]) -> dict[str, str]:
    """The payload's values as ``RUNSPEC_<R>_ARG_<NAME>`` (lists newline
    separated, flags 1/0)."""
    from .invocation import effective_args, env_key

    eff, _ = effective_args(spec or {}, command)
    env: dict[str, str] = {}
    for key, value in args.items():
        declared = eff.get(str(key).replace("_", "-"), (str(key), {}))[0]
        if isinstance(value, bool):
            text = "1" if value else "0"
        elif isinstance(value, list):
            text = "\n".join(tpl._as_text(v) for v in value)
        else:
            text = tpl._as_text(value)
        env[env_key(runnable, declared)] = text
    return env


# ── email ─────────────────────────────────────────────────────────────────────


def send_approval_mail(smtp: SmtpSettings, to: tuple[str, ...], row: dict[str, Any], *, why: str, venv: str, config_path: Path) -> str:
    """Mail one waiting approval. Returns "" when sent, else why not."""
    if not smtp.configured or not to:
        return "no [smtp] relay/from, or nobody to mail"
    import smtplib
    from email.message import EmailMessage

    host = socket.gethostname()
    msg = EmailMessage()
    msg["Subject"] = f"[runspec] webhook {row['hook']} waiting for approval on {host} ({venv})"
    msg["From"] = smtp.sender
    msg["To"] = ", ".join(to)
    fields = {k: v for k, v in (row.get("fields") or {}).items() if k not in ("body", "headers", "item")}
    command = " ".join(row.get("command") or [])
    lines = [
        f"A webhook needs someone to approve it ({why}).",
        "",
        f"Host:      {host}",
        f"Venv:      {venv}",
        f"Hook:      {row['hook']}",
        f"Delivery:  {row['id']}",
        f"Received:  {sp._iso(row.get('received_ts'))}",
        f"Would run: {row.get('runnable')}{(' ' + command) if command else ''} ({row.get('autonomy')})",
        f"Args:      {json.dumps(row.get('args') or {}, default=str)}",
        f"Expires:   {sp._iso(row.get('expires_ts'))}",
        "",
        "Fields:",
        *(f"  {k}: {tpl._as_text(v)[:300]}" for k, v in fields.items()),
        "",
        "Approve or deny it on runspec-console, or on the host as the venv owner:",
        f"  {Path(sys.prefix) / 'bin' / 'runspec-scheduler'} hooks approve --id {row['id']} --config {config_path}",
        f"  {Path(sys.prefix) / 'bin' / 'runspec-scheduler'} hooks deny --id {row['id']} --config {config_path}",
    ]
    msg.set_content("\n".join(lines))
    try:
        with smtplib.SMTP(smtp.relay, smtp.port, timeout=smtp.timeout) as conn:
            if smtp.starttls:
                conn.starttls()
            conn.send_message(msg)
    except Exception as exc:  # noqa: BLE001 — reported in the delivery, never raised
        return f"{type(exc).__name__}: {exc}"
    return ""


def run_transform(hook: Hook, body: bytes, row: dict[str, Any]) -> dict[str, Any] | None:
    """Run the hook's transform runnable on the body (stdin) and read the JSON
    object it prints as extra fields. ``None`` when the hook has none."""
    if not hook.transform:
        return None
    from .runner import find_binary

    binary = find_binary(hook.transform)
    if binary is None:
        raise tpl.TemplateError(f"transform {hook.transform} isn't installed in this venv")
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    env.update({"RUNSPEC_HOOK": hook.name, "RUNSPEC_HOOK_CONTENT_TYPE": row.get("content_type") or ""})
    try:
        done = subprocess.run([str(binary)], input=body, capture_output=True, timeout=TRANSFORM_TIMEOUT, env=env, check=False)
    except subprocess.TimeoutExpired:
        raise tpl.TemplateError(f"transform {hook.transform} took longer than {TRANSFORM_TIMEOUT}s") from None
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or [f"exit {done.returncode}"]
        raise tpl.TemplateError(f"transform {hook.transform} failed: {tail[0]}")
    try:
        out = json.loads(done.stdout.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        raise tpl.TemplateError(f"transform {hook.transform} didn't print a JSON object") from None
    if not isinstance(out, dict):
        raise tpl.TemplateError(f"transform {hook.transform} printed JSON that isn't an object")
    return out


# ── the worker ────────────────────────────────────────────────────────────────


@dataclass
class _Run:
    delivery: int
    hook: str
    proc: subprocess.Popen[bytes]
    started: float
    output: str = ""  # where the run's stdout goes (respond = "output" hooks)
    output_max: int = 0


# ── sending the output back (respond = "output") ─────────────────────────────

#: Where a respond-hook run's standard output is written while it runs.
OUTPUT_DIR = "output"


def output_path(spool: sp.Spool, delivery: int) -> Path:
    return spool.path.parent / OUTPUT_DIR / f"{delivery}.out"


def open_output(spool: sp.Spool, delivery: int) -> tuple[int, str]:
    """A fresh owner-only file for a run's stdout: ``(fd, path)``."""
    folder = spool.path.parent / OUTPUT_DIR
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(folder, 0o700)
    path = output_path(spool, delivery)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o600), str(path)


def collect_output(spool: sp.Spool, delivery: int, limit: int) -> bool:
    """Move a finished run's stdout file into its spool row (cut to
    ``limit`` bytes). False when there was no file."""
    path = output_path(spool, delivery)
    try:
        with open(path, "rb") as fh:
            data = fh.read(limit + 1)
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("webhook delivery %d: couldn't read its output: %s", delivery, exc)
        return False
    truncated = len(data) > limit
    spool.update(delivery, kind="output", output=data[:limit].decode("utf-8", errors="replace"), output_truncated=int(truncated))
    with contextlib.suppress(OSError):
        path.unlink()
    return True


def output_value(text: str | None) -> Any:
    """The output as the sender gets it: parsed when the runnable printed
    JSON, else the text (one trailing newline dropped)."""
    if text is None:
        return None
    stripped = text.strip()
    if stripped[:1] in ("{", "[", '"') or stripped in ("true", "false", "null"):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return text[:-1] if text.endswith("\n") else text


#: States after which nothing more happens to an item without a person.
_ANSWERED = sp.SETTLED | {sp.PENDING}


def request_result(spool: sp.Spool, request_id: int, *, hook: Hook | None, status_url: str) -> dict[str, Any] | None:
    """What a sender gets back for one request: every item's state, exit code
    and (for a ``respond = "output"`` hook) output. ``status`` is ``done``
    (every item settled), ``pending`` (one waits for an approval, the rest
    settled) or ``running``. None when there's no such request."""
    items = spool.items(request_id)
    if not items or items[0]["id"] != request_id or (items[0].get("request") not in (None, request_id)):
        return None
    with_output = bool(hook and hook.respond == "output")
    shown = []
    for row in items:
        entry: dict[str, Any] = {
            "id": row["id"],
            "item": row["item"],
            "state": row["state"],
            "rule": row["rule"],
            "runnable": row["runnable"],
            "exit_code": row["exit_code"],
            "reason": row["reason"],
        }
        if with_output:
            entry["output"] = output_value(row.get("output")) if row["state"] == sp.RAN else None
            entry["output_truncated"] = bool(row.get("output_truncated"))
        shown.append(entry)
    states = {row["state"] for row in items}
    if not states <= _ANSWERED:
        status = "running"
    elif sp.PENDING in states:
        status = "pending"
    else:
        status = "done"
    out: dict[str, Any] = {"id": request_id, "hook": items[0]["hook"], "status": status}
    if status == "done":
        out["ok"] = all(e["state"] in (sp.RAN, sp.SKIPPED, sp.EVENT, sp.NO_MATCH, sp.DUPLICATE) and not e["exit_code"] for e in shown)
    if len(shown) == 1:
        out.update({k: v for k, v in shown[0].items() if k not in ("id", "item")})
    out["items"] = shown
    if status != "done":
        out["status_url"] = status_url
    return out


class HookWorker:
    """Works through the spool on its own thread: plans queued deliveries,
    starts runs (within ``max_runs`` and each hook's ``max_running``), reaps
    them, expires and mails waiting approvals, prunes old rows."""

    def __init__(self, spool: sp.Spool, config_path: Path, *, python: str | None = None, resolver: Resolver | None = None) -> None:
        self.spool = spool
        self.config_path = config_path
        self.python = python or sys.executable
        self.resolve = resolver or Resolver()
        self.cfg: HookConfig | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._runs: dict[int, _Run] = {}
        self._pruned = 0.0
        self._lock = threading.Lock()

    # control

    def configure(self, cfg: HookConfig) -> None:
        with self._lock:
            self.cfg = cfg
        self.notify()

    def notify(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="runspec-hooks-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def running(self) -> list[dict[str, Any]]:
        return [{"delivery": r.delivery, "hook": r.hook, "pid": r.proc.pid} for r in list(self._runs.values())]

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — one bad pass must not end the worker
                logger.exception("webhook worker pass failed")
        self.spool.close()

    def tick(self, now: float | None = None) -> None:
        cfg = self.cfg
        if cfg is None:
            return
        now = now or time.time()
        self.reap()
        # A split adds its items as new queued rows: go round until none are
        # left (bounded, so a flood can't hold the pass forever).
        for _ in range(20):
            rows = self.spool.queued()
            if not rows:
                break
            for row in rows:
                try:
                    self.process(row, cfg, now)
                except Exception as exc:  # noqa: BLE001 — a bad delivery must not block the ones after it
                    logger.exception("webhook delivery %s failed to plan", row["id"])
                    self.spool.update(row["id"], kind="delivery", state=sp.ERROR, reason=f"couldn't plan it: {exc}")
        self.start_runs(cfg)
        self.expire(now)
        self.mail(cfg, now)
        if now - self._pruned > 3600:
            self._pruned = now
            dropped = self.spool.prune(cfg.settings.keep, now)
            if dropped:
                logger.info("webhook spool: dropped %d deliveries older than %ds", dropped, cfg.settings.keep)

    # planning

    def _transform(self, hook: Hook, body: bytes, row: dict[str, Any]) -> dict[str, Any] | None:
        return run_transform(hook, body, row)

    def process(self, row: dict[str, Any], cfg: HookConfig, now: float) -> None:
        """Plan one queued delivery (splitting it into items first)."""
        hook = cfg.hooks.get(row["hook"])
        if hook is None or not hook.enabled:
            self.spool.update(row["id"], state=sp.REFUSED, reason=f"hook {row['hook']} is no longer set up")
            return
        body = self._body(row)
        if row.get("request") is None and hook.split:
            try:
                items = fields_for(hook, row, body, transform=self._transform)
            except tpl.TemplateError as exc:
                self._settle_error(row, hook, str(exc))
                return
            if not items:
                self.spool.update(row["id"], kind="delivery", with_body=True, state=sp.NO_MATCH, reason=f"split {hook.split!r} found no items", request=row["id"])
                return
            # The request row is item 0; each further item gets a row of its own.
            self.spool.update(row["id"], kind="split", request=row["id"], item=0)
            row = self.spool.get(row["id"]) or row
            for i in range(1, len(items)):
                self.spool.add_delivery(hook.name, source=row["source"], content_type=row["content_type"], headers=row["headers"], body=b"", request=row["id"], item=i, received_ts=row["received_ts"])
            self._plan(row, hook, cfg, items[0], now)
            return
        try:
            all_items = fields_for(hook, row, body, transform=self._transform)
        except tpl.TemplateError as exc:
            self._settle_error(row, hook, str(exc))
            return
        index = int(row.get("item") or 0)
        if index >= len(all_items):
            self.spool.update(row["id"], kind="delivery", state=sp.ERROR, reason="the item is gone from the body")
            return
        self._plan(row, hook, cfg, all_items[index], now)

    def _body(self, row: dict[str, Any]) -> bytes:
        body = row.get("body")
        if (body is None or body == b"") and row.get("request") and row["request"] != row["id"]:
            parent = self.spool.get(int(row["request"]))
            body = parent.get("body") if parent else b""
        if isinstance(body, memoryview):
            body = body.tobytes()
        return bytes(body or b"")

    def _settle_error(self, row: dict[str, Any], hook: Hook, reason: str) -> None:
        self.spool.update(row["id"], kind="delivery", with_body=True, state=sp.ERROR, reason=reason)
        audit("hook.received", "failed", target={"hook": hook.name, "delivery": row["id"]}, detail={"error": reason})

    def _plan(self, row: dict[str, Any], hook: Hook, cfg: HookConfig, values: dict[str, Any], now: float) -> None:
        plan = plan_item(hook, values, resolve=self.resolve)
        stored_fields = {k: v for k, v in plan.fields.items() if k not in ("body", "headers", "item")}
        changes: dict[str, Any] = {
            "fields": stored_fields,
            "rule": plan.rule,
            "runnable": plan.runnable,
            "command": plan.command,
            "args": plan.args,
            "autonomy": plan.autonomy,
            "reason": plan.reason,
            "dedup_key": plan.dedup_key,
        }
        if plan.dedup_key and plan.state in (sp.WAITING, sp.PENDING, sp.EVENT):
            earlier = self.spool.find_dedup(hook.name, plan.dedup_key, now - hook.dedup_window, row["id"])
            if earlier is not None:
                changes.update(state=sp.DUPLICATE, dup_of=earlier["id"], reason=f"repeat of delivery {earlier['id']}")
                self.spool.update(row["id"], kind="delivery", **changes)
                self.spool.bump(earlier["id"])
                return
        changes["state"] = plan.state
        if plan.state == sp.PENDING:
            within = hook.approve_within or cfg.settings.approve_within
            changes["expires_ts"] = float(row["received_ts"]) + within
        # This update's event is the delivery's first one: it carries the body.
        self.spool.update(row["id"], kind="delivery", with_body=True, **changes)
        outcome = {sp.REFUSED: "refused", sp.ERROR: "failed"}.get(plan.state, "accepted")
        audit(
            "hook.received",
            outcome,
            target={"hook": hook.name, "delivery": row["id"], "runnable": plan.runnable or None},
            detail={"state": plan.state, "rule": plan.rule, "autonomy": plan.autonomy, "source": row.get("source"), "reason": plan.reason or None},
        )
        if plan.state == sp.PENDING:
            fresh = self.spool.get(row["id"]) or row
            if not self.spool.watching(hook.name):
                self._mail_one(cfg, hook, fresh, "nobody is following this venv on a console, or everyone has muted it")

    # running

    def _slots(self, cfg: HookConfig) -> int:
        return cfg.settings.max_runs - len(self._runs)

    def start_runs(self, cfg: HookConfig) -> None:
        for row in self.spool.in_state(sp.APPROVED, sp.WAITING):
            if self._slots(cfg) <= 0:
                return
            hook = cfg.hooks.get(row["hook"])
            if hook is None:
                self.spool.update(row["id"], state=sp.REFUSED, reason=f"hook {row['hook']} is no longer set up")
                continue
            busy = sum(1 for r in self._runs.values() if r.hook == hook.name)
            if busy >= hook.max_running:
                if hook.skip_if_running and row["state"] == sp.WAITING:
                    self.spool.update(row["id"], state=sp.SKIPPED, reason="the hook's previous run is still going (skip_if_running)")
                continue
            self._start(row, hook, cfg)

    def _start(self, row: dict[str, Any], hook: Hook, cfg: HookConfig) -> None:
        approved = row["state"] == sp.APPROVED
        spec = None
        with contextlib.suppress(Exception):
            from .discovery import discover

            spec = (discover().get(row["runnable"]) or {}).get("spec")
        request = {
            "runnable": row["runnable"],
            "argv": list(row.get("command") or []),
            "env": {**run_env(row["runnable"], spec, list(row.get("command") or []), row.get("args") or {}), "RUNSPEC_HOOK": hook.name, "RUNSPEC_HOOK_DELIVERY": str(row["id"])},
            "label": f"{HOOK_LABEL_PREFIX}{hook.name}",
            "trigger": TRIGGER_WEBHOOK_APPROVED if approved else TRIGGER_WEBHOOK,
            "env_file": str(Path(sys.prefix) / ".runspec_scheduler_env"),
            "timeout": hook.timeout if hook.timeout is not None else 6 * 3600,
        }
        env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
        out_fd, out_path = -1, ""
        try:
            if hook.respond == "output":
                out_fd, out_path = open_output(self.spool, row["id"])
            proc = subprocess.Popen(
                [self.python, "-m", "runspec_scheduler.hookexec"],
                stdin=subprocess.PIPE,
                stdout=out_fd if out_fd >= 0 else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=os.path.expanduser("~") if os.path.isdir(os.path.expanduser("~")) else None,
                start_new_session=True,
                close_fds=True,
            )
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(request).encode())
            proc.stdin.close()
        except OSError as exc:
            self.spool.update(row["id"], state=sp.REFUSED, reason=f"couldn't start the run: {exc}")
            audit("hook.run", "failed", target={"hook": hook.name, "delivery": row["id"]}, detail={"error": str(exc)})
            return
        finally:
            if out_fd >= 0:
                os.close(out_fd)
        self._runs[row["id"]] = _Run(delivery=row["id"], hook=hook.name, proc=proc, started=time.time(), output=out_path, output_max=hook.respond_max)
        self.spool.update(row["id"], state=sp.RUNNING, started_ts=time.time(), pid=proc.pid)
        how = "approved by " + row["decided_by"] if approved else f"{row.get('autonomy') or 'autonomous'}, set up by the hook"
        logger.info("webhook %s delivery %d: running %s (%s)", hook.name, row["id"], row["runnable"], how)

    def reap(self) -> None:
        for delivery, run in list(self._runs.items()):
            code = run.proc.poll()
            if code is None:
                continue
            del self._runs[delivery]
            if run.output:
                collect_output(self.spool, delivery, run.output_max)
            self.spool.update(delivery, state=sp.RAN, finished_ts=time.time(), exit_code=code)
            audit("hook.run", "succeeded" if code == 0 else "failed", target={"hook": run.hook, "delivery": delivery}, detail={"exit_code": code})
            self.notify()

    def stop_runs(self) -> None:
        """At shutdown: leave runs going (they're in their own sessions) but
        stop tracking them; mark them so a restart doesn't think they're new."""
        for delivery, run in list(self._runs.items()):
            self.spool.update(delivery, reason=f"the daemon stopped while pid {run.proc.pid} ran")
        self._runs.clear()

    def recover(self) -> None:
        """At start: a delivery left ``running`` by a previous daemon finished
        without us — say so rather than leave it running forever."""
        for row in self.spool.in_state(sp.RUNNING):
            pid = int(row.get("pid") or 0)
            alive = False
            if pid:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if not alive:
                hook = self.cfg.hooks.get(row["hook"]) if self.cfg else None
                collect_output(self.spool, row["id"], hook.respond_max if hook else DEFAULT_RESPOND_MAX)
                self.spool.update(row["id"], state=sp.RAN, finished_ts=time.time(), reason="finished while the daemon was stopped (see History for its exit code)")

    # approvals

    def expire(self, now: float) -> None:
        for row in self.spool.in_state(sp.PENDING):
            expired = row.get("expires_ts") and float(row["expires_ts"]) <= now
            if expired and self.spool.update(row["id"], when=f"state = '{sp.PENDING}'", state=sp.EXPIRED, reason="nobody approved it in time"):
                audit("hook.expired", "succeeded", target={"hook": row["hook"], "delivery": row["id"]})

    def mail(self, cfg: HookConfig, now: float) -> None:
        if not cfg.smtp.configured:
            return
        for row in self.spool.in_state(sp.PENDING):
            if row.get("emailed_ts"):
                continue
            hook = cfg.hooks.get(row["hook"])
            if hook is None:
                continue
            wait = hook.expect_claim_within if hook.expect_claim_within is not None else cfg.settings.expect_claim_within
            if wait and now - float(row["received_ts"]) >= wait:
                self._mail_one(cfg, hook, row, f"nobody picked it up within {wait // 60} minutes")

    def _mail_one(self, cfg: HookConfig, hook: Hook, row: dict[str, Any], why: str) -> None:
        if row.get("emailed_ts"):
            return
        if not cfg.smtp.configured:
            # Nobody will hear about it — say so once, on the delivery and in the log.
            if not row.get("mail"):
                logger.warning("webhook %s delivery %d needs an approval and %s; no [smtp] is set up to mail it", hook.name, row["id"], why)
                self.spool.update(row["id"], kind="mail", mail=f"not mailed: no [smtp] set up ({why})")
            return
        to = hook.email_to or cfg.smtp.to
        problem = send_approval_mail(cfg.smtp, to, row, why=why, venv=Path(sys.prefix).name, config_path=self.config_path)
        if problem:
            logger.warning("webhook %s delivery %d: couldn't mail it: %s", hook.name, row["id"], problem)
            self.spool.update(row["id"], kind="mail", emailed_ts=time.time(), mail=f"mail failed: {problem}")
            return
        self.spool.update(row["id"], kind="mail", emailed_ts=time.time(), mail=f"mailed to {', '.join(to)} ({why})")
        audit("hook.mailed", "succeeded", target={"hook": hook.name, "delivery": row["id"]}, detail={"to": list(to), "why": why})


# ── decisions (verbs run these as the venv owner) ─────────────────────────────


def approve(spool: sp.Spool, delivery_id: int, who: str, note: str = "") -> tuple[bool, dict[str, Any] | None]:
    """First approval wins. Returns (won, the delivery as it is now)."""
    now = time.time()
    won = spool.update(
        delivery_id,
        kind="approved",
        when=f"state = '{sp.PENDING}' AND (expires_ts IS NULL OR expires_ts > {now!r})",
        state=sp.APPROVED,
        decided_by=who,
        decided_ts=now,
        note=note,
    )
    row = spool.get(delivery_id)
    if won and row:
        audit("hook.approved", "succeeded", target={"hook": row["hook"], "delivery": delivery_id, "runnable": row["runnable"]}, detail={"by": who, "note": note or None})
    return won, row


def claim(spool: sp.Spool, delivery_id: int, who: str, note: str = "") -> tuple[bool, dict[str, Any] | None]:
    """Take a delivery for one console: the first claim wins. Consoles whose
    trigger has ``claim = true`` park a delivery behind a Claim button and only
    the winner runs its trigger; the others see who has it in the follow
    stream. A claim doesn't approve, run or settle anything on this host.
    Returns (won, the delivery as it is now)."""
    won = spool.update(delivery_id, kind="claimed", when="claimed_by = ''", claimed_by=who, claimed_ts=time.time(), claim_note=note)
    row = spool.get(delivery_id)
    if won and row:
        audit("hook.claimed", "succeeded", target={"hook": row["hook"], "delivery": delivery_id}, detail={"by": who, "note": note or None})
    return won, row


def deny(spool: sp.Spool, delivery_id: int, who: str, note: str = "") -> tuple[bool, dict[str, Any] | None]:
    """Deny settles it for everyone. Returns (won, the delivery as it is now)."""
    won = spool.update(delivery_id, kind="denied", when=f"state = '{sp.PENDING}'", state=sp.DENIED, decided_by=who, decided_ts=time.time(), note=note)
    row = spool.get(delivery_id)
    if won and row:
        audit("hook.denied", "succeeded", target={"hook": row["hook"], "delivery": delivery_id, "runnable": row["runnable"]}, detail={"by": who, "note": note or None})
    return won, row


def settings_summary(settings: HooksSettings) -> dict[str, Any]:
    out = settings.to_dict()
    out["problem"] = settings.listen_problem() or None
    return out
