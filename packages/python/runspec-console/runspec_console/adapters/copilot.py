"""pip install runspec-console[copilot]

GitHub Copilot as a model provider, through GitHub's official Copilot SDK
(``github-copilot-sdk``). The SDK drives the Copilot CLI runtime, which runs its
own agent loop; the console needs a *model*, so this adapter turns that around:

* every session is created with **only the console's tools** (no built-in shell /
  file / web tools), the console's system prompt (``mode = "replace"``), and the
  session store, skills, memory and repo instructions switched off;
* each console tool is registered with a handler that **waits** for the console:
  when Copilot asks for tools, the adapter returns them as ``tool_use`` and the
  console runs them through its own approval gate; the next adapter call hands
  the results to the waiting handlers and the same Copilot turn carries on.

So the console keeps its tools, approval cards and loop, and one console chat
turn is one Copilot request however many tool steps it takes.

The SDK's client and sessions are bound to an event loop, and the console runs
each turn in its own ``asyncio.run`` (and rebuilds adapters freely), so the SDK
lives in a process-wide runtime with its own loop thread (:class:`_Runtime`),
keyed by sign-in settings. Live Copilot sessions are matched to the console's
history by a hash of the messages they have seen; anything that doesn't line up
(compaction, an edited history, a stopped turn) starts a fresh session that gets
the earlier conversation as a transcript.

Sign-in: the user's own ``copilot login`` (default), or a GitHub token
(``[llm] api_key``, or ``[llm] api_key_command`` such as ``gh auth token``).
The runtime (~180 MB) is downloaded by the SDK on first use; ``[llm] cli_path``
points at a copy instead (air-gapped machines).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable

from .base import (
    ChatResponse,
    ModelAdapter,
    ModelCapabilities,
    ToolCall,
    capabilities_from_limits,
    run_api_key_command,
)

logger = logging.getLogger("runspec_console")

DEFAULT_MODEL = ""  # blank → Copilot's own default model
DEFAULT_SYSTEM = (
    "You are a helpful assistant with access to runspec tools running on local and remote hosts. "
    "Use tools when they help answer the user's request. "
    "When you call a tool, briefly explain what you're doing before the result."
)

# Live Copilot sessions kept per runtime. Each holds a model-side conversation;
# the least recently used idle one is closed past this.
MAX_CONVERSATIONS = 12
# A fresh session gets the earlier conversation as a transcript; keep the newest
# part of it if it is longer than this.
TRANSCRIPT_MAX_CHARS = 200_000
_TOOL_TEXT_MAX = 4_000  # one tool result inside a transcript
NO_RESULT = "(the console did not run this tool)"
SIGN_IN_HINT = (
    "Sign in with `copilot login` on this machine, or set a GitHub token "
    "(or a token command such as `gh auth token`) in Settings → Model / API."
)


@functools.lru_cache(maxsize=1)
def _load_sdk() -> SimpleNamespace:
    """The SDK names the adapter uses, in one namespace (tests swap in a fake)."""
    import copilot  # noqa: F401  (ImportError → the [copilot] extra is missing)
    from copilot import CopilotClient, RuntimeConnection, ToolSet
    from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject
    from copilot.session_events import (
        AssistantMessageData,
        AssistantMessageDeltaData,
        AssistantUsageData,
        PermissionRequestCustomTool,
        SessionErrorData,
        SessionIdleData,
    )
    from copilot.tools import Tool, ToolResult

    return SimpleNamespace(
        CopilotClient=CopilotClient,
        RuntimeConnection=RuntimeConnection,
        ToolSet=ToolSet,
        Tool=Tool,
        ToolResult=ToolResult,
        AssistantMessageData=AssistantMessageData,
        AssistantMessageDeltaData=AssistantMessageDeltaData,
        AssistantUsageData=AssistantUsageData,
        SessionErrorData=SessionErrorData,
        SessionIdleData=SessionIdleData,
        PermissionRequestCustomTool=PermissionRequestCustomTool,
        Approve=PermissionDecisionApproveOnce,
        Reject=PermissionDecisionReject,
    )


# ── history helpers (pure) ────────────────────────────────────────────────────


def _hash_chain(messages: list[dict[str, Any]]) -> list[str]:
    """``out[i]`` identifies ``messages[:i]`` — a running hash, so comparing a
    prefix is one lookup."""
    out = [""]
    h = hashlib.sha256()
    for msg in messages:
        h.update(json.dumps(msg, sort_keys=True, default=str).encode("utf-8"))
        h.update(b"\x1e")
        out.append(h.copy().hexdigest())
    return out


def _tools_key(tools: list[dict[str, Any]]) -> str:
    blob = json.dumps(
        [[t.get("name"), t.get("description"), _schema(t)] for t in tools],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _schema(tool: dict[str, Any]) -> dict[str, Any]:
    return (
        tool.get("input_schema")
        or tool.get("parameters")
        or {"type": "object", "properties": {}}
    )


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [
            b if isinstance(b, dict) else {"type": "text", "text": str(b)}
            for b in content
        ]
    if content is None:
        return []
    return [{"type": "text", "text": str(content)}]


def _plain_user_text(msg: dict[str, Any]) -> str | None:
    """The text of a user message that carries no tool results, else ``None``."""
    if msg.get("role") != "user":
        return None
    parts = []
    for b in _blocks(msg.get("content")):
        if b.get("type") != "text":
            return None
        parts.append(str(b.get("text") or ""))
    return "\n".join(parts)


def _is_plain_assistant(msg: dict[str, Any]) -> bool:
    if msg.get("role") != "assistant" or msg.get("tool_calls"):
        return False
    return all(b.get("type") == "text" for b in _blocks(msg.get("content")))


def _tool_results(messages: list[dict[str, Any]]) -> dict[str, str] | None:
    """Tool results in a tail of the history, by call id. ``None`` when the tail
    holds anything but tool calls and their results (a new user message, …)."""
    results: dict[str, str] = {}
    for msg in messages:
        role = msg.get("role")
        if role == "tool":  # OpenAI shape
            results[str(msg.get("tool_call_id"))] = _result_text(msg.get("content"))
            continue
        if role == "assistant":
            continue  # the tool-call turn itself (or the text before it)
        if role != "user":
            return None
        for b in _blocks(msg.get("content")):
            if b.get("type") == "tool_result":
                results[str(b.get("tool_use_id"))] = _result_text(b.get("content"))
            elif str(b.get("text") or "").strip():
                return None
    return results


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content
        )
    return "" if content is None else str(content)


def _render_message(msg: dict[str, Any]) -> str:
    """One history message as transcript text, whatever provider wrote it."""
    role = str(msg.get("role") or "")
    lines: list[str] = []
    if role == "tool":
        text = _result_text(msg.get("content"))[:_TOOL_TEXT_MAX]
        return f"Tool result ({msg.get('tool_call_id')}):\n{text}"
    for b in _blocks(msg.get("content")):
        kind = b.get("type")
        if kind == "text":
            text = str(b.get("text") or "").strip()
            if text:
                lines.append(f"{role.capitalize()}: {text}")
        elif kind == "tool_use":
            args = json.dumps(b.get("input") or {}, default=str)
            lines.append(
                f"Assistant called tool {b.get('name')} ({b.get('id')}) with {args}"
            )
        elif kind == "tool_result":
            text = _result_text(b.get("content"))[:_TOOL_TEXT_MAX]
            lines.append(f"Tool result ({b.get('tool_use_id')}):\n{text}")
    for tc in msg.get("tool_calls") or []:  # OpenAI shape
        fn = tc.get("function") or {}
        lines.append(
            f"Assistant called tool {fn.get('name')} ({tc.get('id')}) "
            f"with {fn.get('arguments')}"
        )
    return "\n".join(lines)


def _fresh_prompt(messages: list[dict[str, Any]]) -> str:
    """The first prompt of a new Copilot session: the earlier conversation as a
    transcript, then the user's latest message(s)."""
    tail: list[str] = []
    i = len(messages)
    while i > 0:
        text = _plain_user_text(messages[i - 1])
        if text is None:
            break
        tail.insert(0, text)
        i -= 1
    earlier = messages[:i]
    if not earlier:
        return "\n\n".join(tail)
    transcript = "\n\n".join(t for t in (_render_message(m) for m in earlier) if t)
    if len(transcript) > TRANSCRIPT_MAX_CHARS:
        transcript = "…\n" + transcript[-TRANSCRIPT_MAX_CHARS:]
    ask = (
        "\n\n".join(tail)
        if tail
        else ("Carry on from where the conversation above stopped.")
    )
    return (
        "The conversation so far, for context (tool calls shown were already made "
        "and their results are included):\n\n"
        f"{transcript}\n\n---\n\n{ask}"
    )


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _is_sign_in_error(message: str) -> bool:
    low = message.lower()
    return any(
        k in low
        for k in ("authenticat", "oauth token", "not signed in", "sign in", "401")
    )


def _supports_effort(info: Any, effort: str) -> bool:
    """Whether a Copilot model takes this reasoning effort (unknown ⇒ no)."""
    caps = getattr(info, "capabilities", None)
    if not getattr(getattr(caps, "supports", None), "reasoning_effort", False):
        return False
    allowed = getattr(info, "supported_reasoning_efforts", None)
    return not allowed or effort in allowed


# ── the SDK side (runs on the runtime's own loop) ────────────────────────────


@dataclass
class _Conv:
    """One live Copilot session and how far into the console's history it is."""

    model: str
    system: str
    tools_key: str
    session: Any = None
    state: str = "busy"  # busy | idle | pending (waiting for tool results)
    seen: int = 0
    seen_hash: str = ""
    expected: list[str] = field(default_factory=list)
    results: dict[str, asyncio.Future] = field(default_factory=dict)
    events: asyncio.Queue | None = None
    last_used: float = field(default_factory=time.monotonic)


class _Runtime:
    """One Copilot runtime (CLI process + SDK client) on its own loop thread."""

    def __init__(
        self, sdk: SimpleNamespace, client_kwargs: dict[str, Any], workdir: str
    ):
        self.sdk = sdk
        self.client_kwargs = client_kwargs
        self.workdir = workdir
        self.loop = asyncio.new_event_loop()
        self.client: Any = None
        self.convs: list[_Conv] = []
        self.models: dict[str, Any] = {}
        # Set after a sign-in failure: the next adapter call starts a new runtime
        # (re-reading the token / the user's `copilot login`).
        self.stale = False
        self._start_lock: asyncio.Lock | None = None
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="copilot-runtime", daemon=True
        )
        self.thread.start()

    def submit(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def ensure_client(self) -> Any:
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if self.client is None:
                client = self.sdk.CopilotClient(**self.client_kwargs)
                await client.start()
                self.client = client
                try:
                    for info in await client.list_models():
                        self.models[str(getattr(info, "id", ""))] = info
                except Exception as exc:  # noqa: BLE001 - informational only
                    logger.info("copilot: could not list models: %s", exc)
            return self.client

    async def status(self) -> dict[str, Any]:
        client = await self.ensure_client()
        auth = await client.get_auth_status()
        out: dict[str, Any] = {
            "ok": bool(getattr(auth, "isAuthenticated", False)),
            "login": getattr(auth, "login", None) or "",
            "host": getattr(auth, "host", None) or "",
            "message": getattr(auth, "statusMessage", None) or "",
            "models": [],
        }
        if not out["ok"]:
            self.stale = True
        else:
            self.models.clear()
            for info in await client.list_models():
                self.models[str(info.id)] = info
            for info in self.models.values():
                policy = getattr(getattr(info, "policy", None), "state", "") or ""
                billing = getattr(info, "billing", None)
                out["models"].append(
                    {
                        "id": str(info.id),
                        "name": str(getattr(info, "name", "") or info.id),
                        "enabled": policy != "disabled",
                        "multiplier": getattr(billing, "multiplier", None),
                    }
                )
        return out

    async def shutdown(self) -> None:
        for conv in list(self.convs):
            await self._close(conv)
        if self.client is not None:
            try:
                await self.client.stop()
            except Exception:  # noqa: BLE001 - best effort
                pass
            self.client = None
        self.loop.call_soon(self.loop.stop)

    # -- conversations --------------------------------------------------------

    def _match(self, hashes: list[str], model: str, system: str) -> _Conv | None:
        best: _Conv | None = None
        for conv in self.convs:
            if conv.state == "busy" or conv.model != model or conv.system != system:
                continue
            if conv.seen >= len(hashes) or hashes[conv.seen] != conv.seen_hash:
                continue
            if best is None or conv.seen > best.seen:
                best = conv
        return best

    async def _close(self, conv: _Conv) -> None:
        if conv in self.convs:
            self.convs.remove(conv)
        for fut in conv.results.values():
            if not fut.done():
                fut.set_result(NO_RESULT)
        if conv.session is not None:
            for step in ("abort", "disconnect"):
                try:
                    await getattr(conv.session, step)()
                except Exception:  # noqa: BLE001 - best effort
                    pass

    async def _evict(self) -> None:
        idle = [c for c in self.convs if c.state != "busy"]
        while len(self.convs) > MAX_CONVERSATIONS and idle:
            oldest = min(idle, key=lambda c: c.last_used)
            idle.remove(oldest)
            await self._close(oldest)

    def _tool_objects(self, conv: _Conv, tools: list[dict[str, Any]]) -> list[Any]:
        sdk = self.sdk
        loop = self.loop

        def make_handler(name: str) -> Callable[[Any], Any]:
            async def handler(invocation: Any) -> Any:
                call_id = str(getattr(invocation, "tool_call_id", ""))
                fut = conv.results.get(call_id)
                if fut is None:
                    fut = conv.results[call_id] = loop.create_future()
                text = await fut
                conv.results.pop(call_id, None)
                return sdk.ToolResult(
                    text_result_for_llm=str(text), result_type="success"
                )

            return handler

        return [
            sdk.Tool(
                name=str(t["name"]),
                description=str(t.get("description") or ""),
                parameters=_schema(t),
                handler=make_handler(str(t["name"])),
                skip_permission=True,
                defer="never",
            )
            for t in tools
        ]

    def _on_permission(self, request: Any, _invocation: Any) -> Any:
        # Only the console's own tools exist in these sessions, and the console
        # gates them itself; refuse anything else the runtime might ask about.
        if isinstance(request, self.sdk.PermissionRequestCustomTool):
            return self.sdk.Approve()
        return self.sdk.Reject(feedback="Only the console's tools are available here.")

    async def _new_conv(
        self,
        model: str,
        system: str,
        tools: list[dict[str, Any]],
        tools_key: str,
        effort: str,
    ) -> _Conv:
        client = await self.ensure_client()
        conv = _Conv(model=model, system=system, tools_key=tools_key)
        events: asyncio.Queue = asyncio.Queue()
        conv.events = events
        loop = self.loop

        def on_event(event: Any) -> None:
            if getattr(event, "agent_id", None):
                return  # a sub-agent's event (none are enabled; be safe)
            loop.call_soon_threadsafe(events.put_nowait, event)

        kwargs: dict[str, Any] = dict(
            model=model or None,
            streaming=True,
            tools=self._tool_objects(conv, tools),
            available_tools=self.sdk.ToolSet().add_custom("*"),
            system_message={"mode": "replace", "content": system},
            on_permission_request=self._on_permission,
            working_directory=self.workdir,
            skip_custom_instructions=True,
            enable_skills=False,
            enable_session_store=False,
            enable_config_discovery=False,
            enable_file_hooks=False,
            enable_host_git_operations=False,
            enable_on_demand_instruction_discovery=False,
            memory={"enabled": False},
            on_event=on_event,
        )
        if effort and _supports_effort(self.models.get(model), effort):
            kwargs["reasoning_effort"] = effort
        conv.session = await client.create_session(**kwargs)
        self.convs.append(conv)
        await self._evict()
        return conv

    async def request(
        self,
        messages: list[dict[str, Any]],
        hashes: list[str],
        tools: list[dict[str, Any]],
        model: str,
        system: str,
        effort: str,
        emit: Callable[[tuple[str, Any]], None],
        cancelled: threading.Event,
    ) -> None:
        """One adapter call: send/continue, then stream until Copilot asks for
        tools or finishes. Everything reaches the caller through ``emit``."""
        conv: _Conv | None = None
        try:
            tools_key = _tools_key(tools)
            conv = self._match(hashes, model, system)
            prompt: str | None = None
            if conv is not None:
                tail = messages[conv.seen :]
                if conv.state == "pending":
                    results = _tool_results(tail)
                    if results is None or not (set(results) & set(conv.expected)):
                        await self._close(conv)
                        conv = None
                    else:
                        conv.state = "busy"
                        self._drain(conv)
                        for call_id in conv.expected:
                            fut = conv.results.get(call_id)
                            if fut is None:
                                fut = conv.results[call_id] = self.loop.create_future()
                            if not fut.done():
                                fut.set_result(results.get(call_id, NO_RESULT))
                        conv.expected = []
                else:  # idle: the reply we gave, then the user's next message(s)
                    rest = [m for m in tail if not _is_plain_assistant(m)]
                    texts = [_plain_user_text(m) for m in rest]
                    if not rest or any(t is None for t in texts):
                        await self._close(conv)
                        conv = None
                    else:
                        if conv.tools_key != tools_key:
                            try:
                                await conv.session.set_tools(
                                    self._tool_objects(conv, tools)
                                )
                                conv.tools_key = tools_key
                            except Exception:  # noqa: BLE001 - start over instead
                                await self._close(conv)
                                conv = None
                        if conv is not None:
                            conv.state = "busy"
                            prompt = "\n\n".join(t for t in texts if t)
            if conv is None:
                conv = await self._new_conv(model, system, tools, tools_key, effort)
                prompt = _fresh_prompt(messages)
            conv.seen = len(messages)
            conv.seen_hash = hashes[-1]
            conv.last_used = time.monotonic()
            if prompt is not None:
                self._drain(conv)
                await conv.session.send(prompt)
            await self._stream(conv, emit, cancelled)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            if conv is not None:
                await self._close(conv)
            emit(("error", exc))

    @staticmethod
    def _drain(conv: _Conv) -> None:
        assert conv.events is not None
        while not conv.events.empty():
            conv.events.get_nowait()

    async def _stream(
        self,
        conv: _Conv,
        emit: Callable[[tuple[str, Any]], None],
        cancelled: threading.Event,
    ) -> None:
        sdk = self.sdk
        assert conv.events is not None
        text = ""
        streamed: set[str] = set()
        usage = SimpleNamespace(input_tokens=0, output_tokens=0)
        while True:
            if cancelled.is_set():
                await self._close(conv)
                return
            try:
                event = await asyncio.wait_for(conv.events.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            data = getattr(event, "data", None)
            if isinstance(data, sdk.AssistantMessageDeltaData):
                if getattr(data, "parent_tool_call_id", None):
                    continue
                delta = data.delta_content or ""
                if delta:
                    streamed.add(str(data.message_id))
                    text += delta
                    emit(("text", delta))
            elif isinstance(data, sdk.AssistantUsageData):
                usage.input_tokens += int(getattr(data, "input_tokens", 0) or 0)
                usage.output_tokens += int(getattr(data, "output_tokens", 0) or 0)
            elif isinstance(data, sdk.AssistantMessageData):
                if getattr(data, "parent_tool_call_id", None):
                    continue
                if str(data.message_id) not in streamed and data.content:
                    sep = "\n\n" if text and not text.endswith("\n") else ""
                    text += sep + data.content
                    emit(("text", sep + data.content))
                requests = list(getattr(data, "tool_requests", None) or [])
                if requests:
                    calls = [
                        ToolCall(
                            id=str(r.tool_call_id),
                            name=str(r.name),
                            input=_arguments(getattr(r, "arguments", None)),
                        )
                        for r in requests
                    ]
                    conv.expected = [c.id for c in calls]
                    conv.state = "pending"
                    conv.last_used = time.monotonic()
                    emit(
                        (
                            "done",
                            ChatResponse(
                                text=text or None,
                                tool_calls=calls,
                                stop_reason="tool_use",
                                _raw=SimpleNamespace(usage=usage),
                            ),
                        )
                    )
                    return
            elif isinstance(data, sdk.SessionErrorData):
                message = str(getattr(data, "message", "") or "Copilot error")
                if _is_sign_in_error(message):
                    self.stale = True
                    if not message.rstrip().endswith((".", "!", "?")):
                        message = message.rstrip() + "."
                    message = f"{message} {SIGN_IN_HINT}"
                raise RuntimeError(f"GitHub Copilot: {message}")
            elif isinstance(data, sdk.SessionIdleData):
                conv.state = "idle"
                conv.last_used = time.monotonic()
                emit(
                    (
                        "done",
                        ChatResponse(
                            text=text or None,
                            tool_calls=[],
                            stop_reason="end_turn",
                            _raw=SimpleNamespace(usage=usage),
                        ),
                    )
                )
                return


_RUNTIMES: dict[tuple[Any, ...], _Runtime] = {}
_RUNTIMES_LOCK = threading.Lock()


def _runtime_for(key: tuple[Any, ...], factory: Callable[[], _Runtime]) -> _Runtime:
    with _RUNTIMES_LOCK:
        rt = _RUNTIMES.get(key)
        if rt is not None and rt.stale and not any(c.state == "busy" for c in rt.convs):
            rt.submit(rt.shutdown())
            rt = None
        if rt is None:
            rt = _RUNTIMES[key] = factory()
        return rt


# ── the adapter ──────────────────────────────────────────────────────────────


class CopilotAdapter(ModelAdapter):
    """GitHub Copilot through the Copilot SDK. See the module docstring."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        system: str = DEFAULT_SYSTEM,
        api_key: str | None = None,
        api_key_command: str | None = None,
        api_key_ttl_ms: int | None = None,
        cli_path: str | None = None,
        effort: str | None = None,
        model_limits: dict[str, Any] | None = None,
        workdir: str | None = None,
        _sdk: SimpleNamespace | None = None,
    ) -> None:
        self.model = model or ""
        self.system = system or DEFAULT_SYSTEM
        self._token = (api_key or "").strip()
        self._token_command = (api_key_command or "").strip()
        self._cli_path = (cli_path or "").strip()
        level = (effort or "").strip().lower()
        self._effort = "" if level in ("", "off", "none") else level
        self._model_limits = model_limits or {}
        self._workdir = workdir or ""
        # Import now so a missing extra fails at build time with the install hint.
        self._sdk = _sdk if _sdk is not None else _load_sdk()
        del api_key_ttl_ms  # a GitHub token is long-lived; read once per runtime

    # -- runtime --------------------------------------------------------------

    def _runtime(self) -> _Runtime:
        key = (id(self._sdk), self._cli_path, self._token, self._token_command)
        return _runtime_for(key, self._build_runtime)

    def _build_runtime(self) -> _Runtime:
        kwargs: dict[str, Any] = {}
        token = self._token
        if not token and self._token_command:
            token = run_api_key_command(self._token_command)
        if token:
            kwargs["github_token"] = token
        if self._cli_path:
            kwargs["connection"] = self._sdk.RuntimeConnection.for_stdio(
                path=self._cli_path
            )
        return _Runtime(self._sdk, kwargs, self._workdir or _default_workdir())

    def status(self, timeout: float = 120.0) -> dict[str, Any]:
        """Sign-in state and the models this account can use (blocking)."""
        rt = self._runtime()
        return rt.submit(rt.status()).result(timeout=timeout)

    # -- ModelAdapter -----------------------------------------------------------

    async def stream_with_tools(  # type: ignore[override]
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[tuple[str, Any]]:
        rt = self._runtime()
        caller = asyncio.get_running_loop()
        out: asyncio.Queue = asyncio.Queue()
        cancelled = threading.Event()

        def emit(item: tuple[str, Any]) -> None:
            caller.call_soon_threadsafe(out.put_nowait, item)

        snapshot = json.loads(json.dumps(messages, default=str))
        rt.submit(
            rt.request(
                snapshot,
                _hash_chain(snapshot),
                list(tools or []),
                self.model,
                self.system,
                self._effort,
                emit,
                cancelled,
            )
        )
        finished = False
        try:
            while True:
                kind, value = await out.get()
                if kind == "error":
                    finished = True
                    raise value
                yield (kind, value)
                if kind == "done":
                    finished = True
                    return
        finally:
            if not finished:
                cancelled.set()  # the console stopped reading: end that Copilot turn

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatResponse:
        response: ChatResponse | None = None
        async for kind, value in self.stream_with_tools(messages, tools):
            if kind == "done":
                response = value
        assert response is not None
        return response

    async def stream_chat(  # type: ignore[override]
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[str]:
        async for kind, value in self.stream_with_tools(messages, tools):
            if kind == "text":
                yield value

    def make_tool_turn(
        self, response: ChatResponse, results: list[tuple[ToolCall, str]]
    ) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        if response.text:
            content.append({"type": "text", "text": response.text})
        content.extend(
            {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
            for tc, _ in results
        )
        return [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tc.id, "content": str(r)}
                    for tc, r in results
                ],
            },
        ]

    def capabilities(self) -> ModelCapabilities:
        # adaptive_thinking stays as the table says: false there makes the console
        # clear self._effort, so no reasoning_effort is sent. The adapter itself
        # only sends an effort a Copilot model says it takes.
        caps = capabilities_from_limits(self.model, self._model_limits)
        caps.cache_markers = False
        with _RUNTIMES_LOCK:
            runtimes = list(_RUNTIMES.values())
        for rt in runtimes:
            info = rt.models.get(self.model)
            limits = getattr(getattr(info, "capabilities", None), "limits", None)
            if limits is None:
                continue
            if caps.context_window is None:
                caps.context_window = getattr(limits, "max_context_window_tokens", None)
            if caps.max_output_tokens is None:
                caps.max_output_tokens = getattr(limits, "max_output_tokens", None)
            break
        return caps


def _default_workdir() -> str:
    """An empty folder for the runtime to sit in, so it finds no repo or
    instruction files to read."""
    try:
        from ..config import app_dir

        path = app_dir() / "copilot-workdir"
    except Exception:  # noqa: BLE001 - fall back to a temp folder
        import tempfile
        from pathlib import Path

        path = Path(tempfile.gettempdir()) / "runspec-console-copilot"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)
