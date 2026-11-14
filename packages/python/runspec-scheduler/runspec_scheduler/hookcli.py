"""hookcli.py — the ``runspec-scheduler hooks …`` verbs.

Read-only: ``list``, ``deliveries``, ``show``, ``check``, ``test``, ``follow``.
Changes (run as the venv owner; runspec-console runs them with ``sudo -n -u``,
so the person is ``SUDO_USER``): ``save``, ``remove``, ``token``,
``settings``, ``approve``, ``deny``, ``mute``, ``unmute``.

Every verb prints JSON for programs and text at a terminal (see
:func:`runspec_scheduler.cli._emit`).
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import hookconfig as hc
from . import hookrun
from . import spool as sp
from .config import parse_duration, read_config

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

FOLLOW_POLL = 0.5
FOLLOW_SEEN_EVERY = 5.0
FOLLOW_PING_EVERY = 15.0


def _spool(config_path: Path) -> sp.Spool:
    return sp.Spool(sp.spool_path_for(config_path))


def _spool_if_any(config_path: Path) -> sp.Spool | None:
    path = sp.spool_path_for(config_path)
    return sp.Spool(path) if path.exists() else None


def _host() -> str:
    return socket.gethostname()


# ── status / list ─────────────────────────────────────────────────────────────


def hooks_status(config_path: Path, daemon_hooks: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``hooks`` block of ``status`` (and what ``hooks list`` prints)."""
    out: dict[str, Any] = {"ok": True, "config": str(config_path), "hooks_dir": str(hc.hooks_dir(config_path)), "host": _host()}
    try:
        cfg = hc.load_hook_config(config_path)
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return {**out, "ok": False, "error": f"[hooks]/[smtp] can't be read: {exc}"}
    settings = cfg.settings
    out["settings"] = hookrun.settings_summary(settings)
    out["smtp"] = cfg.smtp.to_dict()
    rows = cfg.listing()
    for row in rows:
        row["url"] = settings.url(_host(), row["name"]) if settings.port else None
    out["hooks"] = rows
    out["errors"] = cfg.errors
    out["daemon"] = daemon_hooks
    spool = _spool_if_any(config_path)
    if spool is not None:
        try:
            out["pending"] = [sp.snapshot(r) for r in spool.in_state(sp.PENDING, sp.APPROVED)]
            out["mutes"] = spool.mutes()
            out["followers"] = spool.followers()
            out["last_event"] = spool.last_event_id()
        finally:
            spool.close()
    else:
        out.update({"pending": [], "mutes": [], "followers": [], "last_event": 0})
    return out


def deliveries(config_path: Path, *, hook: str | None, state: str | None, limit: int) -> dict[str, Any]:
    spool = _spool_if_any(config_path)
    if spool is None:
        return {"ok": True, "deliveries": []}
    try:
        return {"ok": True, "deliveries": [sp.snapshot(r) for r in spool.recent(hook=hook, state=state, limit=limit)]}
    finally:
        spool.close()


def show(config_path: Path, delivery_id: int) -> dict[str, Any]:
    spool = _spool_if_any(config_path)
    row = spool.get(delivery_id) if spool else None
    if row is None:
        return {"ok": False, "error": f"no delivery {delivery_id}"}
    snap = sp.snapshot(row, body=False)
    snap["body"] = sp.body_text(row, None) if row.get("body") is not None else None
    snap["headers"] = row.get("headers") or {}
    return {"ok": True, "delivery": snap}


# ── check / test ──────────────────────────────────────────────────────────────


def check(path: Path) -> dict[str, Any]:
    problems = hc.check_file(path)
    names = []
    with contextlib.suppress(Exception):
        names = [str(r.get("name") or "") for r in hc.read_hook_file(path)]
    return {"ok": not problems, "file": str(path), "hooks": names, "problems": problems}


#: Stands in for a draft's missing token so it parses (a draft is only tested).
_DRAFT_TOKEN = "0" * 64


def test_draft(config_path: Path, text: str) -> dict[str, Any]:
    """Dry run of a hook that isn't saved yet (the console's editor): stdin is
    ``{"hook": {table}, "body": "…" | null, "content_type": "…"}``. With no
    body, the last one the hook (by name) received is used."""
    try:
        data = json.loads(text or "{}")
    except ValueError as exc:
        return {"ok": False, "error": f"--draft reads JSON on stdin: {exc}"}
    raw = dict(data.get("hook") or {}) if isinstance(data, dict) else {}
    raw.pop("token", None)
    raw.pop("password", None)
    if not raw.get("token_sha256") and not raw.get("username"):
        raw["token_sha256"] = _DRAFT_TOKEN
    try:
        hook = hc.parse_hook(raw, source="draft")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    body = data.get("body")
    return _test_hook(config_path, hook, body=body.encode("utf-8") if isinstance(body, str) else None, content_type=str(data.get("content_type") or ""), delivery=None)


def test(config_path: Path, name: str, *, body: bytes | None, content_type: str, delivery: int | None, file: Path | None) -> dict[str, Any]:
    """Dry run: what ``name`` would do with a body (stdin, ``--file``, or a
    stored delivery's). Nothing runs; nothing is stored."""
    cfg = hc.load_hook_config(config_path)
    hook: hc.Hook | None = None
    if file is not None:
        raws = hc.read_hook_file(file)
        raw = next((r for r in raws if str(r.get("name") or "") == name), raws[0] if len(raws) == 1 and not name else None)
        if raw is None:
            return {"ok": False, "error": f"{file} has no hook {name!r}"}
        hook = hc.parse_hook(raw, source="file", file=str(file))
    else:
        hook = cfg.hooks.get(name)
    if hook is None:
        return {"ok": False, "error": f"no hook {name!r}; set up: {', '.join(sorted(cfg.hooks)) or '(none)'}"}
    return _test_hook(config_path, hook, body=body, content_type=content_type, delivery=delivery)


def _test_hook(config_path: Path, hook: hc.Hook, *, body: bytes | None, content_type: str, delivery: int | None) -> dict[str, Any]:
    source = "stdin"
    if delivery is not None or body is None:
        spool = _spool_if_any(config_path)
        row = None
        if spool is not None:
            row = spool.get(delivery) if delivery is not None else spool.last_body(hook.name)
        if row is None:
            missing = f"no body given and {hook.name} hasn't received one yet: pipe one in (hooks test --hook {hook.name} < body.json)"
            return {"ok": False, "error": f"no stored delivery {delivery}" if delivery is not None else missing}
        raw_body = row.get("body") or b""
        body = raw_body.tobytes() if isinstance(raw_body, memoryview) else bytes(raw_body)
        content_type = content_type or row.get("content_type") or ""
        source = f"delivery {row['id']}"
    resolver = hookrun.Resolver()
    transform = hookrun.run_transform if hook.transform else None
    items = hookrun.dry_run(hook, body or b"", content_type, resolve=resolver, transform=transform)
    return {"ok": True, "hook": hook.name, "source": source, "items": items}


# ── changes ───────────────────────────────────────────────────────────────────


def _read_hook_input(text: str) -> dict[str, Any]:
    """One hook from JSON or TOML text (``[[hook]]`` or a bare table)."""
    text = text.strip()
    if not text:
        raise ValueError("no hook given: pipe one in as JSON or TOML")
    if text.startswith("{"):
        data = json.loads(text)
    else:
        data = tomllib.loads(text)
        hooks = data.get("hook")
        if isinstance(hooks, list):
            if len(hooks) != 1:
                raise ValueError("give one [[hook]] at a time")
            data = hooks[0]
    if not isinstance(data, dict):
        raise ValueError("a hook is a table / JSON object")
    return dict(data)


def save(config_path: Path, text: str, *, new_token: bool) -> dict[str, Any]:
    """Add or replace a console-made hook. A plain ``token`` / ``password`` in
    the input is hashed before anything is written and is never echoed back.
    ``new_token`` makes a token; the caller shows it once (it's in the answer
    under ``secrets``, so print it with :func:`show_secret`, never ``print``)."""
    raw = _read_hook_input(text)
    secrets: dict[str, str] = {}
    given = sorted(k for k in ("token", "password") if raw.get(k))
    if new_token:
        if raw.get("token"):
            raise ValueError("give a token or --new-token, not both")
        raw, token = hc.with_secret(raw)
        secrets["token"] = token
    existing = next((dict(r) for r in read_config(config_path).get("hook") or [] if str(r.get("name") or "") == str(raw.get("name") or "")), None)
    if existing:
        # Keep the stored secrets unless new ones were made or given.
        for key, plain in (("token_sha256", "token"), ("password_hash", "password")):
            if key not in raw and existing.get(key) and not raw.get(plain):
                raw[key] = existing[key]
    hook = hc.save_hook(config_path, raw)
    cfg = hc.load_hook_config(config_path)
    out: dict[str, Any] = {"ok": True, "hook": hook.summary(), "replaced": existing is not None}
    if cfg.settings.port:
        out["url"] = cfg.settings.url(_host(), hook.name)
    new_secret = sorted({*secrets, *given})
    if new_secret:
        out["new_secret"] = new_secret
    if secrets:
        out["secrets"] = secrets
        out["note"] = "Shown once: only a hash is stored. Put it in the sending app now."
    hookrun.audit("hook.saved", "succeeded", target={"hook": hook.name}, detail={"by": hookrun.who_am_i(), "replaced": existing is not None, "new_secret": new_secret})
    return out


def remove(config_path: Path, name: str) -> dict[str, Any]:
    removed = hc.remove_hook(config_path, name)
    if not removed:
        cfg = hc.load_hook_config(config_path)
        deployed = next((h for h in cfg.listed if h.name == name and h.source == "deployed"), None)
        if deployed is not None:
            return {"ok": False, "error": f"{name} is deployed from {deployed.file}; remove it there"}
        return {"ok": False, "error": f"no console-made hook {name!r}"}
    hookrun.audit("hook.removed", "succeeded", target={"hook": name}, detail={"by": hookrun.who_am_i()})
    return {"ok": True, "removed": name}


def _literal(value: str) -> Any:
    """``8443`` → 8443, ``true`` → True, ``["a"]`` → list, anything else → text."""
    try:
        return tomllib.loads(f"v = {value}")["v"]
    except Exception:  # noqa: BLE001 — not a TOML literal, so it's text
        return value


def settings(config_path: Path, hooks_changes: list[str], smtp_changes: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True}
    for table, changes in (("hooks", hooks_changes), ("smtp", smtp_changes)):
        if not changes:
            continue
        parsed: dict[str, Any] = {}
        for item in changes:
            key, sep, value = item.partition("=")
            if not sep:
                raise ValueError(f"{item!r}: use key=value (an empty value puts the default back)")
            parsed[key.strip()] = _literal(value.strip()) if value.strip() else None
        out[table] = hc.set_settings(config_path, table, parsed)
    if hooks_changes or smtp_changes:
        changed = {"hooks": sorted(k.split("=")[0] for k in hooks_changes), "smtp": sorted(k.split("=")[0] for k in smtp_changes)}
        hookrun.audit("hook.settings", "succeeded", detail={"by": hookrun.who_am_i(), **changed})
    cfg = hc.load_hook_config(config_path)
    out["settings"] = hookrun.settings_summary(cfg.settings)
    out["smtp_settings"] = cfg.smtp.to_dict()
    return out


def token() -> dict[str, Any]:
    """A fresh random token and its SHA-256, for a hooks.d file. Nothing is
    stored. The token is in the answer, so show it with :func:`show_secret`."""
    value = hc.new_token()
    return {"ok": True, "token": value, "token_sha256": hc.sha256(value), "note": "Put token_sha256 in the hook and the token in the sending app; the token isn't stored."}


def basic_hash(secret: str) -> dict[str, Any]:
    """The ``password_hash`` for a basic-auth password a person chose (read from
    stdin, never an argument, never printed back)."""
    secret = secret.rstrip("\r\n")
    if len(secret) < 12:
        return {"ok": False, "error": "use a password of at least 12 characters (read from stdin, one line)"}
    note = "Put password_hash (and a username) in the hook and the password in the sending app; the password isn't stored."
    return {"ok": True, "password_hash": hc.hash_password(secret), "note": note}


def show_secret(text: str) -> None:
    """Write output that holds a secret to the real stdout only.

    After ``parse()``, ``sys.stdout`` is runspec's tee, which copies every line
    into the run's log file; a token printed there would sit in the log. This
    goes to the terminal (or the SSH channel the console reads) and nowhere
    else."""
    out = sys.__stdout__ or sys.stdout
    out.write(text if text.endswith("\n") else text + "\n")
    out.flush()


def decide(config_path: Path, delivery_id: int, verdict: str, note: str) -> dict[str, Any]:
    spool = _spool(config_path)
    who = hookrun.who_am_i()
    try:
        won, row = (hookrun.approve if verdict == "approve" else hookrun.deny)(spool, delivery_id, who, note)
    finally:
        spool.close()
    if row is None:
        return {"ok": False, "error": f"no delivery {delivery_id}"}
    snap = sp.snapshot(row)
    if won:
        out: dict[str, Any] = {"ok": True, "won": True, "delivery": snap}
        if verdict == "approve":
            from .daemon import read_state

            state = read_state(config_path) or {}
            if not (state.get("hooks") or {}).get("listening") and not _daemon_alive(state):
                out["warning"] = "the daemon isn't running: it runs this when it starts (unless it has expired by then)"
        return out
    if row["state"] == sp.PENDING:
        why = "it has expired"
    elif row.get("decided_by"):
        why = f"already {row['state']} by {row['decided_by']}"
    else:
        why = f"it is {row['state']}"
    return {"ok": False, "won": False, "error": f"delivery {delivery_id} can't be {verdict}d: {why}", "delivery": snap}


def _daemon_alive(state: dict[str, Any]) -> bool:
    pid = int(state.get("pid") or 0)
    if not pid or state.get("stopped"):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def mute(config_path: Path, hook: str, duration: str, reason: str, who: str | None = None) -> dict[str, Any]:
    seconds = parse_duration(duration, "--for")
    if seconds <= 0:
        raise ValueError("--for must be longer than 0")
    spool = _spool(config_path)
    try:
        data = spool.mute(who or hookrun.who_am_i(), hook or "*", time.time() + seconds, reason)
    finally:
        spool.close()
    return {"ok": True, "mute": data}


def unmute(config_path: Path, hook: str | None, who: str | None = None) -> dict[str, Any]:
    """Take one mute off (``hook``; ``"*"`` = the all-hooks mute), or every one
    of yours when ``hook`` is None."""
    spool = _spool(config_path)
    try:
        removed = spool.unmute(who or hookrun.who_am_i(), hook or None)
    finally:
        spool.close()
    return {"ok": True, "removed": removed}


# ── follow ────────────────────────────────────────────────────────────────────


def follow(config_path: Path, *, since: int | None, replay: str, who: str | None, once: bool = False, out: Any = None) -> int:
    """Stream the spool's events as JSON lines, from ``since`` (else the
    ``replay`` window), until the reader goes away. First a ``hello`` line with
    what's waiting now, the mutes and the hooks; a ``ping`` every 15 s keeps
    a dead connection from going unnoticed."""
    out = out or sys.stdout
    spool = _spool(config_path)
    me = who or hookrun.who_am_i()
    token_id = uuid.uuid4().hex
    try:
        if since is None:
            window = parse_duration(replay or "15m", "--replay")
            since = spool.first_event_after(time.time() - window)
        status = hooks_status(config_path)
        hello = {
            "kind": "hello",
            "event": since,
            "last_event": spool.last_event_id(),
            "host": _host(),
            "venv": str(Path(sys.prefix)),
            "who": me,
            "pending": [sp.snapshot(r, body=True) for r in spool.in_state(sp.PENDING, sp.APPROVED, sp.RUNNING)],
            "mutes": spool.mutes(),
            "hooks": status.get("hooks") or [],
            "settings": status.get("settings"),
        }
        _write(out, hello)
        spool.follower_seen(token_id, me, os.getpid())
        last_seen = last_ping = time.monotonic()
        while True:
            for event in spool.events_since(since):
                since = event["event"]
                _write(out, event)
            if once:
                return 0
            now = time.monotonic()
            if now - last_seen >= FOLLOW_SEEN_EVERY:
                spool.follower_seen(token_id, me, os.getpid())
                last_seen = now
            if now - last_ping >= FOLLOW_PING_EVERY:
                _write(out, {"kind": "ping", "event": since, "ts": time.time()})
                last_ping = now
            time.sleep(FOLLOW_POLL)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    finally:
        with contextlib.suppress(Exception):
            spool.follower_gone(token_id)
        spool.close()


def _write(out: Any, payload: dict[str, Any]) -> None:
    out.write(json.dumps(payload, default=str) + "\n")
    out.flush()


# ── text ──────────────────────────────────────────────────────────────────────


def render_list(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return str(payload.get("error"))
    s = payload.get("settings") or {}
    daemon = payload.get("daemon") or {}
    lines = [
        f"Webhooks:  {'on' if s.get('enabled') else 'off'}" + (f", port {s.get('port')} on {s.get('bind')}{' (HTTPS)' if s.get('tls') else ''}" if s.get("port") else ""),
    ]
    if s.get("problem") and s.get("enabled"):
        lines.append(f"Problem:   {s['problem']}")
    if daemon.get("listening"):
        lines.append(f"Listening: {daemon['listening']}")
    elif daemon.get("error"):
        lines.append(f"Listener:  {daemon['error']}")
    lines.append(f"Followed by: {', '.join(sorted({f['who'] for f in payload.get('followers') or []})) or 'nobody right now'}")
    for m in payload.get("mutes") or []:
        lines.append(f"Muted:     {m['who']} muted {m['hook'] if m['hook'] != '*' else 'all hooks'} until {m['until']}")
    lines.append("")
    hooks = payload.get("hooks") or []
    if not hooks:
        lines.append("No hooks yet. Make one: runspec-scheduler hooks save --new-token < hook.toml")
    for h in hooks:
        tag = " (overridden by a deployed copy)" if h.get("overridden") else (" (deployed)" if h.get("source") == "deployed" else "")
        lines.append(f"{h['name']}{tag}{'' if h.get('enabled') else ' [off]'}")
        if h.get("url"):
            lines.append(f"  {h['url']}")
        for r in h.get("rules") or []:
            what = f"run {r['run']} {' '.join(r['command'])}".rstrip() if r["action"] == "run" else r["action"]
            lines.append(f"  rule {r['name']}: {what}")
    for err in payload.get("errors") or []:
        lines.append(f"! {err.get('hook') or err.get('file')}: {err.get('error')}")
    pending = payload.get("pending") or []
    if pending:
        lines.append("")
        lines.append("Waiting for approval:")
        for p in pending:
            lines.append(f"  {p['id']}  {p['hook']}  {p['runnable']} {' '.join(p.get('command') or [])}  expires {p.get('expires')}")
    return "\n".join(lines)


#: What each delivery state means, in words (``hooks test`` and ``deliveries``).
OUTCOME_WORDS = {
    sp.QUEUED: "received, not worked out yet",
    sp.WAITING: "runs at once (autonomous)",
    sp.PENDING: "waits for someone to approve it",
    sp.APPROVED: "approved, about to run",
    sp.RUNNING: "running",
    sp.RAN: "ran",
    sp.DENIED: "denied",
    sp.EXPIRED: "expired: nobody approved it in time",
    sp.REFUSED: "refused",
    sp.SKIPPED: "skipped",
    sp.EVENT: "shown on consoles only, runs nothing",
    sp.NO_MATCH: "no rule matched: nothing happens",
    sp.DUPLICATE: "a repeat: counted on the first one",
    sp.ERROR: "couldn't be worked out",
}


def outcome_words(state: str) -> str:
    return OUTCOME_WORDS.get(state, state)


def render_test(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return str(payload.get("error"))
    lines = [f"{payload['hook']} with {payload['source']}:"]
    for i, item in enumerate(payload.get("items") or []):
        lines.append("")
        if len(payload["items"]) > 1:
            lines.append(f"Item {i + 1}")
        lines.append(f"  outcome:  {outcome_words(item['state'])}{(' — ' + item['reason']) if item.get('reason') else ''}")
        if item.get("rule"):
            lines.append(f"  rule:     {item['rule']}")
        if item.get("runnable"):
            lines.append(f"  runs:     {item['runnable']} {' '.join(item.get('command') or [])}".rstrip())
            lines.append(f"  autonomy: {item.get('autonomy')}")
            for k, v in (item.get("args") or {}).items():
                lines.append(f"  arg {k} = {json.dumps(v, default=str)}")
        if item.get("dedup_key"):
            lines.append(f"  dedup:    {item['dedup_key']}")
        fields = {k: v for k, v in (item.get("fields") or {}).items() if k not in ("item",)}
        if fields:
            lines.append("  fields:")
            for k, v in fields.items():
                lines.append(f"    {k} = {json.dumps(v, default=str)[:200]}")
    return "\n".join(lines)


def render_check(payload: dict[str, Any]) -> str:
    if payload.get("ok"):
        return f"{payload['file']}: OK ({len(payload.get('hooks') or [])} hook(s))"
    return "\n".join([f"{payload['file']}:", *(f"  {p}" for p in payload.get("problems") or [])])


def render_deliveries(payload: dict[str, Any]) -> str:
    rows = payload.get("deliveries") or []
    if not rows:
        return "No deliveries."
    out = []
    for r in rows:
        who = f" by {r['decided_by']}" if r.get("decided_by") else ""
        code = f" exit {r['exit_code']}" if r.get("exit_code") is not None else ""
        out.append(f"{r['id']:>6}  {r['received']}  {r['hook']}  {r['state']}{who}{code}  {r.get('runnable') or ''} {r.get('reason') or ''}".rstrip())
    return "\n".join(out)


def render_decision(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return str(payload.get("error"))
    d = payload["delivery"]
    text = f"Delivery {d['id']} {d['state']} by {d['decided_by']}."
    if payload.get("warning"):
        text += f"\n{payload['warning']}"
    return text


def render_save(payload: dict[str, Any]) -> str:
    if not payload.get("ok"):
        return str(payload.get("error"))
    lines = [f"Saved hook {payload['hook']['name']}{' (replaced)' if payload.get('replaced') else ''}."]
    if payload.get("url"):
        lines.append(f"Address: {payload['url']}")
    for k, v in (payload.get("secrets") or {}).items():
        lines.append(f"{k.capitalize()}: {v}")
    if payload.get("note"):
        lines.append(payload["note"])
    return "\n".join(lines)
