"""bridge_webhooks.py — following hosts' webhooks (console 0.438.0).

A venv's runspec-scheduler (0.11.0+) receives webhooks and keeps them in its
spool. This console follows the hooks a host entry names (``hooks = ["…"]`` or
``["*"]``, next to ``room``) in every venv of that host: one
:class:`~runspec_console.sources.webhook.WebhookFollower` per host + venv, each
running ``runspec-scheduler hooks follow`` as the venv owner over the pooled SSH
connection (an exec channel), the way the Schedules tab runs ``status``.

What it gives the UI: pending approvals as Console-tab cards (Approve and Deny go
to the host, where the first decision wins; Dismiss hides a card here only),
recent deliveries, shared mutes, and the hook editor's calls (list, save, test a
draft, remove, settings). Every fresh delivery is also a ``webhook`` trigger
event (type = the hook's name) for console triggers.

While Rota says off shift or the screen is locked, this console mutes every hook
it follows on the host (a renewed 10-minute ``*`` mute, reason shown to others),
so the host mails approvals instead of waiting on someone who isn't there.
Approvals and host runs need no model, so all of this works with AI off.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

from . import bridge_utils as utils_mod
from . import config as config_mod
from . import executor as executor_mod
from . import hosts as hosts_mod
from . import webhooks as wh

logger = logging.getLogger("runspec.console.webhooks")

#: How long one automatic away mute lasts; it's renewed while still away, so a
#: console that dies while locked stops counting as muted soon after.
AWAY_MUTE = "10m"
AWAY_RENEW_S = 300.0
PRESENCE_EVERY_S = 30.0


class _ProcessChannel:
    """A local ``Popen`` with the channel methods :class:`LineStream` reads."""

    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc

    def settimeout(self, _t: float) -> None:  # a pipe read just blocks
        return None

    def recv(self, n: int) -> bytes:
        assert self._proc.stdout is not None
        return self._proc.stdout.read1(n)  # type: ignore[attr-defined,no-any-return]

    def recv_stderr_ready(self) -> bool:
        return self._proc.poll() is not None

    def recv_stderr(self, n: int) -> bytes:
        assert self._proc.stderr is not None
        return self._proc.stderr.read(n)

    def exit_status_ready(self) -> bool:
        return self._proc.poll() is not None

    def recv_exit_status(self) -> int:
        return self._proc.wait()

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class WebhooksMixin:
    _lock: threading.Lock
    _ssh_pool: Any
    _hosts: list[dict[str, Any]]

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def _wh(self) -> dict[str, Any]:
        """This console's webhook state, made on first use (the Bridge's
        ``__init__`` stays untouched): followers, per-venv state kept across
        restarts (cursor + dismissed cards), and the presence thread."""
        state = getattr(self, "_webhook_state", None)
        if state is None:
            state = {
                "followers": {},
                "venvs": {},
                "stop": threading.Event(),
                "presence": None,
                "away": "",
                "away_at": 0.0,
            }
            self._webhook_state = state
        return state

    def _webhook_targets(self) -> list[tuple[str, str, str, list[str]]]:
        """``(host, venv, venv path, hooks)`` for each venv of each host entry
        that names hooks to follow. Hosts without SSH (this machine) need a
        Linux console, since runspec-scheduler is Linux-only."""
        out: list[tuple[str, str, str, list[str]]] = []
        for entry in list(getattr(self, "_hosts", []) or []):
            hooks = wh.followed_hooks(entry)
            if not hooks:
                continue
            host = str(entry.get("name") or "")
            if not entry.get("ssh") and sys.platform == "win32":
                continue
            for rp in utils_mod._paths(entry):
                if rp:
                    out.append((host, hosts_mod.venv_name(rp), rp, hooks))
        return out

    def start_webhooks(self) -> None:
        """Follow every configured host + venv. Idempotent."""
        from .sources.webhook import WebhookFollower

        st = self._wh()
        if st["followers"]:
            return
        targets = self._webhook_targets()
        if not targets:
            return
        replay_s = wh.parse_duration(self._webhook_replay(), 900)
        st["stop"].clear()
        followers: dict[str, Any] = {}
        for host, venv, rp, hooks in targets:
            key = f"{host}/{venv}"
            vs = st["venvs"].get(key)
            if vs is None:
                vs = wh.VenvHooks(host=host, venv=venv)
                st["venvs"][key] = vs
            vs.hooks = hooks
            vs.error = ""
            followers[key] = WebhookFollower(
                vs,
                self._webhook_opener(host, venv, rp),
                on_change=self._on_webhook_change,
                on_arrival=self._on_webhook_arrival,
                replay_s=replay_s,
            )
        # Venvs no longer followed are forgotten.
        for key in list(st["venvs"]):
            if key not in followers:
                st["venvs"].pop(key, None)
        st["followers"] = followers
        for f in followers.values():
            f.start()
        if st["presence"] is None or not st["presence"].is_alive():
            t = threading.Thread(
                target=self._webhook_presence_loop,
                name="webhooks-presence",
                daemon=True,
            )
            st["presence"] = t
            t.start()
        logger.info("webhooks: following %d venv(s)", len(followers))
        self._dispatch_webhooks()

    def stop_webhooks(self) -> None:
        st = self._wh()
        st["stop"].set()
        followers = st["followers"]
        st["followers"] = {}
        for f in followers.values():
            f.stop()
        for f in followers.values():
            f.join(timeout=3)
        for vs in st["venvs"].values():
            vs.connected = False

    def restart_webhooks(self) -> None:
        self.stop_webhooks()
        self.start_webhooks()
        self._dispatch_webhooks()

    # ── the follow stream ─────────────────────────────────────────────────────

    def _webhook_command(
        self, host: str, venv: str, rp: str, argv: list[str]
    ) -> tuple[list[str], bytes, str, bool]:
        """``(argv parts, stdin frame, binary, remote)`` for ``runspec-scheduler
        <argv>`` as the venv owner (as ``_run_scheduler_as`` builds it)."""
        from runspec.become import build_exec_argv

        entry = self._host_entry(host)  # type: ignore[attr-defined]
        if entry is None:
            raise wh.WebhookError(f"unknown host {host}")
        remote = bool(entry.get("ssh"))
        binary = (
            str(PurePosixPath(rp).parent / "runspec-scheduler")
            if remote
            else str(Path(rp).with_name("runspec-scheduler"))
        )
        owner, venv_owner = self._scheduler_owner(host, venv, remote)  # type: ignore[attr-defined]
        if venv_owner == "root":
            from .sources.webhook import ROOT_OWNED

            raise wh.WebhookError(ROOT_OWNED)
        parts, frame = build_exec_argv(
            [binary, *argv], owner, "sudo", "-n", env={"RUNSPEC_LABEL": "console"}
        )
        return parts, frame or b"", binary, remote

    def _webhook_opener(self, host: str, venv: str, rp: str) -> Any:
        from .sources.webhook import LineStream

        def open_stream(cursor: int | None) -> LineStream:
            argv = wh.follow_argv(cursor, self._webhook_replay())
            parts, frame, binary, remote = self._webhook_command(host, venv, rp, argv)
            if remote:
                entry = self._host_entry(host)  # type: ignore[attr-defined]
                pool = self._ssh_pool
                if pool is None or entry is None:
                    raise wh.WebhookError(
                        "following webhooks needs SSH connection pooling ([ssh] pool = true)"
                    )
                b = shlex.quote(binary)
                command = (
                    f"if [ ! -e {b} ]; then echo {b}: not found >&2; exit 127; fi; "
                    + executor_mod.shell_join(parts)
                )
                ssh_cfg = self.get_config().get("ssh")  # type: ignore[attr-defined]
                channel, release = pool.open_exec(
                    entry["ssh"],
                    command,
                    identity_file=entry.get("identityFile") or None,
                    global_ssh_config=ssh_cfg if isinstance(ssh_cfg, dict) else None,
                )
                channel.sendall(frame)
                return LineStream(channel, release)
            if not Path(binary).exists():
                raise wh.WebhookError(
                    "runspec-scheduler isn't installed in this venv (or is older than 0.11.0)"
                )
            proc = subprocess.Popen(
                parts,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert proc.stdin is not None
            proc.stdin.write(frame)
            proc.stdin.close()
            return LineStream(_ProcessChannel(proc))

        return open_stream

    def _webhook_replay(self) -> str:
        cfg = self.get_config().get("webhooks")  # type: ignore[attr-defined]
        if isinstance(cfg, dict) and cfg.get("replay"):
            return str(cfg["replay"])
        return wh.DEFAULT_REPLAY

    def _on_webhook_change(self, state: wh.VenvHooks) -> None:
        self._dispatch("runspec:webhooks", {"venv": state.snapshot()})  # type: ignore[attr-defined]

    def _dispatch_webhooks(self) -> None:
        self._dispatch("runspec:webhooks", {"all": True})  # type: ignore[attr-defined]

    def _on_webhook_arrival(
        self, state: wh.VenvHooks, delivery: dict[str, Any]
    ) -> None:
        """A fresh delivery: feed console triggers (``source = "webhook"``,
        ``event = <hook name>``) and cue the operator when it waits on them."""
        from .events import Event

        engine = getattr(self, "_trigger_engine", None)
        if engine is not None:
            try:
                engine.feed(
                    Event.new(
                        "webhook",
                        str(delivery.get("hook") or ""),
                        wh.trigger_payload(state.host, state.venv, delivery),
                    )
                )
            except Exception:
                logger.exception("webhooks: feeding the trigger engine failed")
        if delivery.get("state") == "pending":
            cue = getattr(self, "_connection_cue", None)
            notify = getattr(self, "_notify_attention", None)
            if callable(notify):
                try:
                    notify(
                        f"Webhook {delivery.get('hook')} on {state.host} is waiting for approval"
                    )
                except Exception:
                    logger.debug("webhooks: attention cue failed", exc_info=True)
            del cue

    # ── presence: Rota off shift / locked screen = muted ─────────────────────

    def _webhook_away_reason(self) -> str:
        try:
            if not self._build_lock_ok()():  # type: ignore[attr-defined]
                return "screen locked"
            if not self._build_clock_ok()():  # type: ignore[attr-defined]
                return "off shift"
        except Exception:
            logger.debug("webhooks: presence check failed", exc_info=True)
        return ""

    def _webhook_presence_loop(self) -> None:
        st = self._wh()
        while not st["stop"].wait(PRESENCE_EVERY_S):
            try:
                self._webhook_presence_tick()
            except Exception:
                logger.exception("webhooks: presence tick failed")

    def _webhook_presence_tick(self, now: float | None = None) -> None:
        st = self._wh()
        clock = time.time() if now is None else now
        reason = self._webhook_away_reason()
        venvs: list[wh.VenvHooks] = [v for v in st["venvs"].values() if v.connected]
        if reason:
            renew = (
                clock - float(st["away_at"] or 0) >= AWAY_RENEW_S
                or reason != st["away"]
            )
            for vs in venvs:
                if renew or vs.paused != reason:
                    res = self._webhook_call(
                        vs.host, vs.venv, wh.mute_argv("*", AWAY_MUTE, reason)
                    )
                    if res["ok"]:
                        vs.paused = reason
            if renew:
                st["away_at"] = clock
            st["away"] = reason
        elif st["away"]:
            for vs in venvs:
                if vs.paused:
                    res = self._webhook_call(vs.host, vs.venv, wh.unmute_argv("*"))
                    if res["ok"]:
                        vs.paused = ""
            if not any(v.paused for v in venvs):
                st["away"] = ""
                st["away_at"] = 0.0
        self._dispatch_webhooks()

    # ── one-shot runspec-scheduler calls ──────────────────────────────────────

    def _webhook_call(
        self,
        host: str,
        venv: str,
        argv: list[str],
        *,
        data: bytes = b"",
        timeout: int = 45,
    ) -> dict[str, Any]:
        call = self._host_sched_call  # type: ignore[attr-defined]
        out: dict[str, Any] = call(
            {"host": host, "venv": venv}, argv, timeout=timeout, data=data
        )
        return out

    def _webhook_audit(
        self, event: str, payload: dict[str, Any], **fields: Any
    ) -> None:
        audit = getattr(self, "_host_sched_audit", None)
        if callable(audit):
            audit(event, payload, str(fields.pop("id", "") or ""), **fields)

    @staticmethod
    def _wh_target(payload: dict[str, Any]) -> tuple[str, str]:
        host = str((payload or {}).get("host") or "")
        venv = str((payload or {}).get("venv") or "")
        if not host or not venv:
            raise wh.WebhookError("host and venv are needed")
        return host, venv

    # ── JS API ────────────────────────────────────────────────────────────────

    def webhooks_status(self) -> dict[str, Any]:
        """Every followed host + venv: connection, pending approvals, recent
        deliveries, mutes and hooks. ``configured`` is false when no host entry
        names hooks to follow."""
        st = self._wh()
        venvs = [vs.snapshot() for _, vs in sorted(st["venvs"].items())]
        return {
            "configured": bool(self._webhook_targets()),
            "away": st["away"],
            "venvs": venvs,
            "follows": {
                str(e.get("name") or ""): wh.followed_hooks(e)
                for e in getattr(self, "_hosts", []) or []
                if wh.followed_hooks(e)
            },
        }

    def webhook_decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Approve or deny a waiting delivery on its host (``verdict``). The
        first decision wins; the answer says who decided when it wasn't us."""
        try:
            host, venv = self._wh_target(payload)
            argv = wh.decide_argv(
                str(payload.get("verdict") or ""),
                payload.get("id"),
                str(payload.get("note") or ""),
            )
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        res = self._webhook_call(host, venv, argv)
        got = res.get("json") or {}
        out = {
            "ok": bool(res["ok"]) and bool(got.get("ok", True)),
            "error": res.get("error") or got.get("error") or "",
            "result": got,
        }
        self._webhook_audit(
            f"webhook.{payload.get('verdict')}",
            payload,
            id=payload.get("id"),
            ok=out["ok"],
        )
        return out

    def webhook_dismiss(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Hide a card on this console only (the delivery still waits for others)."""
        key = f"{payload.get('host')}/{payload.get('venv')}"
        vs = self._wh()["venvs"].get(key)
        try:
            did = int(payload.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return {"ok": False, "error": "no delivery id"}
        if vs is None:
            return {"ok": False, "error": f"{key} isn't followed"}
        if payload.get("undo"):
            vs.dismissed.discard(did)
        else:
            vs.dismissed.add(did)
        self._on_webhook_change(vs)
        return {"ok": True}

    def webhook_mute(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Mute a hook (or ``*``) on its host for ``for`` (e.g. ``2h``)."""
        try:
            host, venv = self._wh_target(payload)
            argv = wh.mute_argv(
                str(payload.get("hook") or "*"),
                payload.get("for") or "1h",
                str(payload.get("reason") or ""),
            )
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        res = self._webhook_call(host, venv, argv)
        return {
            "ok": res["ok"],
            "error": res.get("error") or "",
            "mute": (res.get("json") or {}).get("mute"),
        }

    def webhook_unmute(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            host, venv = self._wh_target(payload)
            argv = wh.unmute_argv(str(payload.get("hook") or "*"))
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        res = self._webhook_call(host, venv, argv)
        return {"ok": res["ok"], "error": res.get("error") or ""}

    def webhook_follow(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Set which hooks this console follows on a host (the host entry's
        ``hooks``; ``[]`` stops following) and reconnect."""
        host = str((payload or {}).get("host") or "")
        try:
            hooks = wh.validate_follow((payload or {}).get("hooks") or [])
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        path = config_mod.hosts_path()
        entries = hosts_mod.load_hosts(path)
        entry = next((e for e in entries if str(e.get("name") or "") == host), None)
        if entry is None:
            return {"ok": False, "error": f"unknown host {host}"}
        if hooks:
            entry["hooks"] = hooks
        else:
            entry.pop("hooks", None)
        hosts_mod.save_hosts(path, entries)
        reload = getattr(self, "_reload_hosts", None)
        if callable(reload):
            reload()
        self.restart_webhooks()
        self._dispatch("runspec:hosts_updated", {})  # type: ignore[attr-defined]
        return {"ok": True, "hooks": hooks}

    def webhook_hooks(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``hooks list`` in one venv: settings, every hook (with its table),
        mutes, followers and what's waiting."""
        try:
            host, venv = self._wh_target(payload)
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        res = self._webhook_call(host, venv, ["hooks", "list"])
        got = res.get("json") or {}
        if not res["ok"]:
            return {"ok": False, "error": res.get("error") or "couldn't read the hooks"}
        return {"ok": True, **got}

    def webhook_deliveries(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            host, venv = self._wh_target(payload)
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        argv = [
            "hooks",
            "deliveries",
            "--limit",
            str(max(1, min(int(payload.get("limit") or 20), 200))),
        ]
        if payload.get("hook"):
            argv += ["--hook", str(payload["hook"])]
        res = self._webhook_call(host, venv, argv)
        got = res.get("json") or {}
        return {
            "ok": res["ok"],
            "error": res.get("error") or "",
            "deliveries": got.get("deliveries") or [],
        }

    def webhook_hook_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add or replace a console-made hook (``hook`` = its table, or ``text`` =
        TOML). A new token or password is made here, sent to the host on stdin
        (it keeps only the hash) and comes back once in ``secrets``."""
        try:
            host, venv = self._wh_target(payload)
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            hook = wh.hook_table(payload)
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        if not str(hook.get("name") or "").strip():
            return {"ok": False, "error": "The hook needs a name."}
        try:
            hook, made = wh.new_secrets(
                hook,
                token=bool(payload.get("new_token")),
                password=bool(payload.get("new_password")),
            )
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        res = self._webhook_call(
            host, venv, wh.save_argv(), data=json.dumps(hook).encode("utf-8")
        )
        got = res.get("json") or {}
        ok = bool(res["ok"]) and bool(got.get("ok", True))
        self._webhook_audit(
            "webhook.hook_saved",
            payload,
            id=hook.get("name"),
            ok=ok,
            new_secret=sorted(made),
        )
        out: dict[str, Any] = {
            "ok": ok,
            "error": res.get("error") or got.get("error") or "",
            **{k: got.get(k) for k in ("hook", "url", "replaced") if k in got},
        }
        if ok and made:
            out["secrets"] = made
            out["note"] = (
                "Shown once: only a hash is stored. Put it in the sending app now."
            )
        return out

    def webhook_hook_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            host, venv = self._wh_target(payload)
            argv = wh.remove_argv(str(payload.get("name") or ""))
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        res = self._webhook_call(host, venv, argv)
        got = res.get("json") or {}
        ok = bool(res["ok"]) and bool(got.get("ok", True))
        self._webhook_audit(
            "webhook.hook_removed", payload, id=payload.get("name"), ok=ok
        )
        return {"ok": ok, "error": res.get("error") or got.get("error") or ""}

    def webhook_hook_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Dry run of a hook as edited (saved or not) against ``body`` or, with
        no body, the last one that hook received. Nothing runs."""
        try:
            host, venv = self._wh_target(payload)
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            hook = wh.hook_table(payload)
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        body = payload.get("body")
        draft = {
            "hook": hook,
            "body": body if isinstance(body, str) and body.strip() else None,
            "content_type": str(payload.get("content_type") or ""),
        }
        res = self._webhook_call(
            host,
            venv,
            ["hooks", "test", "--draft"],
            data=json.dumps(draft).encode("utf-8"),
        )
        got = res.get("json") or {}
        if not got:
            return {"ok": False, "error": res.get("error") or "the test didn't answer"}
        return {
            "ok": bool(got.get("ok")),
            "error": got.get("error") or "",
            "source": got.get("source"),
            "items": got.get("items") or [],
        }

    def webhook_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Change ``[hooks]`` (``hooks``) and ``[smtp]`` (``smtp``) settings in a
        venv; the daemon picks them up by itself."""
        try:
            host, venv = self._wh_target(payload)
            argv = wh.settings_argv(payload.get("hooks"), payload.get("smtp"))
        except wh.WebhookError as exc:
            return {"ok": False, "error": str(exc)}
        res = self._webhook_call(host, venv, argv)
        got = res.get("json") or {}
        ok = bool(res["ok"]) and bool(got.get("ok", True))
        self._webhook_audit(
            "webhook.settings",
            payload,
            ok=ok,
            hooks=sorted((payload.get("hooks") or {}).keys()),
            smtp=sorted((payload.get("smtp") or {}).keys()),
        )
        return {
            "ok": ok,
            "error": res.get("error") or got.get("error") or "",
            "settings": got.get("settings"),
            "smtp_settings": got.get("smtp_settings"),
        }
