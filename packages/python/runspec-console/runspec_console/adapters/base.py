"""
base.py — ModelAdapter ABC shared across all LLM providers.

Identical contract to runspec-chat's adapter.py so implementations
can be ported between the two packages without changes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ChatResponse:
    text: str | None
    tool_calls: list[ToolCall]
    # Why the model stopped. The agent loop reads this:
    #   • tool calls present            → the loop runs them (return "tool_use")
    #   • hit the output-token ceiling  → the loop AUTO-CONTINUES the reply
    #     (return "max_tokens", or any provider spelling is_output_truncation()
    #     accepts — "length", "max_output_tokens", …)
    #   • anything else ("end_turn" / "stop" / …) → the turn ends
    # A custom adapter MUST surface a truncation stop reason for long replies to
    # continue instead of being silently cut. See adapters/testing.py.
    stop_reason: str
    _raw: Any = field(repr=False, default=None)


@dataclass
class ModelCapabilities:
    """What the model *behind* an adapter can do — the facts the console needs to
    configure a request safely, independent of which provider or proxy carries it.

    Returned by :meth:`ModelAdapter.capabilities`. Populated from the
    ``[llm.model_limits]`` config table (so it rides the config seed / Config Sync)
    and, for the native Anthropic adapter, best-effort from the Models API. Every
    field is optional/defaulted so an adapter that declares nothing still works:

      * ``context_window``     — total prompt+output token budget. Feeds the context
        gauge (``None`` ⇒ fall back to the model-id substring table).
      * ``max_output_tokens``  — hard ceiling on a single reply. The console clamps
        ``[llm] max_tokens`` down to this so a generous operator setting can't 400
        against a model with a lower cap (``None`` ⇒ don't clamp).
      * ``adaptive_thinking``  — the model/gateway accepts extended-thinking /
        reasoning-effort requests. ``False`` ⇒ the console sends no thinking/effort
        (e.g. a LangChain proxy in front of Bedrock that drops the field).
      * ``cache_markers``      — prompt-cache breakpoints are honoured, so applying
        them is worthwhile.
      * ``strict_tools``       — the endpoint enforces strict JSON-schema tool
        arguments (informational; lets an adapter/proxy advertise the contract).
    """

    context_window: int | None = None
    max_output_tokens: int | None = None
    adaptive_thinking: bool = True
    cache_markers: bool = True
    strict_tools: bool = False


def capabilities_from_limits(
    model: str, limits: dict[str, Any] | None
) -> ModelCapabilities:
    """Build a :class:`ModelCapabilities` for ``model`` from a ``[llm.model_limits]``
    table — a mapping of *model-id substring* → a table of capability fields.

    Every entry whose key is a case-insensitive substring of ``model`` applies, and
    they are **merged shortest-key → longest-key**, so a broad ``"claude"`` base can
    set ``context_window`` and a specific ``"claude-opus-4-8"`` override just
    ``max_output_tokens`` — the longer (more specific) key wins field-by-field. An
    empty ``""`` key is a catch-all that matches any model (lowest precedence).
    Unknown keys in an entry are ignored; absent fields fall back to the
    :class:`ModelCapabilities` defaults. Numeric fields coerce leniently (a bad
    value is dropped, not fatal). Deterministic (ties on key length break by the
    key string). Pure/SDK-free and testable — the default
    :meth:`ModelAdapter.capabilities` is just this over the adapter's stored table."""
    caps = ModelCapabilities()
    if not isinstance(limits, dict) or not limits:
        return caps
    m = (model or "").lower()
    matches: list[tuple[int, str, dict[str, Any]]] = []
    for key, entry in limits.items():
        ks = str(key).lower()
        if not isinstance(entry, dict):
            continue
        if ks and ks not in m:
            continue
        matches.append((len(ks), ks, entry))
    if not matches:
        return caps
    matches.sort(key=lambda t: (t[0], t[1]))  # shortest/broadest first
    best: dict[str, Any] = {}
    for _len, _ks, entry in matches:
        best.update(entry)  # longer (more specific) keys override

    def _int(name: str) -> int | None:
        val = best.get(name)
        if val is None:
            return None
        try:
            n = int(val)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    def _bool(name: str, default: bool) -> bool:
        if name not in best:
            return default
        val = best[name]
        if isinstance(val, str):
            return val.strip().lower() not in ("false", "0", "no", "off", "")
        return bool(val)

    return ModelCapabilities(
        context_window=_int("context_window"),
        max_output_tokens=_int("max_output_tokens"),
        adaptive_thinking=_bool("adaptive_thinking", caps.adaptive_thinking),
        cache_markers=_bool("cache_markers", caps.cache_markers),
        strict_tools=_bool("strict_tools", caps.strict_tools),
    )


# stop_reason values, across providers, that mean the model stopped because it
# hit its output-token ceiling (not a natural end / a tool call). The agent loop
# treats any of these as a truncation to auto-continue, so a custom adapter can
# return whichever spelling its provider reports.
_OUTPUT_TRUNCATION_STOPS = frozenset(
    {
        "max_tokens",  # Anthropic / Bedrock Converse
        "length",  # OpenAI (finish_reason)
        "model_length",
        "max_output_tokens",
        "max_completion_tokens",
    }
)


def is_output_truncation(stop_reason: str | None) -> bool:
    """True when ``stop_reason`` means the model hit its output-token ceiling
    (any provider spelling). The agentic loop auto-continues the reply on these.

    Part of the adapter contract: a custom adapter should return one of these
    (canonically ``"max_tokens"``) when its provider reports a length cutoff."""
    return (stop_reason or "").strip().lower() in _OUTPUT_TRUNCATION_STOPS


# ── model-call error classification + recovery ────────────────────────────────
#
# A model request rides HTTP(S) — a corporate proxy, an SSH-tunnelled gateway, an
# overloaded API — so a single call can die on a transient blip (a read timeout, a
# dropped connection, a 429/503) that a plain retry would sail past. The agent loop
# classifies the exception with these helpers so it can retry the *same* call
# instead of ending the whole turn on a bare, often message-less exception (an
# ``httpx.ReadTimeout`` stringifies to ""). Kept here — SDK-free and pure — so both
# the chat loop and the headless trigger/schedule loop share one classifier, and so
# they're unit-testable without a live provider.

# Exception class names (matched across the __cause__/__context__ chain and each
# class's MRO) that mean a transient network / provider blip worth retrying. Matched
# by *name* rather than by importing httpx / the anthropic SDK, so the classifier
# stays dependency-free and works whichever provider raised it.
_TRANSIENT_EXC_NAMES = frozenset(
    {
        # httpx / anyio timeouts + connection failures
        "TimeoutException",
        "ReadTimeout",
        "WriteTimeout",
        "ConnectTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "ConnectError",
        "CloseError",
        "NetworkError",
        "ProxyError",
        "RemoteProtocolError",
        "LocalProtocolError",
        # stdlib / urllib3 / socket
        "ConnectionError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "IncompleteRead",
        "ProtocolError",
        # provider SDK (anthropic / openai) transient shapes
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "OverloadedError",
        "ServiceUnavailableError",
        "RateLimitError",
    }
)

# HTTP status codes (on an exception carrying a ``status_code``) that a retry can
# clear: request timeout, conflict, rate limit, and the 5xx server/gateway family.
_TRANSIENT_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})


def is_transient_model_error(exc: BaseException) -> bool:
    """True when ``exc`` looks like a transient model-call failure worth retrying
    (a read/connect timeout, a dropped connection, a 429/5xx) rather than a durable
    error (bad request, auth failure, unknown model). Walks the exception's
    ``__cause__``/``__context__`` chain and each link's MRO, matching class names
    against :data:`_TRANSIENT_EXC_NAMES`, and honours a ``status_code`` attribute in
    :data:`_TRANSIENT_STATUS_CODES`. Name/attribute-based so it never imports a
    provider SDK. Pure/testable."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        for klass in type(cur).__mro__:
            if klass.__name__ in _TRANSIENT_EXC_NAMES:
                return True
        code = getattr(cur, "status_code", None)
        try:
            if code is not None and int(code) in _TRANSIENT_STATUS_CODES:
                return True
        except (TypeError, ValueError):
            pass
        cur = cur.__cause__ or cur.__context__
    return False


# Human hints for exception classes whose ``str()`` is commonly empty — otherwise
# the surfaced error is just the bare class name (e.g. "httpx.ReadTimeout"), which
# is exactly what left the operator staring at "Error" with no explanation.
_EXC_HINTS = {
    "ReadTimeout": "the model took too long to respond (read timeout)",
    "ConnectTimeout": "could not reach the model endpoint in time (connect timeout)",
    "WriteTimeout": "timed out sending the request to the model (write timeout)",
    "PoolTimeout": "no free connection to the model endpoint in time (pool timeout)",
    "TimeoutException": "the model request timed out",
    "APITimeoutError": "the model request timed out",
    "ConnectError": "could not connect to the model endpoint",
    "APIConnectionError": "could not connect to the model endpoint",
    "RemoteProtocolError": "the model connection closed unexpectedly",
    "ConnectionResetError": "the model connection was reset",
    "OverloadedError": "the model provider is overloaded",
}


def describe_exception(exc: BaseException) -> str:
    """A readable one-line description of ``exc`` for the operator: a qualified
    ``module.ClassName: message``, or — when the message is empty (as it is for a
    bare ``httpx.ReadTimeout``) — the qualified class name plus a plain-language
    hint. Never returns an empty string, so a model-call failure can no longer show
    up as a lone "Error". Pure/testable."""
    name = type(exc).__name__
    module = (type(exc).__module__ or "").split(".")[0]
    label = f"{module}.{name}" if module and module != "builtins" else name
    msg = str(exc).strip()
    if msg:
        return f"{label}: {msg}"
    hint = _EXC_HINTS.get(name, "")
    return f"{label} — {hint}" if hint else label


def retry_delay(attempt: int, *, base: float = 1.0, cap: float = 15.0) -> float:
    """Backoff delay (seconds) before retry number ``attempt`` (1-based):
    exponential ``base * 2**(attempt-1)``, clamped to ``cap``. Deterministic (no
    jitter) so it's easy to test and reason about; the model loop uses it to space
    out retries of a transient model-call failure."""
    n = max(1, int(attempt))
    return float(min(cap, base * (2 ** (n - 1))))


# ── api_key_command execution ─────────────────────────────────────────────────
#
# A corporate deployment often fetches the API key from a shipped helper rather
# than storing a static key. The helper is commonly a console-script installed
# into the same venv as the console — but the venv can be created anywhere, so a
# hardcoded path into its ``Scripts/`` / ``bin/`` breaks. These functions run the
# command with the current venv made discoverable, so a config-seed can carry a
# venv-relative command that resolves wherever the venv lives.


def api_key_command_env() -> dict[str, str]:
    """Environment for running ``api_key_command``, made venv-location-independent.

    Starts from the current process environment and adds, so a key helper shipped
    alongside the console is reachable no matter where the venv was created:

      * ``PATH``           — the venv's script dir (``sysconfig`` ``scripts`` path,
        i.e. ``Scripts`` on Windows / ``bin`` on POSIX) is **prepended**, so a
        console-script installed into this venv (e.g. ``get-key``) resolves by
        bare name — the seed can ship ``api_key_command = "get-key"``.
      * ``RUNSPEC_PYTHON`` — ``sys.executable`` (this venv's interpreter), for a
        module helper: ``api_key_command = '"$RUNSPEC_PYTHON" -m yourpkg.getkey'``
        (``%RUNSPEC_PYTHON%`` under ``cmd``).
      * ``RUNSPEC_VENV``   — ``sys.prefix`` (the venv root), for anything that
        needs to locate other bundled files relative to the install.
    """
    env = dict(os.environ)
    scripts = sysconfig.get_path("scripts") or os.path.dirname(sys.executable)
    if scripts:
        env["PATH"] = scripts + os.pathsep + env.get("PATH", "")
    env["RUNSPEC_PYTHON"] = sys.executable
    env["RUNSPEC_VENV"] = sys.prefix
    return env


def run_api_key_command(command: str) -> str:
    """Run ``api_key_command`` via the shell and return its stripped stdout (the key).

    Executed with :func:`api_key_command_env`, so a helper shipped into the venv
    resolves wherever the venv was created. Raises ``RuntimeError`` (exit code +
    stderr) when the command produces no output — the shared error every adapter
    surfaced individually before.
    """
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        shell=True,
        env=api_key_command_env(),
    )
    key = result.stdout.strip()
    if not key:
        raise RuntimeError(
            f"api_key_command returned no output (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return key


class ModelAdapter(ABC):
    @abstractmethod
    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ChatResponse: ...

    @abstractmethod
    def stream_chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[str]:
        """Yield text tokens as they arrive from the model."""
        ...

    async def stream_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ):
        """
        Yield ('text', str) for each text token, then ('done', ChatResponse) at the end.

        Default falls back to non-streaming chat(). Override for true streaming with tools.
        """
        response = await self.chat(messages, tools)
        if response.text:
            yield ("text", response.text)
        yield ("done", response)

    @abstractmethod
    def make_tool_turn(
        self, response: ChatResponse, results: list[tuple[ToolCall, str]]
    ) -> list[dict[str, Any]]:
        """Return [assistant_turn, tool_result_turn] to append to the conversation."""
        ...

    def capabilities(self) -> ModelCapabilities:
        """What the model behind this adapter can do — see :class:`ModelCapabilities`.

        The default reads the ``[llm.model_limits]`` table the console forwards as a
        ``model_limits`` constructor kwarg (stored as ``self._model_limits``) and
        matches it against ``self.model``. An adapter with a richer source (e.g. the
        Anthropic Models API) overrides this, falling back to the table. Cheap and
        side-effect-free by default so the console can call it per turn."""
        return capabilities_from_limits(
            getattr(self, "model", "") or "",
            getattr(self, "_model_limits", None),
        )


def _mark_last_message_block(messages: list[Any]) -> list[Any] | None:
    """Return a copy of ``messages`` with an ephemeral ``cache_control`` breakpoint
    on the **last content block of the last message**, or ``None`` when there's
    nothing to mark. This is the *moving* breakpoint that caches the growing
    conversation prefix (the prior turns + their tool results) each turn, on top of
    the fixed tools/system breakpoints.

    Copies only the last message (and its block list) — the earlier history and the
    caller's shared objects are never mutated. A string content becomes a single
    cached text block; a block list has its last **dict** block marked (an assistant
    turn's raw SDK block objects — e.g. ``TextBlock`` — are left untouched, since
    cache_control must ride a plain dict; in the loop the last message is always the
    user tool-result turn, whose blocks are dicts)."""
    if not messages:
        return None
    out = list(messages)
    last = dict(out[-1]) if isinstance(out[-1], dict) else None
    if last is None:
        return None
    content = last.get("content")
    if isinstance(content, str):
        if not content:
            return None
        last["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content:
        blocks = list(content)
        marked = False
        for i in range(len(blocks) - 1, -1, -1):
            if isinstance(blocks[i], dict):
                blocks[i] = {**blocks[i], "cache_control": {"type": "ephemeral"}}
                marked = True
                break
        if not marked:
            return None
        last["content"] = blocks
    else:
        return None
    out[-1] = last
    return out


def apply_prompt_caching(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Add ephemeral cache_control breakpoints to the prompt prefix.

    Prompt caching is a *prefix match* in render order ``tools → system →
    messages``: a breakpoint on the last tool caches the whole (large, static)
    tools block, and turning ``system`` into a cached text block caches
    tools+system together. The tools array is rebuilt from the same discovered
    runnables each turn, so it's byte-stable within a session — subsequent turns
    read the prefix at ~0.1x instead of full price.

    A third, *moving* breakpoint marks the **last content block of the last
    message** (see :func:`_mark_last_message_block`), so the growing conversation —
    the prior turns and their tool results — is cached across the multi-step tool
    loop too, not just the static tools+system header. Anthropic allows up to four
    breakpoints; this uses three (last tool, system, last message). Below the
    model's minimum cacheable prefix (~2K tokens on Sonnet 4.6, ~4K on Opus/Haiku)
    the API silently skips caching with no error, so this is always safe to apply.

    Pure and SDK-free: takes the messages.create()/stream() kwargs dict, returns
    it with caching applied. Copies the tool dicts / the last message it annotates
    rather than mutating the caller's shared tool list or conversation history.
    """
    tools = kwargs.get("tools")
    if tools:
        annotated = [dict(t) for t in tools]
        annotated[-1] = {**annotated[-1], "cache_control": {"type": "ephemeral"}}
        kwargs["tools"] = annotated
    system = kwargs.get("system")
    if isinstance(system, str) and system.strip():
        kwargs["system"] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
    messages = kwargs.get("messages")
    if isinstance(messages, list) and messages:
        marked = _mark_last_message_block(messages)
        if marked is not None:
            kwargs["messages"] = marked
    return kwargs


# ── reasoning effort → adaptive extended thinking ─────────────────────────────
#
# `[llm] effort` sets how hard the model thinks before it answers — Anthropic
# "extended thinking". The modern (Claude 4.6+) request shape is *adaptive*: the
# model self-manages its own thinking depth per request, and `output_config.effort`
# is the dial. We send `thinking={"type": "adaptive"}` plus
# `output_config={"effort": <level>}`; `off` (default) / empty sends no thinking
# param at all.
#
# This replaces the deprecated `thinking={"type": "enabled", "budget_tokens": N}`
# shape, which is *rejected with a 400* on Opus 4.8/4.7, Sonnet 5, and Fable 5 (and
# deprecated on Opus/Sonnet 4.6). Adaptive thinking counts toward `max_tokens` (the
# output ceiling), so there's no separate budget to reserve — `max_tokens` is left
# exactly as the operator set it. Applies to the anthropic + bedrock adapters (both
# talk the Anthropic SDK, so the request shape is identical).

# Levels output_config.effort accepts, mapped straight from the named `[llm] effort`
# dial. "xhigh" needs Opus 4.7+; "max" is the deepest. A level the chosen model
# doesn't support (e.g. xhigh on Sonnet 4.6) surfaces as an API error rather than
# being silently downgraded here.
_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


def effort_level(effort: str) -> str:
    """Normalise an ``[llm] effort`` value to the ``output_config.effort`` enum
    ("low"/"medium"/"high"/"xhigh"/"max"). Returns ``""`` (thinking off) for
    ``"off"``/empty/unknown, so a typo fails safe to the no-thinking path.
    Pure/testable."""
    level = str(effort or "").strip().lower()
    return level if level in _EFFORT_LEVELS else ""


def apply_thinking(kwargs: dict[str, Any], effort: str) -> dict[str, Any]:
    """Enable Anthropic *adaptive* extended thinking at the given ``effort`` level
    on a messages.create()/stream() kwargs dict — ``thinking={"type": "adaptive"}``
    plus ``output_config={"effort": <level>}``. No-op when ``effort`` normalises to
    empty (so ``effort = off`` changes nothing). ``max_tokens`` is left untouched:
    adaptive thinking counts toward it, so the operator's ceiling already covers it.
    Pure and SDK-free — mutates and returns the kwargs dict."""
    level = effort_level(effort)
    if level:
        kwargs["thinking"] = {"type": "adaptive"}
        output_config = dict(kwargs.get("output_config") or {})
        output_config["effort"] = level
        kwargs["output_config"] = output_config
    return kwargs


# ── adapter registry + plugin discovery ───────────────────────────────────────
#
# Providers are resolved from three sources, in order:
#
#   1. dotted path  — provider = "my_pkg.module:AdapterClass" (no packaging
#      needed; the class is imported and called directly).
#   2. registry     — names passed to register_adapter() (built-ins register
#      themselves below; plugin packages may call it from an imported module,
#      e.g. one listed in `[plugins] modules`).
#   3. entry points — distributions advertising the `runspec_console.adapters`
#      group; installing the wheel is all that's needed (the recommended path
#      for proprietary in-house adapters that live outside this repo).
#
# A factory is any Callable[..., ModelAdapter] (an adapter class works directly).
# It receives the same kwargs Bridge._get_adapter threads from the [llm] config.

AdapterFactory = Callable[..., ModelAdapter]

ENTRY_POINT_GROUP = "runspec_console.adapters"

_REGISTRY: dict[str, AdapterFactory] = {}


def register_adapter(name: str, factory: AdapterFactory) -> None:
    """Register an adapter factory under a provider name.

    Mirrors ``runspec.register_type``. The public plugin entry point for
    in-process registration; the SDK-free ``ModelAdapter`` / ``ChatResponse`` /
    ``ToolCall`` classes are the contract a factory must satisfy.
    """
    _REGISTRY[name] = factory


def _missing_extra(extra: str) -> Callable[[ImportError], ImportError]:
    def wrap(_exc: ImportError) -> ImportError:
        return ImportError(
            f"Install the {extra} extra: pip install runspec-console[{extra}]"
        )

    return wrap


def _anthropic_factory(**kwargs: Any) -> ModelAdapter:
    try:
        from .anthropic import AnthropicAdapter
    except ImportError as exc:
        raise _missing_extra("anthropic")(exc) from exc
    return AnthropicAdapter(**kwargs)


def _openai_factory(**kwargs: Any) -> ModelAdapter:
    try:
        from .openai import OpenAIAdapter
    except ImportError as exc:
        raise _missing_extra("openai")(exc) from exc
    return OpenAIAdapter(**kwargs)


def _bedrock_factory(**kwargs: Any) -> ModelAdapter:
    try:
        from .bedrock import BedrockAdapter
    except ImportError as exc:
        raise _missing_extra("bedrock")(exc) from exc
    return BedrockAdapter(**kwargs)


def _langserve_factory(**kwargs: Any) -> ModelAdapter:
    # httpx is imported lazily inside __init__, so a missing extra surfaces at
    # construction (a ValueError for bad config propagates unchanged).
    from .langserve import LangServeAdapter

    try:
        return LangServeAdapter(**kwargs)
    except ImportError as exc:
        raise _missing_extra("langserve")(exc) from exc


register_adapter("anthropic", _anthropic_factory)
register_adapter("openai", _openai_factory)
register_adapter("bedrock", _bedrock_factory)
register_adapter("langserve", _langserve_factory)

# runspec-console's own providers. A provider outside this set means an external
# distribution supplies it — the signal branding.py uses to white-label the app
# to that package. Kept as a static constant (not ``set(_REGISTRY)``) so a plugin
# that self-registers via register_adapter is still treated as brandable.
_BUILTIN_NAMES = frozenset({"anthropic", "openai", "bedrock", "langserve"})


def builtin_provider_names() -> frozenset[str]:
    """The provider names that ship with runspec-console itself."""
    return _BUILTIN_NAMES


def _entry_point_factories() -> dict[str, AdapterFactory]:
    """Adapter factories advertised by installed distributions, by provider name."""
    import importlib.metadata as meta

    found: dict[str, AdapterFactory] = {}
    try:
        eps = meta.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:
        return found
    for ep in eps:
        found.setdefault(ep.name, ep.load)  # ep.load() returns the factory lazily
    return found


def available_providers() -> list[str]:
    """All resolvable provider names — built-ins, registered, and entry points.

    Powers the Settings dropdown so installed plugin providers appear there too.
    """
    names = set(_REGISTRY) | set(_entry_point_factories())
    return sorted(names)


def import_plugin_modules(modules: list[str]) -> None:
    """Import modules so their register_adapter() side effects run.

    For the ``[plugins] modules = [...]`` config path — packages that
    self-register on import rather than (or in addition to) advertising an
    entry point. Import errors are surfaced to stderr, not fatal.
    """
    import importlib

    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - defensive
            import sys

            sys.stderr.write(
                f"runspec-console: failed to import plugin module {name!r}: {exc}\n"
            )


def _resolve_factory(provider: str) -> AdapterFactory:
    """Resolve a provider name/path to its (uninstantiated) factory callable.

    Mirrors load_adapter's three-source resolution — dotted path, explicit
    registry, then entry points — and raises the same ValueErrors for an
    unloadable dotted path or an unknown provider. Split out so signature
    inspection (adapter_config_keys) can reach the factory without building it.
    """
    # 1. dotted path — "package.module:attr"
    if ":" in provider:
        import importlib

        module_path, _, attr = provider.partition(":")
        try:
            module = importlib.import_module(module_path)
            return getattr(module, attr)
        except (ImportError, AttributeError) as exc:
            raise ValueError(
                f"Could not load adapter from path {provider!r}: {exc}"
            ) from exc

    # 2. explicit registry (built-ins + plugins that called register_adapter)
    factory = _REGISTRY.get(provider)
    if factory is None:
        # 3. entry-point discovery (installed plugin distributions)
        ep = _entry_point_factories().get(provider)
        if ep is not None:
            factory = ep()  # resolve the EntryPoint.load callable

    if factory is None:
        available = ", ".join(available_providers())
        raise ValueError(
            f"Unknown model provider: {provider!r}. Available: {available}. "
            "Plugins may add more via the 'runspec_console.adapters' entry-point "
            "group or a 'module:Class' provider path."
        )
    return factory


def adapter_config_keys(provider: str) -> frozenset[str]:
    """Named constructor parameters the provider's adapter factory declares.

    Lets Bridge forward extra ``[llm]`` config keys to plugin adapters by
    signature inspection: a config key matching a declared parameter is threaded
    through, so a custom adapter can require its own config without
    runspec-console maintaining a per-key allowlist.

    Best-effort — returns an empty set when the factory can't be resolved or
    introspected. A built-in (or any ``**kwargs``) factory declares no named
    parameters, so nothing extra is forwarded for those; their keys are handled
    by Bridge's explicit allowlist regardless.
    """
    import inspect

    try:
        factory = _resolve_factory(provider)
        sig = inspect.signature(factory)
    except (ValueError, TypeError):
        return frozenset()
    return frozenset(
        name
        for name, param in sig.parameters.items()
        if name != "self"
        and param.kind
        not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
    )


def load_adapter(provider: str, **kwargs: Any) -> ModelAdapter:
    """
    Instantiate the named adapter from the registry, an entry point, or a dotted
    path. Raises ImportError with install instructions when a built-in's optional
    extra is missing, or ValueError for an unknown provider.

    provider: a built-in ("anthropic" | "openai" | "bedrock" | "langserve"), a
              name registered by a plugin, or a "module:ClassOrFactory" path.
    kwargs:   passed straight through to the adapter factory / __init__.
    """
    return _resolve_factory(provider)(**kwargs)
