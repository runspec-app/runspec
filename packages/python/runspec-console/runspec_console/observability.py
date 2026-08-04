"""
observability.py — opt-in OpenTelemetry (OTLP) tracing of the agent.

Exports one connected trace per agent turn — ``turn → llm request → tool call``
— over OTLP to *any* OpenTelemetry backend (LangSmith, Langfuse, Phoenix,
Jaeger, Grafana Tempo). Spans follow the OpenTelemetry GenAI semantic
conventions (``gen_ai.*``) so a GenAI-aware backend renders the model calls and
tool calls natively; runspec-specific detail (autonomy decision, provenance,
cache-token split) rides under a ``runspec.*`` namespace.

Design mirrors ``trace.py``: **opt-in, config/env-gated, best-effort, never
raises into the agent loop.** Every span helper is a complete no-op when
telemetry is disabled or the OpenTelemetry SDK isn't installed, so the
orchestration code calls the helpers unconditionally — no ``if enabled`` checks
scattered through the bridge. ``opentelemetry`` is a **lazy import behind the
``otel`` extra**: importing this module never drags the SDK into a base install
(same discipline as ``sources/schedule.py``'s lazy ``apscheduler``).

Enable it via ``[telemetry] enabled = true`` in ``config.toml`` (see
``config.read_telemetry`` / ``docs/console-telemetry.md``). Standard ``OTEL_*``
env vars are honoured for any value not set in ``[telemetry]`` — config wins,
env fills the gaps.

Privacy: with ``capture_content = false`` (the default) spans carry structure
only — never prompt / completion / tool-argument *text*, which can contain
secrets. When ``capture_content`` is on, tool arguments are scrubbed
(``scrub_args``) before being attached, and resolved credentials never reach a
span (they live below the tool-input layer the agent sees).
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# ── module state, set once by init_tracing (all None/False = disabled no-op) ──
_TRACER: Any = None
_PROVIDER: Any = None
_ENABLED: bool = False
_CAPTURE_CONTENT: bool = False

# Attribute-value caps so a span never carries an unbounded blob even with
# capture_content on. Long strings are truncated with a marker.
_MAX_ATTR_CHARS = 4000

# Argument keys whose value is replaced with «redacted» when capture_content is
# on. A defence-in-depth net over the console's own secret handling: prompts and
# tool inputs can still name a secret even though resolved credentials never
# reach this layer.
_SECRET_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "private_key",
)

_REDACTED = "«redacted»"


def available() -> tuple[bool, str]:
    """``(True, "")`` when the OpenTelemetry SDK + OTLP-http exporter import;
    otherwise ``(False, reason)``. Pure probe (mirrors ``sources/schedule.py``);
    drives the Settings-tab "install the extra" hint. Never raises."""
    try:
        import opentelemetry.sdk.trace  # noqa: F401
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: F401
            OTLPSpanExporter,
        )
    except Exception:  # noqa: BLE001 — any import failure ⇒ unavailable
        return (
            False,
            "OpenTelemetry not installed — pip install runspec-console[otel]",
        )
    return (True, "")


def _resolve_headers(cfg: dict[str, Any]) -> dict[str, str]:
    """Static ``headers`` plus, when ``api_key_command`` is set, the API key
    resolved from that command's stdout injected under ``api_key_header``
    (default ``x-api-key``).

    This is the "ship the command, not the key" path: a config seed / git-sync
    distributes the *command* (``keyring get …`` / ``vault read …`` / a bundled
    venv helper), and each install resolves the real key locally at startup — the
    secret never lives in the config file or package data. Reuses the same runner
    the ``[llm]`` adapters use (venv-aware; raises on empty output), so a command
    failure propagates and cleanly disables tracing rather than exporting
    unauthenticated."""
    headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
    cmd = cfg.get("api_key_command")
    if cmd:
        from .adapters.base import run_api_key_command

        key = run_api_key_command(cmd)
        headers[cfg.get("api_key_header") or "x-api-key"] = key
    return headers


def _build_exporter(cfg: dict[str, Any]) -> Any:
    """Construct the OTLP span exporter from ``[telemetry]`` config.

    Only pass a constructor arg when the corresponding config value is set, so an
    unset value falls through to the OTel SDK's own ``OTEL_*`` env-var reading
    (config wins, env fills the gaps). ``ca_bundle`` is the recommended knob for a
    private CA; ``tls_verify = false`` is honoured best-effort on the exporter's
    session (the proto-http exporter has no public insecure flag)."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    kwargs: dict[str, Any] = {}
    if cfg.get("endpoint"):
        kwargs["endpoint"] = cfg["endpoint"]
    headers = _resolve_headers(cfg)
    if headers:
        kwargs["headers"] = headers
    if cfg.get("ca_bundle"):
        kwargs["certificate_file"] = cfg["ca_bundle"]
    exporter = OTLPSpanExporter(**kwargs)
    if cfg.get("tls_verify") is False:
        # No public "insecure" flag on the proto-http exporter — reach into its
        # requests.Session best-effort. Guarded so an SDK internals change can't
        # break init.
        try:  # pragma: no cover - depends on exporter internals
            exporter._session.verify = False  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("telemetry: could not disable TLS verification on exporter")
    return exporter


def init_tracing(config: dict[str, Any] | None = None) -> bool:
    """Best-effort, idempotent one-time init. ``config`` is a
    ``config.read_telemetry()`` dict (read here if omitted).

    Returns ``True`` when tracing is now active. Returns ``False`` and leaves the
    module a complete no-op when telemetry is disabled, the SDK is missing, or any
    setup step fails — the failure is logged once, never raised into the caller."""
    global _TRACER, _PROVIDER, _ENABLED, _CAPTURE_CONTENT
    if _TRACER is not None:
        return True  # already initialised
    if config is None:
        from . import config as config_mod

        config = config_mod.read_telemetry()
    if not config.get("enabled"):
        return False
    ok, reason = available()
    if not ok:
        logger.warning("telemetry enabled but %s", reason)
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import (
            ParentBased,
            TraceIdRatioBased,
        )

        resource = Resource.create(
            {"service.name": config.get("service_name") or "runspec-console"}
        )
        ratio = float(config.get("sample_ratio", 1.0))
        sampler = ParentBased(TraceIdRatioBased(ratio))
        provider = TracerProvider(resource=resource, sampler=sampler)
        provider.add_span_processor(BatchSpanProcessor(_build_exporter(config)))

        _PROVIDER = provider
        _TRACER = provider.get_tracer("runspec-console")
        _ENABLED = True
        _CAPTURE_CONTENT = bool(config.get("capture_content", False))
        logger.info(
            "telemetry: OTLP tracing enabled (endpoint=%s, capture_content=%s)",
            config.get("endpoint") or "<env/default>",
            _CAPTURE_CONTENT,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — degrade to disabled, never raise
        logger.warning("telemetry: init failed, tracing disabled: %s", exc)
        _TRACER = None
        _PROVIDER = None
        _ENABLED = False
        return False


def reconfigure(config: dict[str, Any] | None = None) -> bool:
    """Tear down any live tracing provider and re-init from ``config``.

    ``init_tracing`` is deliberately idempotent (it returns early once a tracer
    exists), so it can't switch endpoints on its own. ``reconfigure`` is the seam
    for doing exactly that at runtime — re-pointing the OTLP exporter at a
    different endpoint / config without a restart, e.g. to route to a transient
    backend without persisting to ``config.toml``.

    Best-effort and never raises: flushes + shuts down the current provider,
    resets to the disabled no-op state, then re-inits. Returns whatever
    ``init_tracing`` returns (``True`` when tracing is active afterwards)."""
    global _TRACER, _PROVIDER, _ENABLED, _CAPTURE_CONTENT
    try:
        shutdown()
    except Exception:  # noqa: BLE001 — teardown must never raise into the caller
        pass
    _TRACER = None
    _PROVIDER = None
    _ENABLED = False
    _CAPTURE_CONTENT = False
    return init_tracing(config)


def init_tracing_for_test(exporter: Any, *, capture_content: bool = False) -> None:
    """Wire an injected span exporter (e.g. ``InMemorySpanExporter``) with a
    ``SimpleSpanProcessor`` — the network-free test seam. Resets prior state."""
    global _TRACER, _PROVIDER, _ENABLED, _CAPTURE_CONTENT
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _PROVIDER = provider
    _TRACER = provider.get_tracer("runspec-console-test")
    _ENABLED = True
    _CAPTURE_CONTENT = capture_content


def reset_for_test() -> None:
    """Tear the module back down to the disabled no-op state (test cleanup)."""
    global _TRACER, _PROVIDER, _ENABLED, _CAPTURE_CONTENT
    shutdown()
    _TRACER = None
    _PROVIDER = None
    _ENABLED = False
    _CAPTURE_CONTENT = False


def enabled() -> bool:
    """True when tracing is active (init succeeded and not shut down)."""
    return _ENABLED and _TRACER is not None


def capture_content() -> bool:
    """True when prompt / completion / tool-argument content may be attached."""
    return _CAPTURE_CONTENT


def shutdown() -> None:
    """Flush + shut down the provider so a short headless / schedule run doesn't
    drop batched spans. Best-effort; safe to call when disabled."""
    provider = _PROVIDER
    if provider is None:
        return
    try:
        provider.force_flush()
    except Exception:  # noqa: BLE001
        pass
    try:
        provider.shutdown()
    except Exception:  # noqa: BLE001
        pass


def flush() -> bool:
    """Force-flush batched spans to the backend **without** shutting the provider
    down, so a just-emitted span (e.g. the test trace) lands immediately instead
    of waiting for the batch interval — and tracing keeps working afterwards
    (unlike ``shutdown``). Best-effort; ``False`` when there's no live provider."""
    provider = _PROVIDER
    if provider is None:
        return False
    try:
        return bool(provider.force_flush())
    except Exception:  # noqa: BLE001
        return False


def send_test_trace() -> dict[str, Any]:
    """Emit one representative ``turn → llm request → tool call`` trace using the
    real span helpers, then :func:`flush` it so it reaches the backend right away.

    This is the "does my telemetry actually work" probe: it exercises the exact
    span shape and ``gen_ai.*`` / ``runspec.*`` attributes a live agent turn
    produces, and — when :func:`capture_content` is on — attaches sample prompt /
    completion / tool-argument text (including a secret-looking key, to show the
    scrubbing) so content rendering can be verified too. Returns a summary dict
    (``ok``, ``trace_id``, span names, ``capture_content``, ``flushed``) for the
    UI; ``ok=False`` with a ``reason`` when tracing is not active."""
    if not enabled():
        return {"ok": False, "reason": "tracing is not active"}
    from types import SimpleNamespace

    trace_id = ""
    try:
        handle = begin_turn("test", "telemetry-test", model="runspec-test-model")
        try:
            ctx = handle.span.get_span_context()
            trace_id = format(ctx.trace_id, "032x")
        except Exception:  # noqa: BLE001 — id is decorative, never fail the probe
            pass
        with llm_span(provider="runspec-console", model="runspec-test-model") as _llm:
            add_prompt(
                [
                    {
                        "role": "user",
                        "content": "runspec-console telemetry test — is this pipeline working?",
                    }
                ]
            )
            set_llm_response(
                _llm,
                SimpleNamespace(stop_reason="stop"),
                {"input": 42, "output": 17, "cache_read": 8},
            )
            add_completion(
                "Sample completion from the runspec-console telemetry test trace."
            )
        with tool_span(
            "run_runnable",
            "telemetry-test",
            decision="run",
            tool_call_id="telemetry-test-call",
        ) as _tool:
            add_tool_arguments(
                _tool,
                {
                    "host": "local",
                    "runnable": "greeter",
                    "name": "world",
                    # capture_content scrubs this to «redacted» — verifies scrubbing
                    "password": "hunter2",
                },
            )
            set_tool_exit(_tool, "hello world\n")
            set_tool_decision(_tool, "approved")
        end_turn(handle, usage={"input": 42, "output": 17})
    except Exception as exc:  # noqa: BLE001 — surface the failure, don't raise
        return {"ok": False, "reason": f"emit failed: {exc}"}
    flushed = flush()
    return {
        "ok": True,
        "trace_id": trace_id,
        "spans": [
            "runspec.agent.turn",
            "chat runspec-test-model",
            "execute_tool run_runnable",
        ],
        "capture_content": _CAPTURE_CONTENT,
        "flushed": flushed,
    }


# ── span context managers — yield None (and do nothing) when disabled ─────────


@contextmanager
def turn_span(
    kind: str,
    chat_id: str,
    *,
    model: str = "",
    provenance: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Root span for one agent turn (chat / trigger / schedule)."""
    if not enabled():
        yield None
        return
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "runspec.turn.kind": kind,
        "runspec.chat_id": chat_id,
    }
    if model:
        attrs["gen_ai.request.model"] = model
    attrs.update(_provenance_attrs(provenance))
    with _TRACER.start_as_current_span("runspec.agent.turn", attributes=attrs) as span:
        try:
            yield span
        except Exception as exc:  # noqa: BLE001
            record_exception(span, exc)
            raise


class _TurnHandle:
    """Opaque handle for a manually-scoped turn span (see ``begin_turn``)."""

    __slots__ = ("span", "token")

    def __init__(self, span: Any, token: Any) -> None:
        self.span = span
        self.token = token


def begin_turn(
    kind: str,
    chat_id: str,
    *,
    model: str = "",
    provenance: dict[str, Any] | None = None,
) -> Any:
    """Start the root turn span and make it the current OTel span **without** a
    ``with`` block, so a long method body needn't be re-indented. The span stays
    current for the rest of this thread/task (each agent turn runs on its own
    thread), so child ``llm_span`` / ``tool_span`` calls parent to it. Pair with
    ``end_turn``. Returns ``None`` (a no-op handle) when tracing is off."""
    if not enabled():
        return None
    try:
        from opentelemetry import context as _otel_context
        from opentelemetry import trace as _otel_trace

        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "runspec.turn.kind": kind,
            "runspec.chat_id": chat_id,
        }
        if model:
            attrs["gen_ai.request.model"] = model
        attrs.update(_provenance_attrs(provenance))
        span = _TRACER.start_span("runspec.agent.turn", attributes=attrs)
        token = _otel_context.attach(_otel_trace.set_span_in_context(span))
        return _TurnHandle(span, token)
    except Exception:  # noqa: BLE001
        return None


def end_turn(
    handle: Any,
    *,
    usage: dict[str, int] | None = None,
    exc: BaseException | None = None,
) -> None:
    """Close a ``begin_turn`` span: set turn-total usage, record an exception if
    any, detach the context, end the span. Best-effort; safe on a None handle."""
    if handle is None:
        return
    try:
        if usage is not None:
            set_turn_usage(handle.span, usage)
        if exc is not None:
            record_exception(handle.span, exc)
    finally:
        try:
            from opentelemetry import context as _otel_context

            _otel_context.detach(handle.token)
        except Exception:  # noqa: BLE001
            pass
        try:
            handle.span.end()
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def llm_span(*, provider: str = "", model: str = "") -> Iterator[Any]:
    """Child span for one model request (wraps ``adapter.stream_with_tools``)."""
    if not enabled():
        yield None
        return
    name = f"chat {model}".strip()
    attrs: dict[str, Any] = {"gen_ai.operation.name": "chat"}
    if provider:
        attrs["gen_ai.provider.name"] = provider
    if model:
        attrs["gen_ai.request.model"] = model
    with _TRACER.start_as_current_span(name or "chat", attributes=attrs) as span:
        try:
            yield span
        except Exception as exc:  # noqa: BLE001
            record_exception(span, exc)
            raise


@contextmanager
def tool_span(
    tool_name: str,
    chat_id: str,
    *,
    decision: str = "",
    tool_call_id: str = "",
    provenance: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Child span for one tool call. Created in the async gate *before* the
    ``asyncio.to_thread`` dispatch so the copied context carries it into the
    worker thread and the tool stays parented to this turn's trace."""
    if not enabled():
        yield None
        return
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": tool_name,
        "runspec.chat_id": chat_id,
    }
    if decision:
        attrs["runspec.tool.autonomy_decision"] = decision
    if tool_call_id:
        attrs["gen_ai.tool.call.id"] = tool_call_id
    attrs.update(_provenance_attrs(provenance))
    with _TRACER.start_as_current_span(
        f"execute_tool {tool_name}", attributes=attrs
    ) as span:
        try:
            yield span
        except Exception as exc:  # noqa: BLE001
            record_exception(span, exc)
            raise


def _provenance_attrs(provenance: dict[str, Any] | None) -> dict[str, Any]:
    """Map a ``_run_origin`` record → ``runspec.provenance.*`` span attributes."""
    if not provenance:
        return {}
    out: dict[str, Any] = {}
    if provenance.get("trigger"):
        out["runspec.provenance.trigger"] = str(provenance["trigger"])
    out["runspec.provenance.agent"] = bool(provenance.get("agent"))
    if provenance.get("runbook"):
        out["runspec.provenance.runbook"] = str(provenance["runbook"])
    return out


# ── attribute setters — guard on span is None ─────────────────────────────────


def set_llm_response(span: Any, response: Any, usage: dict[str, int]) -> None:
    """Set model-response attributes on an LLM span from a ``ChatResponse`` and a
    ``_usage_from_response`` dict. GenAI ``gen_ai.usage.*`` for prompt/completion
    tokens; the cache split rides ``runspec.usage.*`` (no GenAI standard key)."""
    if span is None:
        return
    try:
        stop = getattr(response, "stop_reason", None)
        if stop:
            span.set_attribute("gen_ai.response.finish_reasons", [str(stop)])
        span.set_attribute("gen_ai.usage.input_tokens", int(usage.get("input", 0)))
        span.set_attribute("gen_ai.usage.output_tokens", int(usage.get("output", 0)))
        if usage.get("cache_read"):
            span.set_attribute(
                "runspec.usage.cache_read_tokens", int(usage["cache_read"])
            )
        if usage.get("cache_creation"):
            span.set_attribute(
                "runspec.usage.cache_creation_tokens", int(usage["cache_creation"])
            )
    except Exception:  # noqa: BLE001 — attributes are decorative, never break the turn
        pass


def set_turn_usage(span: Any, usage: dict[str, int]) -> None:
    """Set turn-total token usage on the root span (summed across iterations)."""
    if span is None:
        return
    try:
        span.set_attribute("gen_ai.usage.input_tokens", int(usage.get("input", 0)))
        span.set_attribute("gen_ai.usage.output_tokens", int(usage.get("output", 0)))
    except Exception:  # noqa: BLE001
        pass


def set_tool_exit(span: Any, output: str) -> None:
    """Record a tool's result size / truncation on its span (never the text)."""
    if span is None:
        return
    try:
        span.set_attribute("runspec.tool.output_chars", len(output or ""))
        from . import bridge_utils as _utils

        span.set_attribute(
            "runspec.tool.truncated", bool(_utils.parse_truncation(output or ""))
        )
    except Exception:  # noqa: BLE001
        pass


def set_tool_decision(span: Any, decision: str) -> None:
    """Record the operator's confirm-gate decision (approved/edited/denied)."""
    if span is None or not decision:
        return
    try:
        span.set_attribute("runspec.tool.operator_decision", str(decision))
    except Exception:  # noqa: BLE001
        pass


def record_exception(span: Any, exc: BaseException) -> None:
    """Mark a span errored and attach the exception. Best-effort."""
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, str(exc)))
    except Exception:  # noqa: BLE001
        pass


# ── public enrichment API (also used by custom adapters) ──────────────────────


def current_span() -> Any | None:
    """The currently-active OTel span, or ``None`` when tracing is off. A custom
    adapter can use this (or the helpers below) to enrich the live LLM span with
    provider-specific detail — the LLM span is held current across the adapter's
    ``stream_with_tools`` call, so no plumbing is needed."""
    if not enabled():
        return None
    try:
        from opentelemetry import trace as _otel_trace

        span = _otel_trace.get_current_span()
        # A non-recording (default) span means no span is active.
        if span is None or not span.is_recording():
            return None
        return span
    except Exception:  # noqa: BLE001
        return None


def set_attributes(**attrs: Any) -> None:
    """Set attributes on the currently-active span. No-op when tracing is off, so
    an adapter can call it unconditionally without importing opentelemetry."""
    span = current_span()
    if span is None:
        return
    try:
        for key, value in attrs.items():
            span.set_attribute(key, _attr_value(value))
    except Exception:  # noqa: BLE001
        pass


def add_prompt(messages: Any) -> None:
    """Attach the outbound message list to the active LLM span as
    ``gen_ai.prompt`` — **only when capture_content is on** (privacy). Serialised
    via ``trace.jsonsafe_messages`` so SDK content blocks don't leak raw reprs."""
    if not _CAPTURE_CONTENT:
        return
    span = current_span()
    if span is None:
        return
    try:
        from .trace import jsonsafe_messages

        safe = jsonsafe_messages(messages) if isinstance(messages, list) else messages
        span.set_attribute(
            "gen_ai.prompt",
            _truncate(json.dumps(safe, default=str, ensure_ascii=False)),
        )
    except Exception:  # noqa: BLE001
        pass


def add_completion(text: str) -> None:
    """Attach the model's completion text to the active LLM span as
    ``gen_ai.completion`` — **only when capture_content is on** (privacy)."""
    if not _CAPTURE_CONTENT or not text:
        return
    span = current_span()
    if span is None:
        return
    try:
        span.set_attribute("gen_ai.completion", _truncate(str(text)))
    except Exception:  # noqa: BLE001
        pass


def add_tool_arguments(span: Any, tool_input: Any) -> None:
    """Attach a tool call's arguments to its span as ``gen_ai.tool.call.arguments``
    — **only when capture_content is on**, and **scrubbed** first so a
    secret-looking value never leaves the machine in cleartext."""
    if span is None or not _CAPTURE_CONTENT:
        return
    try:
        scrubbed = (
            scrub_args(tool_input) if isinstance(tool_input, dict) else tool_input
        )
        span.set_attribute(
            "gen_ai.tool.call.arguments",
            _truncate(json.dumps(scrubbed, default=str, ensure_ascii=False)),
        )
    except Exception:  # noqa: BLE001
        pass


# ── cross-thread context propagation helpers ──────────────────────────────────
#
# asyncio.to_thread copies the current context (which carries the active OTel
# span), so a span created in the async layer before a to_thread dispatch is
# already carried into the worker thread — no manual attach needed on the current
# code paths. These helpers exist for the paths that do NOT get that copy: the
# grouped-fanout SYNC path and any future bare-thread / custom-executor dispatch.


def capture_context() -> Any:
    """Snapshot the current OTel context to hand to a worker thread. ``None`` when
    tracing is off."""
    if not enabled():
        return None
    try:
        from opentelemetry import context as _otel_context

        return _otel_context.get_current()
    except Exception:  # noqa: BLE001
        return None


@contextmanager
def reattach(ctx: Any) -> Iterator[None]:
    """Re-attach a captured context inside a worker thread that did NOT inherit
    it (a bare thread / custom executor). No-op when ``ctx`` is None / tracing
    off."""
    if ctx is None or not enabled():
        yield
        return
    from opentelemetry import context as _otel_context

    token = _otel_context.attach(ctx)
    try:
        yield
    finally:
        try:
            _otel_context.detach(token)
        except Exception:  # noqa: BLE001
            pass


# ── scrubbing / value helpers ─────────────────────────────────────────────────


def scrub_args(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a tool-input dict with secret-looking values redacted and
    long strings capped. Used before attaching ``gen_ai.tool.call.arguments``
    under capture_content. Pure / testable."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if _is_secret_key(key):
            out[key] = _REDACTED
        elif isinstance(value, dict):
            out[key] = scrub_args(value)
        elif isinstance(value, str) and len(value) > _MAX_ATTR_CHARS:
            out[key] = value[:_MAX_ATTR_CHARS] + "…[truncated]"
        else:
            out[key] = value
    return out


def _is_secret_key(key: str) -> bool:
    k = str(key).lower()
    return any(hint in k for hint in _SECRET_KEY_HINTS)


def _truncate(text: str) -> str:
    if len(text) > _MAX_ATTR_CHARS:
        return text[:_MAX_ATTR_CHARS] + "…[truncated]"
    return text


def _attr_value(value: Any) -> Any:
    """OTel attribute values must be a primitive or a homogeneous sequence of
    primitives — coerce anything else to a (truncated) string."""
    if isinstance(value, (str, bool, int, float)):
        return _truncate(value) if isinstance(value, str) else value
    if isinstance(value, (list, tuple)) and all(
        isinstance(v, (str, bool, int, float)) for v in value
    ):
        return list(value)
    return _truncate(str(value))
