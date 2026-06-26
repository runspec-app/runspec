"""bridge_invoke.py — InvokeMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any


from . import bridge_utils as utils_mod
from . import config as config_mod
from . import executor as executor_mod
from . import hosts as hosts_mod
from .config import (
    write_docs_dir,
)
from .trace import jsonsafe_messages

from .bridge_const import (
    logger,
    _CANCEL_KEY,
)
from .bridge_utils import (
    _iso_now,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class InvokeMixin:
    def _become_params(
        self, cached: dict[str, Any] | None, hostname: str
    ) -> tuple[str, str, str | None]:
        """Resolve run_as + become method/flags for a runnable on a host.

        Reads the runnable's raw spec (carried through discovery as ``rawSpec``)
        and resolves ``run_as`` for this host. Returns ("", ...) when no
        privilege escalation is configured."""
        from runspec.become import resolve_run_as

        raw = (cached or {}).get("rawSpec") or {}
        run_as = resolve_run_as(raw.get("run_as"), hostname)
        return run_as, str(raw.get("become_method") or "sudo"), raw.get("become_flags")

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
                    if r.get("host") == host and r.get("name") == runnable
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

        # Inject the operator-selected credentials as plain, name-derived env vars
        # (e.g. JIRA_USERNAME/JIRA_PASSWORD). Secrets are read from the keyring here
        # and never reach the surfaced args record. Returns the ids applied, for
        # the audit record.
        cred_env, applied_creds = self._credential_env(host, runnable, credential_ids)

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

        def run() -> None:
            def on_line(line: str, stream: str) -> None:
                self._dispatch(
                    "runspec:output", {"id": inv_id, "line": line, "stream": stream}
                )
                if on_progress is not None:
                    on_progress("line", {"line": line, "stream": stream})

            def on_done(exit_code: int, duration_ms: int) -> None:
                with self._lock:
                    self._in_flight.pop(inv_id, None)
                self._dispatch(
                    "runspec:run_end",
                    {"id": inv_id, "exit_code": exit_code, "duration_ms": duration_ms},
                )
                # Audio cue for the run outcome (best-effort; muted unless enabled).
                self._play_sound(
                    "completion_success" if exit_code == 0 else "completion_error"
                )
                if on_progress is not None:
                    on_progress(
                        "done", {"exit_code": exit_code, "duration_ms": duration_ms}
                    )

            if entry is None or not rp:
                on_line(f"✗  Host '{host}' not found in runspec_hosts.toml", "stderr")
                on_done(-1, 0)
                return

            ssh = entry.get("ssh")
            if ssh:
                # Stream over a pooled transport when pooling is on — the
                # invocation reuses the host's warm connection (one more channel,
                # no new handshake) instead of opening a fresh client.
                pool = self._ssh_pool
                session_factory = None
                if pool is not None:
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

    def send_chat(self, message: str, invocation_id: str | None = None) -> str:
        return self._spawn_chat_turn(message)

    def _spawn_chat_turn(self, message: str, runbook: str = "") -> str:
        """Start an agentic chat turn on a worker thread; return its chat id.

        ``runbook`` (non-empty only on the run_runbook path) tags the turn's run
        provenance so every runnable the agent invokes during it is attributed to
        the runbook in History/Analytics — direct chat leaves it unset (no origin)."""
        chat_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._chat_cancels[chat_id] = threading.Event()

        def run() -> None:
            try:
                try:
                    # Fold the live console context (working-hours rota +
                    # configured triggers/schedules) and the pinned-memory header
                    # into this turn's system prompt via the one-off system_override
                    # path, so they stay fresh without invalidating the cached
                    # adapter. Both empty → cached adapter (and a byte-identical,
                    # prompt-cached system prefix) for an unconfigured console.
                    from . import memory

                    extra = "\n\n".join(
                        part
                        for part in (self._console_context(), memory.build_header())
                        if part
                    )
                    adapter = (
                        self._get_adapter(
                            system_override=self._base_system() + "\n\n" + extra
                        )
                        if extra
                        else self._get_adapter()
                    )
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
                    self._dispatch(
                        "runspec:token",
                        {
                            "id": chat_id,
                            "token": "⚠ No LLM provider configured. Set provider and API key in Settings.",
                        },
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
                    asyncio.run(self._agentic_chat_turn(chat_id, message, adapter))
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
        Settings editor. ``argsRaw`` is the stored table for faithful editor round-trip."""
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
                "steps": s.get("steps", ""),
                "tags": list(s.get("tags", []) or []),
                "discoverable": list(s.get("discoverable", []) or []),
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
                try:
                    runbookargs.parse_runbook_args(raw)
                except Exception as exc:
                    return {"ok": False, "error": f"Runbook '{sid}': {exc}"}
            entry: dict[str, Any] = {
                "id": sid,
                "title": str(r.get("title") or "").strip(),
                "symptom": str(r.get("symptom") or "").strip(),
                "steps": str(r.get("steps") or "").strip(),
                "tags": [
                    str(t).strip() for t in (r.get("tags") or []) if str(t).strip()
                ],
                "updated": memory._now(),
            }
            if raw:
                entry["args"] = dict(raw)
            # The publish audience (which queue catalogs offer this runbook); opt-in,
            # stored only when set so an ordinary runbook stays out of every catalog.
            aud = [
                str(a).strip() for a in (r.get("discoverable") or []) if str(a).strip()
            ]
            if aud:
                entry["discoverable"] = aud
            normalised.append(entry)
        memory.save_runbooks(normalised)
        return {"ok": True, "count": len(normalised)}

    def run_runbook(
        self,
        runbook_id: str,
        values: dict[str, Any] | None = None,
        extra_instructions: str = "",
    ) -> dict[str, Any]:
        """Render a runbook's body with the operator's form ``values`` (validated against
        its args), append any free-text ``extra_instructions``, and start a chat turn
        tagged with the runbook. Returns ``{chat_id, message}`` — the rendered text the UI
        shows as the chat block — or ``{error}`` if the runbook is unknown / values invalid."""
        from . import memory, runbookargs

        runbook = memory.load_runbook(str(runbook_id or ""))
        if not runbook:
            return {"error": f"No runbook with id '{runbook_id}'."}
        vals = dict(values or {})
        raw = runbook.get("args") if isinstance(runbook.get("args"), dict) else {}
        errors = runbookargs.validate_runbook_values(raw, vals)
        if errors:
            return {"error": "\n".join(errors)}
        body = memory.render_runbook_body(str(runbook.get("steps") or ""), vals)
        extra = str(extra_instructions or "").strip()
        message = f"{body}\n\n{extra}".strip() if extra else body.strip()
        if not message:
            return {"error": "This runbook has an empty body."}
        chat_id = self._spawn_chat_turn(message, runbook=str(runbook.get("id") or ""))
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
        """Signal an in-flight chat turn to stop. Best-effort: it halts at the
        next safe checkpoint — no further model calls and no further tool runs.
        A tool already executing (e.g. a remote command) is left to finish."""
        with self._lock:
            event = self._chat_cancels.get(chat_id)
        if event is not None:
            logger.info("chat %s: cancel requested", chat_id)
            event.set()
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

    def clear_chat(self) -> None:
        """Start a fresh conversation — drop the model's memory of prior turns.

        Blocks on the conversation lock so it can't run mid-turn (a turn in
        flight finishes against its history, then the next turn starts empty)."""
        with self._chat_lock:
            self._chat_history = []

    def _trim_chat_history(self, max_messages: int = 60) -> None:
        """Bound the rolling conversation so a long-lived session doesn't grow
        context without limit. Trims oldest messages, then drops any leading
        assistant / tool-result turn so the kept history still starts on a plain
        user message — the API requires that, and it keeps tool_use/tool_result
        pairs intact. Caller must hold self._chat_lock."""
        hist = self._chat_history
        if len(hist) <= max_messages:
            return
        del hist[: len(hist) - max_messages]
        while hist and not (
            hist[0].get("role") == "user" and isinstance(hist[0].get("content"), str)
        ):
            hist.pop(0)

    async def _agentic_chat_turn(
        self, chat_id: str, message: str, adapter: Any
    ) -> None:
        start = time.monotonic()
        tools = self._runnables_to_tools()
        # Search mode hides runnables behind search_runnables; fold an always-on
        # menu (id + one-liner) into what the model sees so it knows what exists.
        index_text = (
            self._build_catalog_index() if self._tool_mode() == "search" else ""
        )
        usage_total = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        cancel = self._chat_cancels.get(chat_id) or threading.Event()
        stopped = False

        # Hold the conversation lock for the whole turn: it both serialises
        # concurrent sends and is the single owner of self._chat_history.
        with self._chat_lock:
            history = self._chat_history
            history.append({"role": "user", "content": message})

            for _ in range(10):  # max agentic iterations
                if cancel.is_set():
                    stopped = True
                    break
                response: Any = None
                assistant_text = ""  # this iteration's streamed assistant text
                # The message list actually sent this iteration — history plus the
                # folded-in catalog index (search mode). A per-call copy, so the
                # stored history is never polluted with the index.
                model_messages = self._fold_catalog_index(history, index_text)
                # Diagnostic: the exact message list handed to the adapter — i.e.
                # what the console sends to the model / proxy this iteration.
                self._trace(
                    "model_request",
                    {"chat_id": chat_id, "messages": jsonsafe_messages(model_messages)},
                )
                try:
                    async for event in adapter.stream_with_tools(model_messages, tools):
                        if cancel.is_set():
                            stopped = True
                            break  # abort streaming (closes the provider stream)
                        if event[0] == "text":
                            assistant_text += event[1]
                            self._dispatch(
                                "runspec:token", {"id": chat_id, "token": event[1]}
                            )
                        elif event[0] == "done":
                            response = event[1]
                except Exception as exc:
                    self._dispatch(
                        "runspec:token", {"id": chat_id, "token": f"\n⚠ Error: {exc}"}
                    )
                    break

                if response is not None:
                    for key, value in self._usage_from_response(response).items():
                        usage_total[key] += value

                # Persisting any clean assistant text is always safe (text-only,
                # no tool pairing); do it whenever we're about to leave the loop.
                if stopped:
                    if assistant_text.strip():
                        history.append({"role": "assistant", "content": assistant_text})
                    break

                if (
                    response is None
                    or response.stop_reason != "tool_use"
                    or not response.tool_calls
                ):
                    # Final turn — persist the assistant's reply so the next user
                    # message sees it (tool-calling turns are persisted below via
                    # make_tool_turn, which already carries the assistant turn).
                    if response is not None and assistant_text.strip():
                        history.append({"role": "assistant", "content": assistant_text})
                    break

                # About to act on hosts — honour a stop requested while the model
                # was producing the tool calls, before running any of them.
                if cancel.is_set():
                    stopped = True
                    if assistant_text.strip():
                        history.append({"role": "assistant", "content": assistant_text})
                    break

                # Execute each tool call and dispatch events
                tool_results: list[tuple[Any, str]] = []
                for tc in response.tool_calls:
                    self._dispatch(
                        "runspec:tool_start",
                        {
                            "id": chat_id,
                            "tool_name": tc.name,
                            "tool_input": tc.input,
                        },
                    )
                    if cancel.is_set():
                        # Stopped mid-batch — refuse the rest without prompting
                        # or running, but still record a paired tool_result.
                        output = f"⏹ '{tc.name.split('__', 1)[-1]}' was not run — you stopped the assistant."
                    else:
                        output = await self._gated_run_tool_async(
                            tc.name, tc.input, chat_id
                        )
                    self._dispatch(
                        "runspec:tool_end",
                        {
                            "id": chat_id,
                            "tool_name": tc.name,
                            "output": output[:2000],
                        },
                    )
                    # Diagnostic: the *full* tool-result string the model will
                    # receive (the card only shows output[:2000]). This is the
                    # byte-for-byte content that goes into the next request.
                    self._trace(
                        "tool_result",
                        {
                            "chat_id": chat_id,
                            "tool": tc.name,
                            "length": len(output),
                            "output": output,
                        },
                    )
                    tool_results.append((tc, output))

                history.extend(adapter.make_tool_turn(response, tool_results))

            self._trim_chat_history()

        duration_ms = int((time.monotonic() - start) * 1000)
        if stopped:
            self._dispatch("runspec:token", {"id": chat_id, "token": "\n⏹ Stopped."})
        self._dispatch(
            "runspec:run_end",
            {
                "id": chat_id,
                "exit_code": 0,
                "duration_ms": duration_ms,
                "stopped": stopped,
            },
        )
        # Audio cue: "reply ready" when the agent finishes and the console isn't
        # foreground (you're elsewhere); a quiet "stopped" cue if you halted it.
        # Suppressed when you're watching, so a normal back-and-forth stays silent.
        if stopped:
            self._play_sound("completion_stopped")
        elif not self._window_is_foreground():
            self._play_sound("reply")
        if any(usage_total.values()):
            self._dispatch(
                "runspec:chat_usage",
                {
                    "id": chat_id,
                    # The model the turn ran on, so the UI can show an estimated cost.
                    "model": getattr(adapter, "model", "") or "",
                    # input_tokens is the uncached remainder (full price); the cached
                    # portion is reported separately so the UI can show cache savings.
                    "input_tokens": usage_total["input"],
                    "output_tokens": usage_total["output"],
                    "cache_read_tokens": usage_total["cache_read"],
                    "cache_creation_tokens": usage_total["cache_creation"],
                },
            )

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
