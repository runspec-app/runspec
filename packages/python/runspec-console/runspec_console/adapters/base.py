"""
base.py — ModelAdapter ABC shared across all LLM providers.

Identical contract to runspec-chat's adapter.py so implementations
can be ported between the two packages without changes.
"""

from __future__ import annotations

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
    stop_reason: str  # "tool_use" | "end_turn" | "stop"
    _raw: Any = field(repr=False, default=None)


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


def apply_prompt_caching(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Add ephemeral cache_control breakpoints to the static prompt prefix.

    Prompt caching is a *prefix match* in render order ``tools → system →
    messages``: a breakpoint on the last tool caches the whole (large, static)
    tools block, and turning ``system`` into a cached text block caches
    tools+system together. The tools array is rebuilt from the same discovered
    runnables each turn, so it's byte-stable within a session — subsequent turns
    read the prefix at ~0.1x instead of full price. Below the model's minimum
    cacheable prefix (~2K tokens on Sonnet 4.6, ~4K on Opus/Haiku) the API
    silently skips caching with no error, so this is always safe to apply.

    Pure and SDK-free: takes the messages.create()/stream() kwargs dict, returns
    it with caching applied. Copies the tool dicts it annotates rather than
    mutating the caller's shared tool list.
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


def load_adapter(provider: str, **kwargs: Any) -> ModelAdapter:
    """
    Instantiate the named adapter from the registry, an entry point, or a dotted
    path. Raises ImportError with install instructions when a built-in's optional
    extra is missing, or ValueError for an unknown provider.

    provider: a built-in ("anthropic" | "openai" | "bedrock" | "langserve"), a
              name registered by a plugin, or a "module:ClassOrFactory" path.
    kwargs:   passed straight through to the adapter factory / __init__.
    """
    # 1. dotted path — "package.module:attr"
    if ":" in provider:
        import importlib

        module_path, _, attr = provider.partition(":")
        try:
            module = importlib.import_module(module_path)
            factory = getattr(module, attr)
        except (ImportError, AttributeError) as exc:
            raise ValueError(
                f"Could not load adapter from path {provider!r}: {exc}"
            ) from exc
        return factory(**kwargs)

    # 2. explicit registry (built-ins + plugins that called register_adapter)
    factory = _REGISTRY.get(provider)
    if factory is None:
        # 3. entry-point discovery (installed plugin distributions)
        factory = _entry_point_factories().get(provider)
        if factory is not None:
            factory = factory()  # resolve the EntryPoint.load callable

    if factory is None:
        available = ", ".join(available_providers())
        raise ValueError(
            f"Unknown LLM provider: {provider!r}. Available: {available}. "
            "Plugins may add more via the 'runspec_console.adapters' entry-point "
            "group or a 'module:Class' provider path."
        )
    return factory(**kwargs)
