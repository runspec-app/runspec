"""Bridge mixin: chat sessions + the one agent-turn engine.

Every agent turn the console runs — an interactive chat message, a reactive
agent-mode trigger, a scheduled prompt/runbook, a room-assigned runbook — goes
through :meth:`TurnMixin._run_agent_turn` with a :class:`TurnSpec` describing
what differs: the session it runs against, the tools it may call, whether it is
interactive (persist + sounds) or headless (card timeline/column hooks, inline
draft tools, the longer confirm timeout, a summary string for the run card).
Before 0.275.0 there were two ~500-line copies of this loop (the chat loop in
``bridge_invoke`` and the "isolated" loop in ``bridge_triggers``) that had
drifted: the headless one emitted no token usage, no audit record, no turn
metric and no context estimate, never auto-continued a cut-off reply, and sent
a poorer ``tool_end``. One engine means every kind of turn gets every feature.

The session registry lives here too (``Bridge._sessions``): interactive
sessions (concurrent chats the operator swaps between) and automation sessions
(one per trigger / schedule fire, read-only). See ``session.py`` for the
identity rules and ``docs/design/console-sessions.md`` for the design.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import bridge_utils as utils_mod
from . import config as config_mod
from . import observability
from .adapters.base import (
    describe_exception,
    is_output_truncation,
    is_transient_model_error,
    retry_delay,
)
from .bridge_const import CURRENT_CALL_ID, logger
from .session import (
    ChatSession,
    new_automation,
    new_interactive,
    prune_finished,
    session_label,
)
from .trace import jsonsafe_messages

if TYPE_CHECKING:
    from .bridge import Bridge

    _Base = Bridge
else:
    _Base = object


InlineTool = Callable[[dict[str, Any]], Awaitable[str]]

# A saved/live reasoning summary is capped — it's a UI note, not model input.
_THOUGHT_CAP = 4000


def _thought_ms(t0: float | None, t1: float) -> int:
    """How long a reasoning summary streamed (first → last delta), in ms."""
    return int(max(0.0, t1 - t0) * 1000) if t0 is not None else 0


@dataclass
class TurnSpec:
    """Everything one agent turn needs; built by the callers, consumed by the
    engine. ``interactive`` turns persist to the conversation store, trim the
    rolling history and play the reply cue; headless (automation) turns drive
    a run card through ``record``/``stage`` and return a summary string."""

    chat_id: str
    session: ChatSession
    message: str
    adapter: Any
    tools: list[dict[str, Any]]
    kind: str = "chat"  # "chat" | "trigger" | "schedule" → span / metric kind
    interactive: bool = True
    #: Confirm gates wait on the longer unattended timeout (the operator may be
    #: away when a trigger fires). Explicit — no longer inferred from ``stage``.
    headless_confirm: bool = False
    cancel: threading.Event | None = None
    system_parts: dict[str, int] = field(default_factory=dict)
    index_text: str = ""
    #: Card timeline / column hooks (automation only).
    record: Callable[[str], None] | None = None
    stage: Callable[[str], None] | None = None
    #: Tools handled inside the turn instead of the autonomy gate — the
    #: trigger draft tools (``draft_reply`` / ``propose_reply``).
    inline_tools: dict[str, InlineTool] = field(default_factory=dict)
    pause_hint: str = 'say "continue" to keep going'
    cutoff_hint: str = 'say "continue" to finish it'
    max_continuations: int | None = None
    summary_cap: int = 4000


@dataclass
class TurnResult:
    chat_id: str
    stopped: bool = False
    duration_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    #: The provider-agnostic transcript of the turn (all assistant prose).
    assistant_text: str = ""
    #: The model's closing reply (its conclusion), for the run card.
    final_text: str = ""
    actions: list[str] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    #: ``• actions`` + closing reply (+ ``⏹ Stopped.``), capped — the run card.
    summary: str = ""
    error: str = ""


class TurnMixin(_Base):
    # ── session registry ─────────────────────────────────────────────────

    def _emit_session(self, session: ChatSession) -> None:
        self._dispatch("runspec:session", session.to_dict())

    def _chat_session(self) -> ChatSession:
        """The interactive session sends / operator runs / the workspace
        fallback target — created on first use (an id is minted up front, so
        the session's workspace and conversation file share it)."""
        created = False
        with self._lock:
            session = self._sessions.get(self._chat_session_id)
            if session is None or not session.interactive:
                session = new_interactive()
                self._sessions[session.id] = session
                self._chat_session_id = session.id
                created = True
        if created:
            self._emit_session(session)
        return session

    def _session_for_turn(self, chat_id: str) -> ChatSession | None:
        """The session a turn id belongs to — the live mapping while the turn
        runs, else by tag (a completed turn's messages carry its id) or by
        identity (an automation session id *is* its turn id)."""
        if not chat_id:
            return None
        with self._lock:
            sid = self._turn_sessions.get(chat_id, "")
            session = self._sessions.get(sid) if sid else None
            if session is None:
                for cand in self._sessions.values():
                    if cand.id == chat_id or cand.owns_turn(chat_id):
                        session = cand
                        break
        return session

    def _resolve_chat_session(self, session_id: str = "") -> ChatSession | None:
        """The interactive session a public method with an optional
        ``session_id`` targets: that session (must exist and be interactive),
        or the current chat session when none is given. ``None`` = bad id."""
        sid = str(session_id or "")
        if not sid:
            return self._chat_session()
        with self._lock:
            session = self._sessions.get(sid)
        if session is None or not session.interactive:
            return None
        return session

    def _open_automation_session(
        self, kind: str, chat_id: str, label: str
    ) -> ChatSession:
        """Register a read-only session for a trigger / schedule card (its id
        is the card id) and announce it. Re-opening an id replaces the entry."""
        session = new_automation(kind, chat_id, label)
        with self._lock:
            self._sessions[session.id] = session
            evicted = prune_finished(self._sessions)
        self._emit_session(session)
        self._announce_evicted(evicted)
        return session

    def _close_automation_session(self, chat_id: str, state: str = "done") -> None:
        """Mark an automation session finished (``done`` / ``failed`` both read
        as finished; the card carries the outcome) and prune the finished set."""
        with self._lock:
            session = self._sessions.get(chat_id)
            if session is None or session.interactive:
                return
            session.state = "done" if state != "closed" else "closed"
            session.current_turn = ""
            self._turn_sessions.pop(chat_id, None)
            evicted = prune_finished(self._sessions)
        self._emit_session(session)
        self._announce_evicted(evicted)

    def _announce_evicted(self, ids: list[str]) -> None:
        for sid in ids:
            self._dispatch("runspec:session", {"id": sid, "state": "closed"})

    # ── public JS API ───────────────────────────────────────────────────

    def new_chat_session(self) -> dict[str, Any]:
        """Open a fresh interactive chat and make it the send target — the
        previous one keeps running (its turn, if any, finishes on its own)."""
        session = new_interactive()
        with self._lock:
            self._sessions[session.id] = session
            self._chat_session_id = session.id
        self._emit_session(session)
        return session.to_dict()

    def list_chat_sessions(self) -> list[dict[str, Any]]:
        """Every live session (chat + automation), oldest first — the chip
        row's cold-start seed. Nothing survives a console restart."""
        with self._lock:
            rows = sorted(self._sessions.values(), key=lambda s: s.created_at)
            return [s.to_dict() for s in rows]

    def select_chat_session(self, session_id: str) -> dict[str, Any]:
        """Make an interactive session the send target (operator runs and the
        workspace fallback follow it). Automation sessions are read-only, so
        selecting one is a UI-only affair and is refused here."""
        with self._lock:
            session = self._sessions.get(str(session_id or ""))
            if session is None:
                return {"ok": False, "error": "unknown session"}
            if not session.interactive:
                return {"ok": False, "error": "automation sessions are read-only"}
            self._chat_session_id = session.id
        return {"ok": True, "session": session.to_dict()}

    def close_chat_session(self, session_id: str) -> dict[str, Any]:
        """Drop a session: an in-flight turn is stopped first (the same Stop
        the composer offers), then the session leaves the registry. For an
        automation session this is the chip's dismiss."""
        sid = str(session_id or "")
        with self._lock:
            session = self._sessions.get(sid)
        if session is None:
            return {"ok": False, "error": "unknown session"}
        if session.current_turn:
            self.cancel_chat(session.current_turn)
        with self._lock:
            self._sessions.pop(sid, None)
            if self._chat_session_id == sid:
                self._chat_session_id = ""
            session.state = "closed"
            session.current_turn = ""
        self._emit_session(session)
        return {"ok": True}

    # ── turn lifecycle ─────────────────────────────────────────────────

    def _begin_turn(self, spec: TurnSpec) -> None:
        session = spec.session
        with self._lock:
            self._turn_sessions[spec.chat_id] = session.id
            session.state = "running"
            session.current_turn = spec.chat_id
            if spec.interactive and not session.label:
                session.label = session_label(spec.message)
        self._emit_session(session)

    def _end_turn_session(self, spec: TurnSpec) -> None:
        session = spec.session
        with self._lock:
            self._turn_sessions.pop(spec.chat_id, None)
            live = self._sessions.get(session.id) is session
            session.current_turn = ""
            session.turns += 1
            if session.state != "closed":
                session.state = "idle" if spec.interactive else "done"
            evicted = prune_finished(self._sessions) if not spec.interactive else []
        if live:
            self._emit_session(session)
        self._announce_evicted(evicted)

    async def _run_agent_turn(self, spec: TurnSpec) -> TurnResult:
        """The agent loop: stream → tool_use → autonomy gate → tool_result,
        bounded by the tool-round backstop, with transient-error retries, a
        self-recovery pass, and output-ceiling auto-continuation. Holds the
        session lock for the whole turn (serialises sends on *that* session and
        owns its history). Never raises for an interactive turn (the error is
        surfaced in the transcript); re-raises for a headless one after
        reporting, so the card-failure paths keep firing."""
        chat_id = spec.chat_id
        session = spec.session
        adapter = spec.adapter
        tools = spec.tools
        start = time.monotonic()
        usage_total = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        # For the context indicator: the *last* iteration's prompt size (not the
        # cross-iteration sum) is the current window fill, plus the char size of
        # the history actually sent that iteration.
        last_usage = {"input": 0, "cache_read": 0, "cache_creation": 0}
        last_history_chars = 0
        last_history_msgs = 0
        if spec.cancel is not None:
            cancel = spec.cancel
        else:
            with self._lock:
                cancel = self._chat_cancels.get(chat_id) or threading.Event()
        stopped = False
        # Provider-agnostic transcript of this turn (the saved-conversation
        # store / the run card), separate from the adapter-native history.
        turn_assistant_parts: list[str] = []
        turn_tools: list[dict[str, Any]] = []
        # Reasoning summaries ([llm] thinking_display = "summarized"), per model
        # call: a call that ends in tool calls hands its thought to the first of
        # them ("why it ran this"); one that ends in the reply keeps it as the
        # reply's reasoning. Both ride the live events and the saved transcript.
        reply_thinking: list[str] = []
        reply_thinking_ms = 0
        actions: list[str] = []
        final_text = ""
        # Continuations (finishing a cut-off reply) and tool rounds are
        # independent budgets — a long reply can never starve tool use.
        continuations = 0
        max_continuations = (
            spec.max_continuations
            if spec.max_continuations is not None
            else self._max_continuations()
        )
        tool_rounds = 0
        hit_iteration_cap = False
        # Transient model-call retries used so far (reset after any clean
        # stream) and self-recovery passes used so far, both bounded by config.
        network_retries = 0
        self_recoveries = 0
        model_name = getattr(adapter, "model", "") or ""
        provider_name = str(
            (config_mod.read_config().get("llm") or {}).get("provider") or ""
        )
        log_tag = f"{spec.kind} {chat_id}"
        # OpenTelemetry root span for the whole turn (no-op unless [telemetry]
        # is on); the per-iteration LLM span and each tool span parent to it.
        turn_span = observability.begin_turn(
            spec.kind,
            chat_id,
            model=model_name,
            provenance=getattr(self, "_run_origin", {}).get(chat_id),
        )
        self._begin_turn(spec)
        error = ""
        raised: BaseException | None = None
        try:
            with session.lock:
                history = session.history
                history.append({"role": "user", "content": spec.message})

                while True:
                    if tool_rounds >= self._max_tool_rounds():
                        hit_iteration_cap = True
                        break
                    if cancel.is_set():
                        stopped = True
                        break
                    response: Any = None
                    assistant_text = ""
                    retry_iteration = False
                    errored = False
                    error_detail = ""
                    iter_thinking = ""
                    think_t0: float | None = None
                    think_t1 = 0.0
                    # Tag anything appended since the last iteration with this
                    # chat_id, then build the model's view minus excluded turns.
                    session.sync_tags(chat_id)
                    model_messages = session.visible()
                    last_history_chars = self._messages_chars(model_messages)
                    last_history_msgs = len(model_messages)
                    self._trace(
                        "model_request",
                        {
                            "chat_id": chat_id,
                            "messages": jsonsafe_messages(model_messages),
                        },
                    )
                    with observability.llm_span(
                        provider=provider_name, model=model_name
                    ) as llm_span:
                        observability.add_prompt(
                            model_messages, system=getattr(adapter, "system", "")
                        )
                        try:
                            async for event in adapter.stream_with_tools(
                                model_messages, tools
                            ):
                                if cancel.is_set():
                                    stopped = True
                                    break
                                if event[0] == "text":
                                    assistant_text += event[1]
                                    self._dispatch(
                                        "runspec:token",
                                        {"id": chat_id, "token": event[1]},
                                    )
                                elif event[0] == "thinking":
                                    # Reasoning summary (the anthropic built-in
                                    # with thinking_display = "summarized"): shown
                                    # live, then attached to the step it led to.
                                    iter_thinking += event[1]
                                    think_t1 = time.monotonic()
                                    if think_t0 is None:
                                        think_t0 = think_t1
                                    self._dispatch(
                                        "runspec:thinking",
                                        {"id": chat_id, "text": event[1]},
                                    )
                                elif event[0] == "done":
                                    response = event[1]
                        except Exception as exc:  # noqa: BLE001
                            observability.record_exception(llm_span, exc)
                            detail = describe_exception(exc)
                            max_net = self._max_network_retries()
                            # A transient blip with nothing shown yet → retry the
                            # same call after a backoff. Once text has streamed
                            # the self-recovery pass handles it instead.
                            if (
                                is_transient_model_error(exc)
                                and not assistant_text.strip()
                                and network_retries < max_net
                            ):
                                network_retries += 1
                                delay = retry_delay(network_retries)
                                logger.warning(
                                    "%s: transient model-stream error (%s); "
                                    "retry %d/%d in %.1fs",
                                    log_tag,
                                    detail,
                                    network_retries,
                                    max_net,
                                    delay,
                                )
                                self._dispatch(
                                    "runspec:token",
                                    {
                                        "id": chat_id,
                                        "token": (
                                            f"\n⏳ {detail} — retrying "
                                            f"({network_retries}/{max_net})…"
                                        ),
                                    },
                                )
                                retry_iteration = True
                            else:
                                logger.warning(
                                    "%s: model-stream error (%s)", log_tag, detail
                                )
                                errored = True
                                error_detail = detail

                        if response is not None:
                            u = self._usage_from_response(response)
                            for key, value in u.items():
                                usage_total[key] += value
                            last_usage = {
                                "input": u["input"],
                                "cache_read": u["cache_read"],
                                "cache_creation": u["cache_creation"],
                            }
                            observability.set_llm_response(llm_span, response, u)
                        if assistant_text.strip():
                            observability.add_completion(assistant_text)

                    if retry_iteration:
                        await asyncio.sleep(retry_delay(network_retries))
                        continue

                    if assistant_text.strip():
                        turn_assistant_parts.append(assistant_text.strip())

                    # Durable / budget-exhausted failure: keep any partial reply,
                    # then hand the turn back to the agent to analyse and resume
                    # (bounded), or end with a descriptive error.
                    if errored:
                        if assistant_text.strip():
                            history.append(
                                {"role": "assistant", "content": assistant_text}
                            )
                        if (
                            not cancel.is_set()
                            and self_recoveries < self._max_self_recoveries()
                        ):
                            self_recoveries += 1
                            max_self = self._max_self_recoveries()
                            logger.warning(
                                "%s: self-recovery %d/%d after error (%s)",
                                log_tag,
                                self_recoveries,
                                max_self,
                                error_detail,
                            )
                            self._dispatch(
                                "runspec:token",
                                {
                                    "id": chat_id,
                                    "token": (
                                        f"\n⚠ {error_detail}\n↻ Trying to recover "
                                        f"({self_recoveries}/{max_self})…"
                                    ),
                                },
                            )
                            self._append_recovery_prompt(history, error_detail)
                            network_retries = 0
                            await asyncio.sleep(retry_delay(self_recoveries, base=2.0))
                            continue
                        self._dispatch(
                            "runspec:token",
                            {"id": chat_id, "token": f"\n⚠ Error: {error_detail}"},
                        )
                        turn_assistant_parts.append(f"⚠ Error: {error_detail}")
                        final_text = f"⚠ Error: {error_detail}"
                        break

                    network_retries = 0

                    if stopped:
                        if assistant_text.strip():
                            history.append(
                                {"role": "assistant", "content": assistant_text}
                            )
                        break

                    # Tool calls run whenever present (any adapter's stop_reason
                    # spelling); a length cut-off is any spelling
                    # is_output_truncation() accepts.
                    has_tools = response is not None and bool(response.tool_calls)
                    truncated = response is not None and is_output_truncation(
                        response.stop_reason
                    )

                    # Output-ceiling cut-off — auto-continue (assistant + user,
                    # never a trailing assistant turn) so a long answer streams
                    # out whole; bounded by max_continuations.
                    if (
                        truncated
                        and not has_tools
                        and assistant_text.strip()
                        and continuations < max_continuations
                    ):
                        history.append({"role": "assistant", "content": assistant_text})
                        history.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your previous message was cut off at the output "
                                    "limit. Continue exactly where you left off — do "
                                    "not repeat text you already sent."
                                ),
                            }
                        )
                        continuations += 1
                        if iter_thinking.strip():
                            reply_thinking.append(iter_thinking.strip())
                            reply_thinking_ms += _thought_ms(think_t0, think_t1)
                        continue

                    if response is None or not has_tools:
                        # The thought that led to the reply (not to a tool).
                        if response is not None and iter_thinking.strip():
                            reply_thinking.append(iter_thinking.strip())
                            reply_thinking_ms += _thought_ms(think_t0, think_t1)
                            self._dispatch(
                                "runspec:reply_thinking",
                                {
                                    "id": chat_id,
                                    "text": "\n\n".join(reply_thinking)[:_THOUGHT_CAP],
                                    "ms": reply_thinking_ms,
                                },
                            )
                        # Final turn — persist the reply so the next message sees
                        # it (tool turns are persisted via make_tool_turn below).
                        if response is not None and assistant_text.strip():
                            history.append(
                                {"role": "assistant", "content": assistant_text}
                            )
                        final_text = assistant_text
                        if truncated:
                            marker = (
                                "\n\n_(response was cut off at the output limit — "
                                f"{spec.cutoff_hint})_"
                            )
                            self._dispatch(
                                "runspec:token", {"id": chat_id, "token": marker}
                            )
                            turn_assistant_parts.append(marker.strip())
                            final_text = f"{final_text.rstrip()}{marker}".strip()
                        break

                    # About to act — honour a stop requested while the model was
                    # producing the tool calls.
                    if cancel.is_set():
                        stopped = True
                        if assistant_text.strip():
                            history.append(
                                {"role": "assistant", "content": assistant_text}
                            )
                        break

                    # The thought this model call produced goes to the first tool
                    # call it made (a parallel batch shares one thought — shown
                    # once, on the first row). Keyed by object identity: not
                    # every adapter gives calls an id.
                    thought_by_call: dict[int, tuple[str, int]] = {}
                    if iter_thinking.strip() and response.tool_calls:
                        thought_by_call[id(response.tool_calls[0])] = (
                            iter_thinking.strip()[:_THOUGHT_CAP],
                            _thought_ms(think_t0, think_t1),
                        )

                    async def _exec_call(tc: Any) -> tuple[str, dict[str, Any]]:
                        """Run one tool call end to end — tool_start, the autonomy
                        gate + execution, tool_end, trace — and return its output
                        and the operator's gate decision (if the gate fired).
                        Safe to run several at once for parallel-safe calls: the
                        events carry ``call_id`` so each card pairs with its own
                        result."""
                        start_event: dict[str, Any] = {
                            "id": chat_id,
                            "call_id": tc.id,
                            "tool_name": tc.name,
                            "tool_input": tc.input,
                        }
                        thought = thought_by_call.get(id(tc))
                        if thought:
                            start_event["thinking"] = thought[0]
                            start_event["thinking_ms"] = thought[1]
                        self._dispatch("runspec:tool_start", start_event)
                        display = tc.name.split("__", 1)[-1]
                        # The operator's confirm-gate decision (if the gate fires),
                        # recorded on the saved tool entry and echoed into the live
                        # tool_end so the card keeps its badge.
                        gate_decision: dict[str, Any] = {}
                        inline = spec.inline_tools.get(tc.name)
                        # Gathered calls each run in their own task (their own
                        # context copy), so this names *this* call to any confirm
                        # it raises.
                        call_token = CURRENT_CALL_ID.set(str(tc.id or ""))
                        try:
                            if cancel.is_set():
                                output = f"⏹ '{display}' was not run — you stopped the assistant."
                            elif inline is not None:
                                output = await inline(tc.input)
                            else:
                                output = await self._gated_run_tool_async(
                                    tc.name,
                                    tc.input,
                                    chat_id,
                                    stage=spec.stage,
                                    on_decision=gate_decision.update,
                                    headless=spec.headless_confirm,
                                )
                        except Exception as exc:  # noqa: BLE001
                            # A raising tool must not escape the loop (that strands
                            # the tool_start spinner and aborts the turn) — surface
                            # it as a readable result and always emit tool_end.
                            logger.exception("%s: tool %s raised", log_tag, tc.name)
                            observability.record_exception(
                                observability.current_span(), exc
                            )
                            output = f"✗ '{display}' failed: {exc}"
                            if spec.stage is not None:
                                spec.stage("running")  # un-stick an awaiting card
                        finally:
                            CURRENT_CALL_ID.reset(call_token)
                        self._dispatch(
                            "runspec:tool_end",
                            {
                                "id": chat_id,
                                "call_id": tc.id,
                                "tool_name": tc.name,
                                "output": output[:2000],
                                "output_chars": len(output),
                                "truncated": utils_mod.parse_truncation(output),
                                "operator_decision": gate_decision.get("decision")
                                or "",
                            },
                        )
                        self._trace(
                            "tool_result",
                            {
                                "chat_id": chat_id,
                                "tool": tc.name,
                                "length": len(output),
                                "output": output,
                            },
                        )
                        return output, gate_decision

                    # Independent runnable runs (and read-only discovery) from one
                    # response run concurrently — they're separate processes on the
                    # hosts; a sequential loop made a 5-host inventory 5× slower.
                    # Anything that may prompt the operator or touch shared console
                    # state is a barrier that runs alone, so relative order between
                    # gated and ungated calls is preserved (see _parallel_safe_call).
                    calls = list(response.tool_calls)
                    outcomes: list[tuple[str, dict[str, Any]] | None] = [None] * len(
                        calls
                    )
                    max_parallel = self._max_parallel_tools()
                    limiter = asyncio.Semaphore(max_parallel)

                    async def _limited(i: int) -> None:
                        async with limiter:
                            outcomes[i] = await _exec_call(calls[i])

                    batch: list[int] = []
                    for i, tc in enumerate(calls):
                        if (
                            max_parallel > 1
                            and tc.name not in spec.inline_tools
                            and self._parallel_safe_call(tc.name, tc.input)
                        ):
                            batch.append(i)
                            continue
                        if batch:
                            await asyncio.gather(*(_limited(j) for j in batch))
                            batch = []
                        outcomes[i] = await _exec_call(tc)
                    if batch:
                        await asyncio.gather(*(_limited(j) for j in batch))

                    # Record in the model's original order — tool results must pair
                    # with their tool_use blocks, and the transcript/actions read in
                    # the order the model asked, whatever order they finished in.
                    tool_results: list[tuple[Any, str]] = []
                    for tc, outcome in zip(calls, outcomes):
                        assert outcome is not None
                        output, gate_decision = outcome
                        display = tc.name.split("__", 1)[-1]
                        tool_results.append((tc, output))
                        tool_rec: dict[str, Any] = {
                            "name": tc.name,
                            "input": tc.input,
                            "output": output[:4000],
                        }
                        thought = thought_by_call.get(id(tc))
                        if thought:
                            tool_rec["thinking"] = thought[0]
                            tool_rec["thinking_ms"] = thought[1]
                        if gate_decision.get("decision"):
                            tool_rec["operator_decision"] = gate_decision["decision"]
                            if gate_decision.get("proposed_input") is not None:
                                tool_rec["proposed_input"] = gate_decision[
                                    "proposed_input"
                                ]
                        turn_tools.append(tool_rec)
                        first_line = next(
                            (ln for ln in output.splitlines() if ln.strip()), ""
                        )
                        step = f"{display} → {first_line[:160]}"
                        actions.append(step)
                        if spec.record is not None:
                            spec.record(step)

                    tool_rounds += 1
                    history.extend(adapter.make_tool_turn(response, tool_results))

                # Hit the tool-round backstop mid-work — say so visibly and invite
                # resumption (the tool results are already in history).
                if hit_iteration_cap:
                    marker = (
                        f"\n\n_(paused after {tool_rounds} tool steps — "
                        f"{spec.pause_hint})_"
                    )
                    self._dispatch("runspec:token", {"id": chat_id, "token": marker})
                    turn_assistant_parts.append(marker.strip())
                    final_text = f"{final_text.rstrip()}{marker}".strip()

                session.sync_tags(chat_id)
                if spec.interactive:
                    session.trim()
                    # Only when there is one, so older call shapes still fit.
                    persist_extra: dict[str, Any] = {}
                    if reply_thinking:
                        persist_extra["reply_thinking"] = {
                            "text": "\n\n".join(reply_thinking)[:_THOUGHT_CAP],
                            "ms": reply_thinking_ms,
                        }
                    self._persist_conversation_turn(
                        session,
                        spec.message,
                        "\n\n".join(turn_assistant_parts),
                        turn_tools,
                        usage_total,
                        model_name,
                        **persist_extra,
                    )
        except Exception as exc:  # noqa: BLE001
            raised = exc
            error = describe_exception(exc)
            logger.exception("%s: agent turn failed", log_tag)
            self._dispatch(
                "runspec:token", {"id": chat_id, "token": f"\n⚠ Error: {error}"}
            )
            turn_assistant_parts.append(f"⚠ Error: {error}")
            final_text = f"⚠ Error: {error}"

        duration_ms = int((time.monotonic() - start) * 1000)
        parts: list[str] = []
        if actions:
            parts.append("\n".join(f"• {a}" for a in actions))
        if final_text.strip():
            parts.append(final_text.strip())
        if stopped:
            parts.append("⏹ Stopped.")
        result = TurnResult(
            chat_id=chat_id,
            stopped=stopped,
            duration_ms=duration_ms,
            usage=dict(usage_total),
            assistant_text="\n\n".join(turn_assistant_parts),
            final_text=final_text,
            actions=actions,
            tools=turn_tools,
            summary="\n\n".join(parts)[: spec.summary_cap],
            error=error,
        )
        self._finish_turn(
            spec,
            result,
            turn_span=turn_span,
            last_usage=last_usage,
            last_history_chars=last_history_chars,
            last_history_msgs=last_history_msgs,
            exc=raised,
        )
        if raised is not None and not spec.interactive:
            raise raised
        return result

    def _finish_turn(
        self,
        spec: TurnSpec,
        result: TurnResult,
        *,
        turn_span: Any = None,
        last_usage: dict[str, int] | None = None,
        last_history_chars: int = 0,
        last_history_msgs: int = 0,
        exc: BaseException | None = None,
    ) -> None:
        """Everything a finished turn reports, for every kind of turn: the OTel
        turn span + metric, the History/Analytics ``run_summary`` line,
        ``run_end`` (with ``stopped``), token usage, the context estimate, the
        session lifecycle event — and, for an interactive turn, the audio cue."""
        chat_id = spec.chat_id
        adapter = spec.adapter
        usage_total = result.usage
        observability.end_turn(turn_span, usage=usage_total, exc=exc)
        status = "stopped" if result.stopped else ("error" if exc else "ok")
        observability.record_turn(
            kind=spec.kind,
            provenance=getattr(self, "_run_origin", {}).get(chat_id),
            duration_ms=result.duration_ms,
            usage=usage_total,
            status=status,
        )
        self._audit_chat_turn(
            run_id=uuid.uuid4().hex,
            chat_id=chat_id,
            model=getattr(adapter, "model", "") or "",
            duration_ms=result.duration_ms,
            usage=usage_total,
            stopped=result.stopped,
            error=exc is not None,
        )
        if result.stopped:
            self._dispatch("runspec:token", {"id": chat_id, "token": "\n⏹ Stopped."})
        self._dispatch(
            "runspec:run_end",
            {
                "id": chat_id,
                "session": spec.session.id,
                "exit_code": 1 if exc is not None else 0,
                "duration_ms": result.duration_ms,
                "stopped": result.stopped,
            },
        )
        if spec.interactive:
            # "reply ready" when the console isn't foreground; a quiet "stopped"
            # cue if you halted it. Silent during a watched back-and-forth.
            if result.stopped:
                self._play_sound("completion_stopped")
            elif not self._window_is_foreground():
                self._play_sound("reply")
        if any(usage_total.values()):
            self._dispatch(
                "runspec:chat_usage",
                {
                    "id": chat_id,
                    "session": spec.session.id,
                    "model": getattr(adapter, "model", "") or "",
                    "input_tokens": usage_total.get("input", 0),
                    "output_tokens": usage_total.get("output", 0),
                    "cache_read_tokens": usage_total.get("cache_read", 0),
                    "cache_creation_tokens": usage_total.get("cache_creation", 0),
                },
            )
        self._emit_context_estimate(
            spec,
            last_usage or {},
            last_history_chars,
            last_history_msgs,
        )
        self._end_turn_session(spec)

    def _emit_context_estimate(
        self,
        spec: TurnSpec,
        last_usage: dict[str, int],
        last_history_chars: int,
        last_history_msgs: int,
    ) -> None:
        """Context-window indicator: the last iteration's prompt size is the
        current window fill, split across the buckets that built it by char
        share (exact total, estimated slices) — see
        docs/design/console-context-indicator.md."""
        last_prompt_tokens = (
            last_usage.get("input", 0)
            + last_usage.get("cache_read", 0)
            + last_usage.get("cache_creation", 0)
        )
        if last_prompt_tokens <= 0:
            return
        from . import memory as memory_mod

        adapter = spec.adapter
        model = getattr(adapter, "model", "") or ""
        parts = dict(spec.system_parts or {})
        parts["tools"] = self._messages_chars(spec.tools)
        parts["catalog"] = len(spec.index_text)
        parts["history"] = last_history_chars
        try:
            pins = memory_mod.pinned_count()
        except Exception:  # noqa: BLE001 — pin count is decorative
            pins = 0
        self._dispatch(
            "runspec:context_estimate",
            {
                "id": spec.chat_id,
                "session": spec.session.id,
                "model": model,
                "window": self._context_window(adapter),
                "total_tokens": last_prompt_tokens,
                "cache_hit_pct": round(
                    100 * last_usage.get("cache_read", 0) / last_prompt_tokens
                ),
                "buckets": utils_mod.estimate_context_buckets(
                    parts, last_prompt_tokens
                ),
                "tool_mode": self._tool_mode(),
                "history_msgs": last_history_msgs,
                "memory_pins": pins,
                "memory_pins_shown": memory_mod.HEADER_MAX_FACTS,
                "catalog_overflow": (
                    getattr(self, "_catalog_overflow", 0) if spec.index_text else 0
                ),
            },
        )
