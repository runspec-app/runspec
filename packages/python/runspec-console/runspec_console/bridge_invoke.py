"""bridge_invoke.py — InvokeMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


from . import bridge_utils as utils_mod
from . import config as config_mod
from . import executor as executor_mod
from . import hosts as hosts_mod
from . import observability
from .config import (
    write_docs_dir,
)

from .bridge_const import (
    logger,
    AI_OFF_MESSAGE,
    _CANCEL_KEY,
    _OPERATOR_RUN_OUTPUT_CAP,
    _UI_OUTPUT_LINE_CAP,
    _UI_OUTPUT_BYTE_CAP,
)
from .bridge_turn import TurnSpec
from .bridge_utils import (
    _iso_now,
)
from .session import ChatSession, new_interactive

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class InvokeMixin:
    def _become_params(
        self, cached: dict[str, Any] | None, hostname: str
    ) -> tuple[str, str, str | None]:
        """Resolve run_as + become method/flags for a runnable on a host.

        ``run_as`` is the runnable's own (its raw spec, carried through
        discovery as ``rawSpec``), else its package's ``[config] run_as``
        (``configRunAs``), else ``"@owner"`` — resolved for this host. ``"@owner"``
        becomes the venv root's owner found at discovery (``venvOwner``; ``""``
        when root-owned, ``None`` when unknown ⇒ no escalation). Escalation is
        dropped when the target is the account the console logs in as
        (``loginUser``). Returns ("", ...) when no privilege escalation applies."""
        from runspec.become import OWNER, effective_run_as, resolve_run_as

        rec = cached or {}
        raw = rec.get("rawSpec") or {}
        spec = effective_run_as(raw.get("run_as"), rec.get("configRunAs"))
        run_as = resolve_run_as(spec, hostname)
        if run_as == OWNER:
            run_as = str(rec.get("venvOwner") or "")
        if run_as and run_as == rec.get("loginUser"):
            run_as = ""
        return run_as, str(raw.get("become_method") or "sudo"), raw.get("become_flags")

    def _ui_output_caps(self) -> tuple[int, int]:
        """(line_cap, byte_cap) for a *direct* Console run's streamed output,
        from ``[console] output_line_cap`` / ``output_byte_cap`` (defaults in
        bridge_const). A value of 0 disables that limit. Past the cap the bridge
        stops dispatching ``runspec:output`` and emits one sentinel line — a
        UI-flood guard so a huge dump can't hang the card. The runnable's own
        audit log is unaffected."""
        console = self.get_config().get("console", {})

        def _read(key: str, default: int) -> int:
            try:
                v = int(console.get(key, default))
            except (TypeError, ValueError):
                return default
            return v if v >= 0 else default

        return (
            _read("output_line_cap", _UI_OUTPUT_LINE_CAP),
            _read("output_byte_cap", _UI_OUTPUT_BYTE_CAP),
        )

    @staticmethod
    def _positionals_for(
        cached: dict[str, Any] | None, command_path: list[str]
    ) -> dict[str, int]:
        """{arg_name: position} for the node at ``command_path``.

        Positional args (those with a ``position`` in the spec) must be sent as
        bare values, not ``--name`` flags — the executor uses this to place them
        correctly. Resolves the leaf node so a subcommand's positionals are found
        too. Empty when the runnable isn't cached (falls back to all-flags)."""
        node = cached or {}
        for seg in command_path:
            node = (node.get("commands") or {}).get(seg) or {}
        return {
            a["name"]: a["position"]
            for a in node.get("args", [])
            if isinstance(a, dict) and a.get("position") is not None
        }

    @staticmethod
    def _secret_arg_names(
        cached: dict[str, Any] | None, command_path: list[str]
    ) -> set[str]:
        """Names of ``password``-typed args for the node at ``command_path``.

        Secrets must never become CLI flags — the bridge pulls them out of the
        argv args and hands them to the executor as env vars instead. Resolves
        the leaf node so a subcommand's secret args are found too."""
        node = cached or {}
        for seg in command_path:
            node = (node.get("commands") or {}).get(seg) or {}
        return {
            a["name"]
            for a in node.get("args", [])
            if isinstance(a, dict) and a.get("type") == "password"
        }

    def invoke_runnable(
        self,
        host: str,
        runnable: str,
        args: dict[str, Any],
        command_path: list[str] | None = None,
        group: str | None = None,
        credential_ids: list[str] | None = None,
        operator: str | None = None,
        trigger: str = "",
        inv_id: str | None = None,
        on_progress: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> str:
        # Callers may pre-allocate the id (the scheduled-run path uses it as the
        # run card's card_id so output/run_end route to the card with no
        # correlation step); default mints a fresh one as before. on_progress, when
        # set, mirrors the per-line + completion lifecycle to the caller so it can
        # build a run card without the dispatch path having to know about cards.
        inv_id = inv_id or uuid.uuid4().hex[:12]
        cp = command_path or []
        entry = self._host_entry(host)
        cancel_event = threading.Event()

        entry_paths = utils_mod._paths(entry) if entry else []
        # Look up the runnable's discovery record — needed for its venv (group)
        # and its run_as / become_method (privilege escalation).
        with self._lock:
            cached = next(
                (
                    r
                    for r in self._runnables_cache
                    if r.get("host") == host
                    and r.get("name") == runnable
                    and (group is None or r.get("group") == group)
                ),
                None,
            )
        if group is None and cached:
            group = cached.get("group")
        rp = next(
            (p for p in entry_paths if hosts_mod.venv_name(p) == group),
            entry_paths[0] if entry_paths else "",
        )

        run_as, become_method, become_flags = self._become_params(cached, host)

        # Split secret (password-typed) values out of the argv args: they are
        # injected as RUNSPEC_*_ARG_* env vars by the executor, never passed on
        # the command line. The stored/surfaced args record keeps only the
        # non-secret values so a secret never reaches the UI, history, or logs.
        # NOTE: on the human/form path credentials are *never* copied into args —
        # a required arg must be typed by the operator. The only way the form
        # supplies a credential is the name-derived env-var channel below.
        secret_names = self._secret_arg_names(cached, cp)
        secrets = {
            k: v for k, v in args.items() if k in secret_names and v not in (None, "")
        }
        exec_args = {k: v for k, v in args.items() if k not in secret_names}
        # The run dialog's "Debug logging" switch (or any caller's `debug`) rides
        # the env channel, never argv — see executor.DEBUG_ARG.
        dbg_env = utils_mod.pop_debug(cached, cp, exec_args)

        # Inject the operator-selected credentials as plain, name-derived env vars
        # (e.g. JIRA_USERNAME/JIRA_PASSWORD). Secrets are read from the keyring here
        # and never reach the surfaced args record. Returns the ids applied, for
        # the audit record.
        cred_env, applied_creds = self._credential_env(host, runnable, credential_ids)
        cred_env.update(dbg_env)
        # Also honour the selected credentials' *arg-bindings* (username_arg →
        # argv, secret_arg → the env channel), so a runnable that reads its
        # credential from args — not bare env vars — works on the Forms run path
        # exactly as it does for the agent. Driven only by the operator's explicit
        # selection (no scope auto-apply); operator-typed values win. The run
        # dialog pre-fills/labels these args so the fill stays visible.
        applied_binding = self._apply_selected_credentials(
            runnable,
            exec_args,
            secrets,
            list(credential_ids or []),
            valid_args=self._declared_arg_names(cached, cp),
        )
        if applied_binding:
            applied_creds = sorted(set(applied_creds) | set(applied_binding))

        with self._lock:
            self._in_flight[inv_id] = {
                "id": inv_id,
                "runnable": runnable,
                "group": hosts_mod.venv_name(rp) if rp else "",
                "host": host,
                "operator": operator or self._current_user(),
                "runAs": run_as,
                "startedAt": _iso_now(),
                "args": exec_args,
                **({"trigger": trigger} if trigger else {}),
                **({"credentials": applied_creds} if applied_creds else {}),
                _CANCEL_KEY: cancel_event,
            }

        line_cap, byte_cap = self._ui_output_caps()

        def run() -> None:
            # Backstop against a runnable that dumps a huge blob (e.g. 10k lines of
            # pretty-printed JSON): one dispatch per line otherwise floods the
            # WebView and hangs the card. Past the line/byte cap we stop
            # dispatching and emit a single sentinel; `capped` latches so the
            # sentinel is emitted exactly once. Counters live in a dict so the
            # closure can mutate them without `nonlocal` gymnastics.
            counts = {"lines": 0, "bytes": 0, "capped": False}
            # Capture output (bounded) so an operator-initiated run can be saved
            # to chat history for the operator to review on reopen. Independent of
            # the UI-flood cap above — we keep collecting up to the storage cap
            # even after the console stops rendering inline.
            captured: dict[str, Any] = {"parts": [], "chars": 0}

            def on_line(line: str, stream: str) -> None:
                if captured["chars"] < _OPERATOR_RUN_OUTPUT_CAP:
                    captured["parts"].append(line)
                    captured["chars"] += len(line) + 1
                if counts["capped"]:
                    return
                counts["lines"] += 1
                counts["bytes"] += len(line)
                over_lines = line_cap and counts["lines"] > line_cap
                over_bytes = byte_cap and counts["bytes"] > byte_cap
                if over_lines or over_bytes:
                    counts["capped"] = True
                    reason = (
                        f"{line_cap:,} lines" if over_lines else f"{byte_cap:,} bytes"
                    )
                    sentinel = (
                        f"⚠  Output truncated in the console at {reason} — this "
                        "runnable returned more than the console renders inline. "
                        "The full result is in its audit log; a summary/filtered "
                        "runnable keeps the card responsive."
                    )
                    self._dispatch(
                        "runspec:output",
                        {"id": inv_id, "line": sentinel, "stream": "stderr"},
                    )
                    if on_progress is not None:
                        on_progress("line", {"line": sentinel, "stream": "stderr"})
                    return
                self._dispatch(
                    "runspec:output", {"id": inv_id, "line": line, "stream": stream}
                )
                if on_progress is not None:
                    on_progress("line", {"line": line, "stream": stream})

            def on_done(exit_code: int, duration_ms: int) -> None:
                with self._lock:
                    self._in_flight.pop(inv_id, None)
                # A run just finished on this host → its run_summary is now on
                # disk, so drop the cached remote-history read for the host and
                # the History tab's run_end refetch re-streams fresh (local isn't
                # cached at this layer, so it's already live). getattr-guarded so
                # the invoke path tolerates a stub bridge without the reporting mixin.
                invalidate = getattr(self, "_invalidate_remote_history", None)
                if callable(invalidate):
                    invalidate(host)
                self._dispatch(
                    "runspec:run_end",
                    {"id": inv_id, "exit_code": exit_code, "duration_ms": duration_ms},
                )
                # OTLP tool-run metric for this runnable execution (no-op unless
                # [telemetry.metrics] is on). This is the human/automation path —
                # Forms, MCP server, schedules, rule triggers — which is disjoint
                # from the agent tool gate (agent run_runnable executes via
                # executor.run_remote, built-ins never reach here), so a run is
                # counted exactly once. `trigger` names the source (mcp / schedule /
                # a trigger name); blank ⇒ an operator Forms/command-bar run.
                observability.record_tool_run(
                    tool=runnable,
                    provenance={"trigger": trigger, "agent": False},
                    duration_ms=duration_ms,
                    host=host,
                    status=(
                        "ok"
                        if exit_code == 0
                        else ("cancelled" if exit_code == 130 else "error")
                    ),
                )
                # Audio cue for the run outcome (best-effort; muted unless enabled).
                self._play_sound(
                    "completion_success" if exit_code == 0 else "completion_error"
                )
                # Save operator-initiated manual runs (Forms tab / command bar) to
                # the active chat conversation for the operator's own review. Skip
                # the scheduled/trigger path (it sets ``trigger``/``on_progress``);
                # the agent's own runnable tool never routes through here.
                if not trigger and on_progress is None:
                    self._persist_operator_run(
                        runnable=runnable,
                        host=host,
                        args=exec_args,
                        output="\n".join(captured["parts"]),
                        exit_code=exit_code,
                        duration_ms=duration_ms,
                        operator=operator or self._current_user(),
                    )
                if on_progress is not None:
                    on_progress(
                        "done", {"exit_code": exit_code, "duration_ms": duration_ms}
                    )

            if entry is None or not rp:
                on_line(f"✗  Host '{host}' not found in runspec_hosts.toml", "stderr")
                on_done(-1, 0)
                return

            # A refused escalation ends with a plain-English note, not just
            # sudo's "a terminal is required … askpass helper".
            on_line, on_done = utils_mod.watch_escalation(
                on_line,
                on_done,
                run_as,
                str((cached or {}).get("loginUser") or ""),
                host,
                become_method,
            )

            ssh = entry.get("ssh")
            jump = utils_mod.host_jump(getattr(self, "_hosts", None), entry)
            if ssh:
                # Stream over a pooled transport when pooling is on — the
                # invocation reuses the host's warm connection (one more channel,
                # no new handshake) instead of opening a fresh client. A ``jump``
                # host uses the system-ssh transport instead (not pooled), so
                # there's no session_factory on that path.
                pool = self._ssh_pool
                session_factory = None
                if pool is not None and not jump:
                    cfg = self.get_config().get("ssh")
                    idf = entry.get("identityFile")

                    def session_factory() -> tuple[Any, Any]:
                        return pool.open_session(
                            ssh, identity_file=idf, global_ssh_config=cfg
                        )

                executor_mod.run_remote(
                    ssh,
                    rp,
                    runnable,
                    exec_args,
                    cp,
                    on_line,
                    on_done,
                    entry.get("identityFile"),
                    cancel_event=cancel_event,
                    global_ssh_config=self.get_config().get("ssh"),
                    run_as=run_as,
                    become_method=become_method,
                    become_flags=become_flags,
                    session_factory=session_factory,
                    positionals=self._positionals_for(cached, cp),
                    secrets=secrets,
                    cred_env=cred_env,
                    trigger=trigger,
                    jump=jump,
                )
            else:
                executor_mod.run_local(
                    rp,
                    runnable,
                    exec_args,
                    cp,
                    on_line,
                    on_done,
                    cancel_event=cancel_event,
                    run_as=run_as,
                    become_method=become_method,
                    become_flags=become_flags,
                    positionals=self._positionals_for(cached, cp),
                    secrets=secrets,
                    cred_env=cred_env,
                    trigger=trigger,
                )

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return inv_id

    def get_in_flight(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {k: v for k, v in inv.items() if k != _CANCEL_KEY}
                for inv in self._in_flight.values()
            ]

    def cancel_invocation(self, inv_id: str) -> None:
        """Signal a running invocation to stop. Best-effort: kill the subprocess."""
        with self._lock:
            inv = self._in_flight.get(inv_id)
        if inv is None:
            return
        ev: threading.Event | None = inv.get(_CANCEL_KEY)
        if ev is not None:
            ev.set()

    # ── chat ──────────────────────────────────────────────────────────────────

    def send_chat(
        self, message: str, invocation_id: str | None = None, session_id: str = ""
    ) -> str:
        """Send a message to an interactive chat session (the current one when
        ``session_id`` is empty) and return the turn's chat id. Sessions run
        concurrently — a turn in flight on another session is untouched."""
        return self._spawn_chat_turn(message, session_id=session_id)

    def _spawn_chat_turn(
        self, message: str, runbook: str = "", session_id: str = "", model: str = ""
    ) -> str:
        """Start an agentic chat turn on a worker thread; return its chat id.

        ``runbook`` (non-empty only on the run_runbook path) tags the turn's run
        provenance so every runnable the agent invokes during it is attributed to
        the runbook in History/Analytics — direct chat leaves it unset (no origin).
        ``model`` (a runbook's own model) runs this one turn on that model.
        ``session_id`` picks the interactive session (default: the current one);
        an automation session is read-only and refused with a visible error."""
        chat_id = uuid.uuid4().hex[:12]
        cancel = threading.Event()
        with self._lock:
            self._chat_cancels[chat_id] = cancel
        session = self._resolve_chat_session(session_id)

        def run() -> None:
            try:
                if session is None:
                    self._dispatch(
                        "runspec:token",
                        {
                            "id": chat_id,
                            "token": "⚠ That session is read-only (an automation "
                            "run) — switch to a chat session to talk.",
                        },
                    )
                    self._dispatch(
                        "runspec:run_end",
                        {"id": chat_id, "exit_code": 1, "duration_ms": 0},
                    )
                    return
                try:
                    # Fold the current local date, a lean presence caveat and the
                    # pinned-memory header into this turn's system prompt via the
                    # one-off system_override path, so they stay fresh without
                    # invalidating the cached adapter. The date is always present,
                    # so the override path is always taken — but it's date-only, so
                    # the prompt stays byte-identical across a day's turns and the
                    # model-side prompt cache still hits. The presence caveat is
                    # empty on the common turn (operator present, inside working
                    # hours) and only appears when interacting outside hours — the
                    # full rota + automation inventory now lives behind the
                    # on-demand console_status tool, not on every turn.
                    now_block = self._now_context()
                    console_block = self._presence_caveat()
                    memory_block = self._knowledge_header()
                    base_block = self._base_system()
                    # Standing workspace guidance (how to read a spilled result
                    # back, when to set `deliver`) — static text, cache-stable;
                    # rides every turn because the workspace_* tools do too.
                    workspace_block = self._workspace_guidance()
                    # Search-mode catalogue index (runnables menu + deferred
                    # built-ins menu). Byte-stable across a day's turns, so it now
                    # rides the per-turn system_override path as a TRAILING section
                    # — after base/date/presence/memory — instead of being folded
                    # into the first user message. A gateway that marks only the
                    # system block as cacheable then actually caches it: in search
                    # mode the tools block is tiny, so a system-only cache marker
                    # used to fall under the model's minimum cacheable prefix and
                    # silently cache nothing. Empty outside search mode.
                    index_text = self._chat_index_text()
                    extra = "\n\n".join(
                        part
                        for part in (
                            now_block,
                            workspace_block,
                            console_block,
                            memory_block,
                        )
                        if part
                    )
                    catalog_section = self._catalog_system_section(index_text)
                    system_body = base_block
                    if extra:
                        system_body += "\n\n" + extra
                    if catalog_section:
                        system_body += "\n\n" + catalog_section
                    adapter = (
                        self._get_adapter(
                            system_override=system_body, **utils_mod.model_kw(model)
                        )
                        if (extra or catalog_section)
                        else self._get_adapter(**utils_mod.model_kw(model))
                    )
                    # Char counts of the system-prompt buckets, for the context
                    # indicator's composition breakdown. The date block is tiny
                    # (~250 chars) so it folds into "system" rather than earning
                    # its own near-zero slice. The "console_context" bucket now
                    # measures the lean presence caveat (usually 0). The catalogue
                    # index keeps its own "catalog" bucket even though it now lives
                    # inside the system prompt string, so the gauge still shows it
                    # separately from "system". Computed here where the blocks
                    # already exist — no recomputation in the turn loop.
                    system_parts = {
                        "system": len(base_block)
                        + len(now_block)
                        + len(workspace_block),
                        "console_context": len(console_block),
                        "memory": len(memory_block),
                    }
                except Exception as exc:
                    self._dispatch(
                        "runspec:token", {"id": chat_id, "token": f"⚠ {exc}"}
                    )
                    self._dispatch(
                        "runspec:run_end",
                        {"id": chat_id, "exit_code": 1, "duration_ms": 0},
                    )
                    return
                if adapter is None:
                    no_model = (
                        AI_OFF_MESSAGE
                        if not getattr(self, "_ai_enabled", True)
                        else "No model provider configured. Set provider and API key in Settings."
                    )
                    self._dispatch(
                        "runspec:token",
                        {"id": chat_id, "token": f"⚠ {no_model}"},
                    )
                    self._dispatch(
                        "runspec:run_end",
                        {"id": chat_id, "exit_code": 1, "duration_ms": 0},
                    )
                    return
                if runbook:
                    self._set_run_origin(
                        chat_id, trigger="", agent=True, runbook=runbook
                    )
                try:
                    spec = TurnSpec(
                        chat_id=chat_id,
                        session=session,
                        message=message,
                        adapter=adapter,
                        tools=self._runnables_to_tools(),
                        kind="chat",
                        interactive=True,
                        cancel=cancel,
                        system_parts=system_parts,
                        index_text=index_text,
                    )
                    asyncio.run(self._run_agent_turn(spec))
                finally:
                    if runbook:
                        self._clear_run_origin(chat_id)
            finally:
                with self._lock:
                    self._chat_cancels.pop(chat_id, None)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return chat_id

    # ── runbooks (user-invocable, parameterized runbooks) ───────────────────────

    def get_runbooks(self) -> list[dict[str, Any]]:
        """All runbooks with their ``[runbook.args]`` table parsed to the same ``ArgDef``
        shape runnable forms use — feeds the `$` runbooks menu, the run modal, and the
        Settings editor. ``steps`` is the ordered list of ``{text, title?}`` rows
        (normalised from the ``[[runbook.step]]`` array, or best-effort-split from a
        legacy string). ``argsRaw`` is the stored table for faithful editor round-trip."""
        from . import memory, runbookargs

        out: list[dict[str, Any]] = []
        for s in memory.load_runbooks():
            if not s.get("id"):
                continue
            raw = s.get("args") if isinstance(s.get("args"), dict) else {}
            entry: dict[str, Any] = {
                "id": s.get("id"),
                "title": s.get("title", ""),
                "symptom": s.get("symptom", ""),
                "steps": memory.normalize_runbook_steps(s),
                "tags": list(s.get("tags", []) or []),
                "discoverable": list(s.get("discoverable", []) or []),
                "model": str(s.get("model") or ""),
                "updated": s.get("updated", ""),
                "argsRaw": raw,
            }
            try:
                entry["args"] = runbookargs.parse_runbook_args(raw)
            except Exception as exc:
                # Malformed args table — surface it, don't break the whole list.
                entry["args"] = []
                entry["argsError"] = str(exc)
            out.append(entry)
        return out

    def save_runbooks(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Persist the whole runbooks collection from the Settings editor (mirrors
        save_jump_hosts). Validates each runbook's args table up front so a bad one is
        rejected before it lands in the synced file."""
        from . import memory, runbookargs

        normalised: list[dict[str, Any]] = []
        seen: set[str] = set()
        for r in rows or []:
            sid = str(r.get("id") or "").strip() or memory._slug(
                str(r.get("title") or "")
            )
            if not sid or sid in seen:
                continue
            seen.add(sid)
            raw = r.get("argsRaw")
            if not isinstance(raw, dict):
                raw = r.get("args") if isinstance(r.get("args"), dict) else None
            if raw:
                errors = runbookargs.validate_runbook_arg_table(raw)
                if errors:
                    return {
                        "ok": False,
                        "error": f"Runbook '{sid}': " + "; ".join(errors),
                    }
            steps = r.get("steps")
            step_errors = runbookargs.validate_runbook_steps(steps)
            if step_errors:
                return {
                    "ok": False,
                    "error": f"Runbook '{sid}': " + "; ".join(step_errors),
                }
            entry: dict[str, Any] = {
                "id": sid,
                "title": str(r.get("title") or "").strip(),
                "symptom": str(r.get("symptom") or "").strip(),
                "tags": [
                    str(t).strip() for t in (r.get("tags") or []) if str(t).strip()
                ],
                "updated": memory._now(),
            }
            # The editor sends an ordered list of ``{text, title?}`` rows → store the
            # readable ``[[runbook.step]]`` array; tolerate a legacy string too.
            if isinstance(steps, (list, tuple)):
                entry["step"] = memory._coerce_steps(steps)
            else:
                entry["steps"] = str(steps or "").strip()
            if raw:
                entry["args"] = dict(raw)
            # The publish audience (which queue catalogs offer this runbook); opt-in,
            # stored only when set so an ordinary runbook stays out of every catalog.
            aud = [
                str(a).strip() for a in (r.get("discoverable") or []) if str(a).strip()
            ]
            if aud:
                entry["discoverable"] = aud
            if str(r.get("model") or "").strip():
                entry["model"] = str(r["model"]).strip()
            normalised.append(entry)
        memory.save_runbooks(normalised)
        return {"ok": True, "count": len(normalised)}

    def save_runbook_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Upsert a **single** runbook from the Settings editor via read-modify-write
        (``memory.save_runbook`` re-reads the file, replaces this id, keeps the rest),
        instead of the whole-collection bulk write. This is the data-loss fix: the old
        bulk save rebuilt every *other* runbook from a stale in-memory snapshot, so an
        edit here clobbered any runbook changed on disk (or via Config Sync) since load —
        and silently dropped ``discoverable`` (which the editor doesn't carry). Here only
        this entry is touched, and ``discoverable`` is preserved from the stored entry
        unless the caller explicitly provides it (the editor never does)."""
        from . import memory, runbookargs

        sid = str(entry.get("id") or "").strip() or memory._slug(
            str(entry.get("title") or "")
        )
        if not sid:
            return {"ok": False, "error": "A runbook needs an id or a title."}
        raw = entry.get("argsRaw")
        if not isinstance(raw, dict):
            raw = entry.get("args") if isinstance(entry.get("args"), dict) else None
        # Accept the model's JSON type spellings (string→str, …) the same way the agent
        # save path does, then validate the declaration shape.
        raw = runbookargs.normalize_arg_types(raw) if raw else raw
        if raw:
            errors = runbookargs.validate_runbook_arg_table(raw)
            if errors:
                return {"ok": False, "error": f"Runbook '{sid}': " + "; ".join(errors)}
        steps = entry.get("steps")
        step_errors = runbookargs.validate_runbook_steps(steps)
        if step_errors:
            return {"ok": False, "error": f"Runbook '{sid}': " + "; ".join(step_errors)}
        # Carry forward publish audience unless explicitly supplied (editor omits it).
        if "discoverable" in entry:
            discoverable = entry.get("discoverable") or []
        else:
            existing = memory.load_runbook(sid) or {}
            discoverable = existing.get("discoverable") or []
        memory.save_runbook(
            sid,
            title=str(entry.get("title") or "").strip(),
            symptom=str(entry.get("symptom") or "").strip(),
            steps=steps if isinstance(steps, (list, tuple)) else str(steps or ""),
            tags=[str(t).strip() for t in (entry.get("tags") or []) if str(t).strip()],
            args=raw or None,
            discoverable=discoverable,
            # Present → set/clear the runbook's model; absent → keep the stored one.
            model=str(entry.get("model") or "") if "model" in entry else None,
        )
        return {"ok": True, "id": sid}

    def delete_runbook(self, runbook_id: str) -> dict[str, Any]:
        """Delete a single runbook by id via read-modify-write (the other runbooks,
        including any edited on disk since load, are left untouched). Pairs with
        ``save_runbook_entry`` so the editor never bulk-overwrites the file."""
        from . import memory

        removed = memory.delete_runbook(str(runbook_id or ""))
        return {"ok": True, "removed": removed}

    def run_runbook(
        self,
        runbook_id: str,
        values: dict[str, Any] | None = None,
        extra_instructions: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        """Render a runbook's body with the operator's form ``values`` (validated against
        its args), append any free-text ``extra_instructions``, and start a chat turn
        tagged with the runbook (in ``session_id``, default the current chat session).
        Returns ``{chat_id, message}`` — the rendered text the UI shows as the chat
        block — or ``{error}`` if the runbook is unknown / values invalid."""
        from . import memory, runbookargs

        if not getattr(self, "_ai_enabled", True):
            return {"error": AI_OFF_MESSAGE}
        runbook = memory.load_runbook(str(runbook_id or ""))
        if not runbook:
            return {"error": f"No runbook with id '{runbook_id}'."}
        vals = dict(values or {})
        raw = runbook.get("args") if isinstance(runbook.get("args"), dict) else {}
        errors = runbookargs.validate_runbook_values(raw, vals)
        if errors:
            return {"error": "\n".join(errors)}
        body = memory.render_runbook(runbook, vals)
        extra = str(extra_instructions or "").strip()
        message = f"{body}\n\n{extra}".strip() if extra else body.strip()
        if not message:
            return {"error": "This runbook has an empty body."}
        chat_id = self._spawn_chat_turn(
            message,
            runbook=str(runbook.get("id") or ""),
            session_id=session_id,
            model=str(runbook.get("model") or ""),
        )
        return {"chat_id": chat_id, "message": message}

    # ── runbook docs (deterministic wiki from runbooks + hosts) ────────────────────

    def get_docs_config(self) -> dict[str, Any]:
        """The runbook-docs settings for the UI: the configured output ``dir`` and
        the ``default`` used when it's blank (``config_mod.app_dir()/docs``)."""
        return {
            "dir": config_mod.read_docs_dir(),
            "default": str(config_mod.app_dir() / "docs"),
        }

    def save_docs_config(self, dest_dir: str) -> None:
        """Persist the runbook-docs output directory (blank clears it)."""
        write_docs_dir(str(dest_dir or ""))

    def generate_docs(self, dest_dir: str = "") -> dict[str, Any]:
        """Write the deterministic runbook wiki (README + per-runbook + hosts pages)
        from the local runbooks + fleet into ``dest_dir``.

        Resolution: the ``dest_dir`` arg → the saved ``[docs] dir`` → the default
        ``config_mod.app_dir()/docs``. Output is byte-stable (no timestamps), so re-running
        only changes files whose runbooks changed — clean to commit back to the
        config repo. Returns ``{ok, dir, written}`` or ``{ok: False, error}``."""

        from . import docsgen, memory

        target = str(dest_dir or "").strip() or config_mod.read_docs_dir()
        base = Path(target).expanduser() if target else (config_mod.app_dir() / "docs")
        try:
            pages = docsgen.render_wiki(
                memory.load_runbooks(),
                list(self._hosts),
                self.get_runnables("all"),
            )
            written: list[str] = []
            for rel, text in pages.items():
                dest = base / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                self._atomic_write_bytes(dest, text.encode("utf-8"))
                written.append(rel)
            return {"ok": True, "dir": str(base), "written": sorted(written)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_docs failed", exc_info=True)
            return {"ok": False, "error": f"could not generate docs: {exc}"}

    def cancel_chat(self, chat_id: str) -> None:
        """Signal an in-flight chat turn to stop. It halts at the next safe
        checkpoint — no further model calls and no further tool runs — and any
        built-in tool this turn is currently running (e.g. a repo clone/grep or a
        denylist fan-out) is signalled to abort cooperatively. A *runnable*
        already executing (a remote command) is still left to finish."""
        with self._lock:
            event = self._chat_cancels.get(chat_id)
        if event is not None:
            logger.info("chat %s: cancel requested", chat_id)
            event.set()
        # Stop any built-in tool this turn is running mid-call: its in-flight
        # record carries this chat_id and its own cancel Event. Without this the
        # chat Stop button only took effect *between* tool calls, so a long
        # repo_sync/repo_grep looked unstoppable until it finished.
        with self._lock:
            tool_events = [
                ev
                for inv in self._in_flight.values()
                if inv.get("chatId") == chat_id
                and (ev := inv.get(_CANCEL_KEY)) is not None
            ]
        for ev in tool_events:
            ev.set()
        # Wake any confirmation prompt waiting on this turn — otherwise Stop does
        # nothing while the Approve/Deny dialog is up (the gate is parked in
        # event.wait, which the cancel flag alone never interrupts). Mark them
        # cancelled so the gate refuses the tool rather than running it.
        with self._lock:
            pending = [
                (confirm_event, holder)
                for cid, confirm_event, holder in self._pending_confirms.values()
                if cid == chat_id
            ]
        for confirm_event, holder in pending:
            holder["cancelled"] = True
            confirm_event.set()

    def clear_chat(self, session_id: str = "") -> dict[str, Any]:
        """Reset a chat session in place: the session (the current one, or
        ``session_id``) is replaced by a fresh one under the same chip slot —
        the model's memory of prior turns is gone and the next turn opens a new
        conversation file. A turn still in flight on it is stopped first (the
        operator asked for a clean slate), never waited on.

        The Console's "New chat" no longer calls this — it opens an *additional*
        session (``new_chat_session``) and leaves the current one running; this
        remains the AI-off "Clear" and the explicit reset."""
        old = self._resolve_chat_session(session_id)
        if old is None:
            return {"ok": False, "error": "unknown or read-only session"}
        if old.current_turn:
            self.cancel_chat(old.current_turn)
        fresh = new_interactive()
        with self._lock:
            self._sessions.pop(old.id, None)
            self._sessions[fresh.id] = fresh
            if self._chat_session_id == old.id:
                self._chat_session_id = fresh.id
            old.state = "closed"
            old.current_turn = ""
        self._emit_session(old)
        self._emit_session(fresh)
        # Reset the context-window gauge — the new conversation starts empty.
        self._dispatch(
            "runspec:chat_cleared", {"session": fresh.id, "replaced": old.id}
        )
        return {"ok": True, "session": fresh.to_dict(), "replaced": old.id}

    def compact_chat(self, session_id: str = "") -> dict[str, Any]:
        """Condense the *live* rolling chat history in place: drop the older
        prefix, fold a note summarising it into the first kept user message, and
        keep the recent tail verbatim. Same safe transformation as resume-time
        compaction (``resume_conversation``), but on the in-session history — for
        shrinking a long chat's context without losing the thread or starting
        over. Refuses (rather than blocks) while a turn is in flight on that
        session.

        The cut lands on a plain user message so tool_use/tool_result pairs and
        user/assistant alternation stay intact (the API requires both). Returns
        ``{ok, compacted}`` — ``compacted`` is the number of messages dropped, 0
        when there's nothing to safely compact."""
        session = self._resolve_chat_session(session_id)
        if session is None:
            return {"ok": False, "compacted": 0, "error": "unknown session"}
        cfg = config_mod.read_chat_history()
        keep_recent = max(0, int(cfg.get("resume_keep_recent", 20) or 0))
        dropped_n = 0
        dropped_turns: set[str] = set()
        if not session.lock.acquire(blocking=False):
            return {"ok": False, "compacted": 0, "error": "a turn is in flight"}
        try:
            # Compaction is the commit point for the current in/out state: turns
            # the operator excluded were already out of context, so physically
            # drop them here rather than fold them into the summary note (which
            # would re-inject removed content). Their cards get retired below.
            if session.excluded:
                excluded = set(session.excluded)
                if session.drop_turns(excluded):
                    dropped_turns |= excluded
                session.excluded.clear()
            hist = session.history
            tags = session.turn_ids
            # Need a meaningful prefix to drop beyond the kept tail.
            if len(hist) <= keep_recent + 2:
                return self._finish_compact(dropped_turns, session=session)
            cut = len(hist) - keep_recent
            while cut < len(hist) and not (
                hist[cut].get("role") == "user"
                and isinstance(hist[cut].get("content"), str)
            ):
                cut += 1
            if cut <= 0 or cut >= len(hist):
                # no clean boundary — leave it (excluded drops above still count)
                return self._finish_compact(dropped_turns, session=session)
            dropped, kept = hist[:cut], hist[cut:]
            note = self._compact_note(dropped, cfg)
            kept[0]["content"] = note + "\n\n" + kept[0]["content"]
            dropped_turns |= {t for t in tags[:cut] if t}
            session.replace_history(kept, tags[cut:])
            dropped_n = len(dropped)
        finally:
            session.lock.release()
        return self._finish_compact(dropped_turns, dropped_n, session=session)

    def _finish_compact(
        self,
        dropped_turns: set[str],
        dropped_n: int = 0,
        session: ChatSession | None = None,
    ) -> dict[str, Any]:
        """Announce a compaction (or excluded-turn drop) and return its result.
        ``dropped_turns`` are chat_ids whose messages are now gone — the frontend
        retires those cards' "in context" checkboxes (re-include is impossible
        once the messages are compacted away) and refreshes the context gauge.
        Stays silent on a pure no-op (nothing dropped, nothing excluded) so an
        idle 'Compact now' doesn't emit a spurious event."""
        if dropped_n or dropped_turns:
            self._dispatch(
                "runspec:chat_compacted",
                {
                    "dropped": dropped_n,
                    "dropped_turns": sorted(dropped_turns),
                    "session": session.id if session is not None else "",
                },
            )
        return {"ok": True, "compacted": dropped_n}

    def _compact_note(self, dropped: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
        """A note standing in for the dropped prefix on live compaction — an LLM
        summary when ``[chat_history] compact_on_resume`` is on (best-effort), else
        a no-model list of what those turns covered."""
        turns: list[dict[str, Any]] = []
        for m in dropped:
            role, content = m.get("role"), m.get("content")
            if not isinstance(content, str) or not content.strip():
                continue  # tool blocks aren't part of the text summary
            if role == "user":
                turns.append({"user": content, "assistant": ""})
            elif role == "assistant" and turns:
                turns[-1]["assistant"] = content
        if cfg.get("compact_on_resume"):
            body = self._summarize_middle_turns(turns)
            if body:
                return f"[Summary of {len(turns)} earlier turns (compacted): {body}]"
        qs = [
            t["user"].strip().replace("\n", " ")[:80]
            for t in turns
            if t.get("user", "").strip()
        ]
        if not qs:
            return "[Context: earlier turns were compacted to free up context.]"
        preview = "; ".join(qs[:5])
        extra = "" if len(qs) <= 5 else f" (+{len(qs) - 5} more)"
        return (
            f"[Context: {len(qs)} earlier turns were compacted to free up "
            f"context. They covered: {preview}{extra}.]"
        )

    # ── saved conversations (chat history) ──────────────────────────────────────

    def _persist_conversation_turn(
        self,
        session: ChatSession,
        user: str,
        assistant: str,
        tools: list[dict[str, Any]],
        usage: dict[str, int],
        model: str,
        reply_thinking: dict[str, Any] | None = None,
    ) -> None:
        """Append a completed chat exchange to the session's saved conversation
        (its id — an interactive session *is* its conversation).

        Best-effort and gated on ``[chat_history] enabled``; a failure here never
        breaks the chat turn. Prunes to the retention cap after writing."""
        try:
            cfg = config_mod.read_chat_history()
            if not cfg.get("enabled", True) or not session.conversation_id:
                return
            from . import conversations as conv

            turn = {
                "user": user,
                "assistant": assistant,
                "tools": tools,
                "usage": {k: int(v) for k, v in (usage or {}).items()} or None,
                "model": model,
                "ts": _iso_now(),
            }
            # The reasoning summary behind the final reply (tool calls carry
            # their own on each tools[] entry). Display-only: never replayed.
            if reply_thinking:
                turn["reply_thinking"] = reply_thinking
            directory = config_mod.conversations_dir()
            provider = str(self.get_config().get("llm", {}).get("provider", ""))
            with self._conv_store_lock:
                conv.append_turn(
                    directory,
                    session.conversation_id,
                    turn,
                    provider=provider,
                    model=model,
                )
            conv.prune(
                directory,
                max_conversations=int(cfg.get("max_conversations", 0) or 0),
                max_age_days=int(cfg.get("max_age_days", 0) or 0),
                workspaces_root=config_mod.workspaces_root(),
            )
        except Exception:  # noqa: BLE001 — persistence is best-effort
            logger.warning("could not persist chat conversation turn", exc_info=True)

    def _persist_operator_run(
        self,
        *,
        runnable: str,
        host: str,
        args: dict[str, Any],
        output: str,
        exit_code: int,
        duration_ms: int,
        operator: str,
    ) -> None:
        """Append an operator-initiated manual runnable run to the current chat
        session's saved conversation so the operator can review its result on
        reopen.

        This is an **operator-only** record: it renders in the transcript but is
        **never replayed to the agent** (``resume_conversation`` rebuilds the model
        history from :func:`conversations.chat_turns` only). Opens a chat session
        when none is active (so a run with no prior chat is still captured). This
        runs off the invoke worker thread, so the store write is serialised
        against a concurrent turn's persist by ``_conv_store_lock`` (read-modify-
        write) — never by the session lock a running turn holds. Gated on
        ``[chat_history] enabled``; best-effort."""
        try:
            cfg = config_mod.read_chat_history()
            if not cfg.get("enabled", True):
                return
            from . import conversations as conv

            directory = config_mod.conversations_dir()
            record = {
                "kind": "operator_run",
                "runnable": runnable,
                "host": host,
                "args": args,
                "output": str(output)[:_OPERATOR_RUN_OUTPUT_CAP],
                "exit_code": int(exit_code),
                "duration_ms": int(duration_ms),
                "operator": operator,
                "ts": _iso_now(),
            }
            session = self._chat_session()
            with self._conv_store_lock:
                conv.append_turn(directory, session.conversation_id, record)
            conv.prune(
                directory,
                max_conversations=int(cfg.get("max_conversations", 0) or 0),
                max_age_days=int(cfg.get("max_age_days", 0) or 0),
                workspaces_root=config_mod.workspaces_root(),
            )
        except Exception:  # noqa: BLE001 — persistence is best-effort
            logger.warning("could not persist operator run", exc_info=True)

    def list_conversations(self) -> list[dict[str, Any]]:
        """Metadata for every saved chat conversation, newest first:
        ``{id, title, created, updated, turn_count, preview}``."""
        from . import conversations as conv

        try:
            return conv.list_meta(config_mod.conversations_dir())
        except Exception:  # noqa: BLE001
            logger.warning("could not list conversations", exc_info=True)
            return []

    def load_conversation(self, conversation_id: str) -> dict[str, Any]:
        """A saved conversation's metadata + provider-agnostic transcript (its
        ``turns``), for rendering it in the Console. ``{}`` when not found."""
        from . import conversations as conv

        return conv.load(config_mod.conversations_dir(), str(conversation_id)) or {}

    @staticmethod
    def _flatten_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Render saved transcript turns into a plain user/assistant message list
        (tool calls folded into the assistant text as short notes). Provider-
        agnostic — every adapter accepts plain text messages."""
        from . import conversations as conv

        out: list[dict[str, Any]] = []
        for turn in turns or []:
            # Operator-only records (manual runs, etc.) never reach the model.
            if not conv.is_chat_turn(turn):
                continue
            user = str(turn.get("user", "")).strip()
            if user:
                out.append({"role": "user", "content": user})
            parts: list[str] = []
            assistant = str(turn.get("assistant", "")).strip()
            if assistant:
                parts.append(assistant)
            for t in turn.get("tools", []) or []:
                # A denied gated call never ran — don't replay it as if it had,
                # but keep the operator's redirect (their decline reason).
                if str(t.get("operator_decision", "")) == "denied":
                    reason = str(t.get("decline_reason") or "").strip()
                    if reason:
                        parts.append(
                            f"[user declined {t.get('name', 'tool')}: {reason[:500]}]"
                        )
                    continue
                out_text = str(t.get("output", ""))[:500]
                parts.append(f"[ran {t.get('name', 'tool')} → {out_text}]")
            out.append(
                {"role": "assistant", "content": "\n\n".join(parts) or "(no response)"}
            )
        return out

    @staticmethod
    def _mechanical_resume_note(middle: list[dict[str, Any]]) -> str:
        """A no-model note standing in for the omitted middle turns on resume —
        lists what they were about (the operator's questions) so the agent keeps
        the thread without an LLM call. This is the always-on default (compaction
        path A)."""
        qs = [
            str(t.get("user", "")).strip().replace("\n", " ")[:80]
            for t in middle
            if str(t.get("user", "")).strip()
        ]
        preview = "; ".join(qs[:5])
        extra = "" if len(qs) <= 5 else f" (+{len(qs) - 5} more)"
        return (
            f"[Context: {len(middle)} earlier turns were condensed to keep this "
            f"resumed chat compact. They covered: {preview}{extra}.]"
        )

    def _summarize_middle_turns(self, middle: list[dict[str, Any]]) -> str:
        """Best-effort LLM summary of the omitted middle turns (compaction path B,
        off by default). Returns ``""`` on any failure or when no model provider is
        configured, so the caller falls back to the mechanical note — this never
        blocks or breaks a resume."""
        try:
            from . import conversations as conv

            adapter = self._utility_adapter()
            if adapter is None:
                return ""
            prompt = (
                "Summarize the earlier portion of this conversation in 3-5 "
                "sentences, preserving key facts, decisions, hostnames/ids, and "
                "anything the assistant needs to continue. Be concise.\n\n"
                + conv.turns_to_text(middle)
            )
            resp = asyncio.run(adapter.chat([{"role": "user", "content": prompt}], []))
            return (getattr(resp, "text", "") or "").strip()
        except Exception:  # noqa: BLE001 — summary is best-effort
            logger.warning(
                "resume summary failed; using the mechanical note", exc_info=True
            )
            return ""

    def resume_conversation(self, conversation_id: str) -> dict[str, Any]:
        """Make a saved conversation the active rolling chat: rebuild the model
        history from its transcript and point new turns at its file, so sending
        continues it.

        The history is flattened to plain user/assistant **text** (tool calls
        folded in as short notes), not exact provider-native tool_use/tool_result
        pairs — robust across providers (``exact`` is always ``False``).

        **Resume-time compaction:** a long conversation keeps its opening turn +
        the recent ``resume_keep_recent`` turns verbatim and condenses the middle
        into one note, so it doesn't blow the context window. The note is a
        no-model summary by default; with ``[chat_history] compact_on_resume`` it's
        an LLM summary (falling back to the note if that fails). The **saved
        transcript is never modified** — compaction only shapes the working
        history. Returns ``{ok, turns, kept, compacted, summarized, exact}``."""
        from . import conversations as conv

        rec = conv.load(config_mod.conversations_dir(), str(conversation_id))
        if not rec:
            return {"ok": False, "error": "conversation not found"}
        turns = rec.get("turns", []) or []
        # The model only ever sees chat exchanges — operator-only records
        # (manual runs, etc.) are rendered for the operator but must never enter
        # the replayed history, so filter them out before compaction/flatten.
        model_turns = conv.chat_turns(turns)
        cfg = config_mod.read_chat_history()
        head, middle, recent = conv.split_for_compaction(
            model_turns,
            keep_first=int(cfg.get("resume_keep_first", 1) or 0),
            keep_recent=int(cfg.get("resume_keep_recent", 20) or 0),
        )

        summarized = False
        if middle:
            note_body = ""
            if cfg.get("compact_on_resume"):
                note_body = self._summarize_middle_turns(middle)
                summarized = bool(note_body)
            note = (
                f"[Summary of {len(middle)} earlier turns: {note_body}]"
                if note_body
                else self._mechanical_resume_note(middle)
            )
            rebuilt = self._flatten_turns(head)
            recent_msgs = self._flatten_turns(recent)
            # Fold the note into the first (user) recent message so the history
            # stays strictly alternating user/assistant (Anthropic requires it).
            if recent_msgs and recent_msgs[0].get("role") == "user":
                recent_msgs[0]["content"] = note + "\n\n" + recent_msgs[0]["content"]
            else:
                recent_msgs.insert(0, {"role": "user", "content": note})
            rebuilt += recent_msgs
            kept = len(head) + len(recent)
        else:
            rebuilt = self._flatten_turns(model_turns)
            kept = len(model_turns)

        cid = str(conversation_id)
        with self._lock:
            existing = self._sessions.get(cid)
        if existing is not None and existing.interactive:
            # Already open as a session (its id is the conversation id) — just
            # make it the send target; its live history is the truth, so nothing
            # is rebuilt and the transcript on screen is not replaced.
            with self._lock:
                self._chat_session_id = cid
            return {
                "ok": True,
                "turns": len(turns),
                "kept": len(existing.history),
                "compacted": False,
                "summarized": False,
                "exact": False,
                "session": cid,
                "already_open": True,
            }
        session = new_interactive(cid, label=rec.get("title") or "")
        # Rebuilt from a saved transcript — no live turn boundaries survive.
        # Every resumed message carries the neutral "" tag (never a real chat_id,
        # so never excluded → always visible); new turns get their own chat_id
        # via sync_tags.
        session.replace_history(rebuilt)
        session.trim()
        with self._lock:
            self._sessions[session.id] = session
            self._chat_session_id = session.id
        self._emit_session(session)
        return {
            "ok": True,
            "turns": len(turns),
            "kept": kept,
            "compacted": bool(middle),
            "summarized": summarized,
            "exact": False,
            "session": session.id,
        }

    def delete_conversation(self, conversation_id: str) -> None:
        from . import conversations as conv

        conv.delete(
            config_mod.conversations_dir(),
            str(conversation_id),
            workspaces_root=config_mod.workspaces_root(),
        )

    def rename_conversation(self, conversation_id: str, title: str) -> None:
        from . import conversations as conv

        conv.rename(config_mod.conversations_dir(), str(conversation_id), str(title))

    def set_turn_included(self, chat_id: str, included: bool) -> dict[str, Any]:
        """Toggle whether a completed chat exchange stays in the model's context
        (the Console card "in context" checkbox). Non-destructive and reversible:
        the messages remain in the session history; only its visible() filter
        changes, so re-including restores the exchange on the next turn. Returns
        ``{ok, included}`` — the resulting in/out state."""
        cid = str(chat_id or "")
        if not cid:
            return {"ok": False, "included": True}
        session = self._session_for_turn(cid)
        if session is None or not session.interactive:
            return {"ok": False, "included": True}
        with self._lock:
            included_now = session.set_included(cid, included)
        return {"ok": True, "included": included_now}

    @staticmethod
    def _messages_chars(obj: Any) -> int:
        """Serialised character size of a message list / tool-schema list, for the
        context-indicator breakdown. JSON with ``default=str`` so provider-native
        blocks (dicts, SDK objects) never raise; counts structural overhead too,
        which is fine — the slices are scaled to the exact token total anyway."""
        try:
            return len(json.dumps(obj, default=str))
        except (TypeError, ValueError):
            return len(str(obj))

    @staticmethod
    def _context_window(adapter: Any) -> int:
        """Context-window size (tokens) for the gauge. Prefers the adapter's
        declared ``capabilities().context_window`` (from ``[llm.model_limits]`` or
        the Anthropic Models API) — the authoritative figure when the model is
        hidden behind a gateway — and falls back to the model-id substring table
        when the adapter declares none."""
        try:
            caps = adapter.capabilities()
            if getattr(caps, "context_window", None):
                return int(caps.context_window)
        except Exception:  # noqa: BLE001 — gauge is decorative; never fail a turn
            pass
        return utils_mod.model_context_window(getattr(adapter, "model", "") or "")

    @staticmethod
    def _usage_from_response(response: Any) -> dict[str, int]:
        """Pull token counts off a provider response. Anthropic/Bedrock split the
        prompt into uncached `input_tokens` plus `cache_read_input_tokens` (served
        at ~0.1x) and `cache_creation_input_tokens` (the first-turn write); OpenAI
        reports only prompt/completion totals (cache fields resolve to 0)."""
        zero = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        usage = getattr(getattr(response, "_raw", None), "usage", None)
        if usage is None:
            return zero

        def field(*names: str) -> int:
            for name in names:
                value = getattr(usage, name, None)
                if value:
                    return int(value)
            return 0

        return {
            "input": field("input_tokens", "prompt_tokens"),
            "output": field("output_tokens", "completion_tokens"),
            "cache_read": field("cache_read_input_tokens"),
            "cache_creation": field("cache_creation_input_tokens"),
        }
