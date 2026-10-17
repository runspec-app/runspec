"""
observability.py — opt-in OpenTelemetry (OTLP) tracing + metrics of the agent.

Exports one connected trace per agent turn — ``turn → llm request → tool call``
— over OTLP to *any* OpenTelemetry backend (LangSmith, Langfuse, Phoenix,
Jaeger, Grafana Tempo).

It also exports **metrics** as a *separate* OTLP signal (see the "metrics"
section below): per-tool-run and per-turn counters/histograms
(``runspec.tool.runs`` / ``runspec.tool.duration`` / ``runspec.agent.turns`` /
``runspec.agent.turn.duration`` / ``runspec.agent.turn.tokens``) pushed on an
interval by a ``PeriodicExportingMetricReader``. Metrics have their **own
endpoint + auth** (``[telemetry.metrics]``) and enable independently of traces,
so a deployment can send traces to a trace backend (e.g. LangSmith, which
ingests OTLP traces but not metrics) and metrics to a metrics/OTLP backend (a
Grafana/Prometheus OTLP endpoint or a local collector).

Spans carry **two overlapping convention sets on purpose**, because the popular
backends disagree on which they read:

* **OpenTelemetry GenAI** (``gen_ai.*``) — LangSmith, Arize AX, and generic OTel
  backends read this: operation name, model/provider, prompt/completion, token
  usage, finish reason, tool name + arguments.
* **OpenInference** (``openinference.span.kind`` + ``llm.*`` / ``tool.*`` /
  ``input.value`` / ``output.value``) — **Arize Phoenix reads this and does NOT
  understand ``gen_ai.*``**, so without it every span renders as an unknown kind
  with no LLM/tool card. The two sets coexist on each span (the standard
  dual-emit pattern), so one exporter serves both without a per-backend build.

The turn is an ``AGENT`` span (GenAI ``invoke_agent``), the model call an ``LLM``
span (GenAI ``chat``), the tool call a ``TOOL`` span (GenAI ``execute_tool``).
runspec-specific detail (autonomy decision, provenance, cache-token split, and
the ``runspec.eval.*`` verdict) rides under a ``runspec.*`` namespace.

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

import contextvars
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# ── module state, set once by init_tracing (all None/False = disabled no-op) ──
_TRACER: Any = None
_PROVIDER: Any = None
_ENABLED: bool = False
_CAPTURE_CONTENT: bool = False
# Optional ceiling on each captured *content* attribute (prompt / completion /
# tool arguments). 0 = no limit (the default) — capture the full text so a trace
# is faithful, not a slice. Set from [telemetry] max_content_chars.
_MAX_CONTENT_CHARS: int = 0

# Cap for *structured* attribute values set via set_attributes / _attr_value and
# for any single scrubbed tool-argument string — these are meant to be short
# attributes, not the big content blobs, so they stay bounded regardless.
_MAX_ATTR_CHARS = 4000

# The current turn's chat id, stamped as OpenInference ``session.id`` on every
# span of the turn so Phoenix / Arize group a conversation's traces into one
# session. Set by begin_turn, reset by end_turn; a ContextVar so it follows the
# turn into ``asyncio.to_thread`` tool dispatch like the OTel context does.
_SESSION_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "runspec_session_id", default=""
)

# Outcome of the most recent span export (see _RecordingExporter): None until a
# batch has been exported. Lets the "Send test trace" button report what the
# backend actually answered, not just that the flush finished.
_LAST_EXPORT: dict[str, Any] | None = None
# The same for metric exports (see _record_metric_exports).
_LAST_METRIC_EXPORT: dict[str, Any] | None = None

# ── metrics state, set once by init_metrics (all None/False = disabled no-op) ──
# Metrics are a SEPARATE OTLP signal from traces, with their own MeterProvider,
# exporter, endpoint and auth (config `[telemetry.metrics]`). They can be enabled
# independently and pointed at a different backend (e.g. traces → LangSmith,
# metrics → a Grafana/Prometheus OTLP endpoint). Every recording helper is a
# complete no-op when metrics are off, mirroring the trace contract.
_METER_PROVIDER: Any = None
_METER: Any = None
_METRICS_ENABLED: bool = False
# Instruments, created once in _create_instruments.
_TOOL_RUNS: Any = None
_TOOL_DURATION: Any = None
_TURN_RUNS: Any = None
_TURN_DURATION: Any = None
_TURN_TOKENS: Any = None

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

    return _RecordingExporter(_construct_exporter(OTLPSpanExporter, cfg, "traces"))


class _ExportLogCapture(logging.Handler):
    """Keeps the last warning/error the OTLP exporter logged (e.g. ``Failed to
    export span batch code: 401, reason: …``) so a failed export can say *why*."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.last: tuple[float, str] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.last = (record.created, record.getMessage())
        except Exception:  # noqa: BLE001
            pass


_EXPORT_LOG = _ExportLogCapture()
_EXPORT_LOGGER_NAME = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
_METRIC_EXPORT_LOG = _ExportLogCapture()
_METRIC_EXPORT_LOGGER_NAME = "opentelemetry.exporter.otlp.proto.http.metric_exporter"


class _RecordingExporter:
    """Span exporter wrapper that records each export's outcome in
    ``_LAST_EXPORT``. The OTLP exporter reports a rejected batch (bad auth, wrong
    endpoint) only as a log line and a FAILURE result, which a force-flush
    swallows; this keeps that outcome for :func:`last_export`."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        exporter_logger = logging.getLogger(_EXPORT_LOGGER_NAME)
        if _EXPORT_LOG not in exporter_logger.handlers:
            exporter_logger.addHandler(_EXPORT_LOG)

    def export(self, spans: Any) -> Any:
        global _LAST_EXPORT
        started = time.time()
        try:
            result = self._inner.export(spans)
        except Exception as exc:  # noqa: BLE001 — record, then let the SDK see it
            _LAST_EXPORT = {"ok": False, "error": str(exc), "spans": len(spans)}
            raise
        ok = getattr(result, "name", "") == "SUCCESS"
        error = ""
        last = _EXPORT_LOG.last
        if not ok and last is not None and last[0] >= started:
            error = last[1]
        _LAST_EXPORT = {"ok": ok, "error": error, "spans": len(spans)}
        return result

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._inner.force_flush(timeout_millis))


def last_export() -> dict[str, Any] | None:
    """``{"ok", "error", "spans"}`` for the most recent span export, or ``None``
    when nothing has been exported since tracing (re)started."""
    return dict(_LAST_EXPORT) if _LAST_EXPORT is not None else None


def _build_metric_exporter(cfg: dict[str, Any]) -> Any:
    """Construct the OTLP **metric** exporter from a ``[telemetry.metrics]`` config
    dict (see ``config._telemetry_metrics_config``).

    Same endpoint / headers / CA / TLS knobs and the same "unset ⇒ fall through to
    the SDK's OTEL_* env reading" discipline as :func:`_build_exporter` — only the
    exporter class differs (and it ships in the same
    ``opentelemetry-exporter-otlp-proto-http`` wheel, so no new dependency). The
    endpoint here is the metrics URL (``…/v1/metrics``), which is why it is a
    separate config value from the traces endpoint."""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )

    return _record_metric_exports(
        _construct_exporter(OTLPMetricExporter, cfg, "metrics")
    )


def _record_metric_exports(exporter: Any) -> Any:
    """Wrap the metric exporter's ``export`` in place so each export's outcome
    lands in ``_LAST_METRIC_EXPORT`` (the metrics twin of
    :class:`_RecordingExporter`). In place rather than a wrapper object because
    the metric reader reads the exporter's temporality / aggregation settings."""
    exporter_logger = logging.getLogger(_METRIC_EXPORT_LOGGER_NAME)
    if _METRIC_EXPORT_LOG not in exporter_logger.handlers:
        exporter_logger.addHandler(_METRIC_EXPORT_LOG)
    inner = exporter.export

    def export(metrics_data: Any, *args: Any, **kwargs: Any) -> Any:
        global _LAST_METRIC_EXPORT
        started = time.time()
        try:
            result = inner(metrics_data, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — record, then let the SDK see it
            _LAST_METRIC_EXPORT = {"ok": False, "error": str(exc)}
            raise
        ok = getattr(result, "name", "") == "SUCCESS"
        error = ""
        last = _METRIC_EXPORT_LOG.last
        if not ok and last is not None and last[0] >= started:
            error = last[1]
        _LAST_METRIC_EXPORT = {"ok": ok, "error": error}
        return result

    exporter.export = export
    return exporter


def last_metric_export() -> dict[str, Any] | None:
    """``{"ok", "error"}`` for the most recent metric export, or ``None``."""
    return dict(_LAST_METRIC_EXPORT) if _LAST_METRIC_EXPORT is not None else None


def _signal_endpoint(endpoint: str, signal: str) -> str:
    """Add ``/v1/<signal>`` to an OTLP *base* URL. The exporters POST to the
    configured endpoint exactly as given, so a base URL such as Grafana Cloud's
    ``https://otlp-gateway-….grafana.net/otlp`` or a bare collector
    ``http://host:4318`` would 404. A URL with any other path is left alone, since
    some backends use their own paths (LangSmith's ``/otel/v1/traces``).

    Repeated slashes in the path are collapsed: a backend that redirects
    ``/otlp//v1/metrics`` to ``/otlp/v1/metrics`` (Grafana Cloud does, with a 301)
    makes the HTTP client retry as a GET, which fails with 405."""
    import re
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(endpoint)
    clean = re.sub(r"/{2,}", "/", parts.path)
    path = clean.rstrip("/")
    if path and not path.endswith("/otlp"):
        if clean == parts.path:
            return endpoint
        return urlunsplit(parts._replace(path=clean))
    return urlunsplit(parts._replace(path=f"{path}/v1/{signal}"))


def _construct_exporter(
    exporter_cls: Any, cfg: dict[str, Any], signal: str = ""
) -> Any:
    """Build an OTLP proto-http exporter (span or metric) from ``cfg``, passing a
    constructor arg only when set (so an unset value falls through to the SDK's
    own ``OTEL_*`` env reading) and honouring ``tls_verify = false`` best-effort on
    the exporter's requests session. ``signal`` (``traces`` / ``metrics``)
    completes a base endpoint URL (see :func:`_signal_endpoint`)."""
    kwargs: dict[str, Any] = {}
    if cfg.get("endpoint"):
        endpoint = str(cfg["endpoint"])
        kwargs["endpoint"] = _signal_endpoint(endpoint, signal) if signal else endpoint
    headers = _resolve_headers(cfg)
    if headers:
        kwargs["headers"] = headers
    if cfg.get("ca_bundle"):
        kwargs["certificate_file"] = cfg["ca_bundle"]
    session = _proxy_session(str(cfg.get("endpoint") or ""))
    if session is not None:
        if cfg.get("tls_verify") is False:
            session.verify = False
        kwargs["session"] = session
    exporter = exporter_cls(**kwargs)
    if cfg.get("tls_verify") is False and session is None:
        # No public "insecure" flag on the proto-http exporters — reach into their
        # requests.Session best-effort. Guarded so an SDK internals change can't
        # break init.
        try:  # pragma: no cover - depends on exporter internals
            exporter._session.verify = False  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("telemetry: could not disable TLS verification on exporter")
    return exporter


def _proxy_session(endpoint: str) -> Any:
    """A requests session routed through the console-wide ``[http]`` proxy for
    the exporter, or ``None`` in system mode (the exporter's own session reads
    ``HTTPS_PROXY`` / ``NO_PROXY`` itself)."""
    from . import netproxy

    route = netproxy.proxy_for(endpoint or "http://localhost:4318")
    if route is None:
        return None
    try:
        import requests
    except Exception:  # noqa: BLE001 — the exporter always brings requests
        return None
    session = requests.Session()
    # trust_env off in both cases: requests lets env proxies override a
    # session's own, and the route above already applied no_proxy.
    session.trust_env = False
    if route:
        session.proxies = {"http": route, "https": route}
    return session


def _hostname() -> str:
    """The console machine's hostname, or ``""`` if it can't be read."""
    try:
        import socket

        return socket.gethostname() or ""
    except Exception:  # noqa: BLE001
        return ""


def _username() -> str:
    """The OS user running the console, or ``""`` if it can't be read."""
    try:
        import getpass

        return getpass.getuser() or ""
    except Exception:  # noqa: BLE001 — getuser raises when no username resolves
        return ""


def _build_resource(config: dict[str, Any]) -> Any:
    """Build the OTel ``Resource`` shared by the tracer and meter providers.

    Every console exports its own spans and metrics, so each needs a resource
    identity that (a) names the app and (b) distinguishes this instance — otherwise
    N consoles emitting under one identical resource collide into overlapping OTLP
    time series (cumulative counters read as resets), and per-console breakdown is
    impossible. Attributes:

    * ``service.name`` — the branded app slug (``service_name``; defaults to
      :func:`config.app_slug`), so a white-label build self-identifies.
    * ``service.instance.id`` — the operator ``label`` if set, else the hostname,
      so each console (including several on one machine, once labelled) is its own
      clean time series.
    * ``host.name`` / ``process.owner`` — the console machine's hostname and OS
      user, so a fleet dashboard can group/filter by *where* and *who*.

    Best-effort: any unreadable value is simply omitted."""
    from opentelemetry.sdk.resources import Resource

    attrs: dict[str, Any] = {
        "service.name": config.get("service_name") or "runspec-console"
    }
    host = _hostname()
    if host:
        attrs["host.name"] = host
    user = _username()
    if user:
        attrs["process.owner"] = user
    attrs["service.instance.id"] = (
        str(config.get("label") or "").strip() or host or "runspec-console"
    )
    project = str(config.get("project_name") or "").strip()
    if project:
        # OpenInference project routing — Phoenix and Arize AX file traces under
        # this project (absent ⇒ their "default" project).
        attrs["openinference.project.name"] = project
    return Resource.create(attrs)


def init_tracing(config: dict[str, Any] | None = None) -> bool:
    """Best-effort, idempotent one-time init. ``config`` is a
    ``config.read_telemetry()`` dict (read here if omitted).

    Returns ``True`` when tracing is now active. Returns ``False`` and leaves the
    module a complete no-op when telemetry is disabled, the SDK is missing, or any
    setup step fails — the failure is logged once, never raised into the caller."""
    global _TRACER, _PROVIDER, _ENABLED, _CAPTURE_CONTENT, _MAX_CONTENT_CHARS
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
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import (
            ParentBased,
            TraceIdRatioBased,
        )

        resource = _build_resource(config)
        ratio = float(config.get("sample_ratio", 1.0))
        sampler = ParentBased(TraceIdRatioBased(ratio))
        provider = TracerProvider(resource=resource, sampler=sampler)
        provider.add_span_processor(BatchSpanProcessor(_build_exporter(config)))

        _PROVIDER = provider
        _TRACER = provider.get_tracer("runspec-console")
        _ENABLED = True
        _CAPTURE_CONTENT = bool(config.get("capture_content", False))
        _MAX_CONTENT_CHARS = max(0, int(config.get("max_content_chars", 0) or 0))
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
    global _TRACER, _PROVIDER, _ENABLED, _CAPTURE_CONTENT, _MAX_CONTENT_CHARS
    global _LAST_EXPORT
    try:
        shutdown()
    except Exception:  # noqa: BLE001 — teardown must never raise into the caller
        pass
    _LAST_EXPORT = None
    _TRACER = None
    _PROVIDER = None
    _ENABLED = False
    _CAPTURE_CONTENT = False
    _MAX_CONTENT_CHARS = 0
    return init_tracing(config)


def init_tracing_for_test(
    exporter: Any, *, capture_content: bool = False, max_content_chars: int = 0
) -> None:
    """Wire an injected span exporter (e.g. ``InMemorySpanExporter``) with a
    ``SimpleSpanProcessor`` — the network-free test seam. Resets prior state."""
    global _TRACER, _PROVIDER, _ENABLED, _CAPTURE_CONTENT, _MAX_CONTENT_CHARS
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _PROVIDER = provider
    _TRACER = provider.get_tracer("runspec-console-test")
    _ENABLED = True
    _CAPTURE_CONTENT = capture_content
    _MAX_CONTENT_CHARS = max(0, int(max_content_chars))


def reset_for_test() -> None:
    """Tear the module back down to the disabled no-op state (test cleanup)."""
    global _TRACER, _PROVIDER, _ENABLED, _CAPTURE_CONTENT, _MAX_CONTENT_CHARS
    global _LAST_EXPORT
    shutdown()
    _LAST_EXPORT = None
    _TRACER = None
    _PROVIDER = None
    _ENABLED = False
    _CAPTURE_CONTENT = False
    _MAX_CONTENT_CHARS = 0


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
    UI; ``ok=False`` with a ``reason`` when tracing is not active, or when the
    backend rejected the export (``export`` carries the exporter's outcome)."""
    global _LAST_EXPORT
    if not enabled():
        return {"ok": False, "reason": "tracing is not active"}
    from types import SimpleNamespace

    # Forget earlier exports so the result below reflects this trace's batch.
    _LAST_EXPORT = None

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
    export = last_export()
    if export is not None and not export["ok"]:
        return {
            "ok": False,
            "reason": (
                "the backend rejected the export"
                + (f": {export['error']}" if export["error"] else "")
                + " — check the endpoint and auth headers"
            ),
            "trace_id": trace_id,
            "export": export,
        }
    return {
        "ok": True,
        "trace_id": trace_id,
        "export": export,
        "spans": [
            "runspec.agent.turn",
            "chat runspec-test-model",
            "execute_tool run_runnable",
        ],
        "capture_content": _CAPTURE_CONTENT,
        "flushed": flushed,
    }


# ── metrics: MeterProvider + OTLP export (separate signal from traces) ────────
#
# Same opt-in / best-effort / never-raises / no-op-when-disabled contract as the
# tracing above. The instrument site is the shared tool chokepoint
# (bridge_autonomy._gated_run_tool_async → record_tool_run) and the turn boundary
# (bridge_invoke / bridge_triggers → record_turn). Labels are kept to a BOUNDED
# set on purpose — a high-cardinality attribute (chat_id, a run id, an ad-hoc SSH
# host) would explode the backend's time-series count, so those stay on spans, not
# metrics.


def metrics_available() -> tuple[bool, str]:
    """``(True, "")`` when the OTel SDK metrics + OTLP-http metric exporter import;
    otherwise ``(False, reason)``. The metric exporter ships in the same wheel as
    the span exporter, so this succeeds whenever :func:`available` does. Never
    raises."""
    try:
        import opentelemetry.sdk.metrics  # noqa: F401
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (  # noqa: F401
            OTLPMetricExporter,
        )
    except Exception:  # noqa: BLE001
        return (
            False,
            "OpenTelemetry not installed — pip install runspec-console[otel]",
        )
    return (True, "")


def _create_instruments(meter: Any) -> None:
    """Create the metric instruments once from ``meter`` and store them globally."""
    global _TOOL_RUNS, _TOOL_DURATION, _TURN_RUNS, _TURN_DURATION, _TURN_TOKENS
    _TOOL_RUNS = meter.create_counter(
        "runspec.tool.runs",
        unit="1",
        description="Agent tool calls executed through the autonomy gate.",
    )
    _TOOL_DURATION = meter.create_histogram(
        "runspec.tool.duration",
        unit="ms",
        description="Wall-clock duration of an agent tool call's execution.",
    )
    _TURN_RUNS = meter.create_counter(
        "runspec.agent.turns",
        unit="1",
        description="Agent turns completed (chat / trigger / schedule).",
    )
    _TURN_DURATION = meter.create_histogram(
        "runspec.agent.turn.duration",
        unit="ms",
        description="Wall-clock duration of an agent turn.",
    )
    _TURN_TOKENS = meter.create_counter(
        "runspec.agent.turn.tokens",
        unit="1",
        description="Model tokens used by agent turns (by gen_ai.token.type).",
    )


def init_metrics(config: dict[str, Any] | None = None) -> bool:
    """Best-effort, idempotent one-time metrics init. ``config`` is a
    ``config.read_telemetry()`` dict (read here if omitted); the metrics sub-config
    is ``config["metrics"]``.

    Returns ``True`` when metrics export is now active. Returns ``False`` and
    leaves every recording helper a no-op when ``[telemetry.metrics] enabled`` is
    false, the SDK is missing, or any setup step fails — logged once, never raised.
    Independent of :func:`init_tracing`: traces and metrics enable separately and
    may target different endpoints."""
    global _METER_PROVIDER, _METER, _METRICS_ENABLED
    if _METER is not None:
        return True  # already initialised
    if config is None:
        from . import config as config_mod

        config = config_mod.read_telemetry()
    mcfg = config.get("metrics") or {}
    if not mcfg.get("enabled"):
        return False
    ok, reason = metrics_available()
    if not ok:
        logger.warning("telemetry metrics enabled but %s", reason)
        return False
    try:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        # Build the resource from the top-level telemetry config (service_name +
        # label + host/user), so the meter shares the tracer's identity.
        resource = _build_resource(config)
        interval_ms = int(mcfg.get("export_interval", 60) or 60) * 1000
        reader = PeriodicExportingMetricReader(
            _build_metric_exporter(mcfg), export_interval_millis=interval_ms
        )
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        _METER_PROVIDER = provider
        _METER = provider.get_meter("runspec-console")
        _create_instruments(_METER)
        _METRICS_ENABLED = True
        logger.info(
            "telemetry: OTLP metrics enabled (endpoint=%s, interval=%ss)",
            mcfg.get("endpoint") or "<env/default>",
            mcfg.get("export_interval", 60),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — degrade to disabled, never raise
        logger.warning("telemetry: metrics init failed, metrics disabled: %s", exc)
        _METER_PROVIDER = None
        _METER = None
        _METRICS_ENABLED = False
        return False


def reconfigure_metrics(config: dict[str, Any] | None = None) -> bool:
    """Tear down any live MeterProvider and re-init from ``config`` — the runtime
    endpoint-switch seam for metrics (``init_metrics`` is idempotent, so it can't
    switch on its own). Best-effort; never raises. Mirrors :func:`reconfigure`."""
    global _METER_PROVIDER, _METER, _METRICS_ENABLED
    global _TOOL_RUNS, _TOOL_DURATION, _TURN_RUNS, _TURN_DURATION, _TURN_TOKENS
    try:
        shutdown_metrics()
    except Exception:  # noqa: BLE001
        pass
    _METER_PROVIDER = None
    _METER = None
    _METRICS_ENABLED = False
    _TOOL_RUNS = _TOOL_DURATION = _TURN_RUNS = _TURN_DURATION = _TURN_TOKENS = None
    return init_metrics(config)


def metrics_enabled() -> bool:
    """True when metrics export is active (init succeeded and not shut down)."""
    return _METRICS_ENABLED and _METER is not None


def shutdown_metrics() -> None:
    """Flush + shut down the MeterProvider so a short headless / schedule run
    doesn't drop its last metric export. Best-effort; safe to call when disabled."""
    provider = _METER_PROVIDER
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


def flush_metrics() -> bool:
    """Force-flush pending metrics to the backend **without** shutting the provider
    down (so a test metric lands immediately and metrics keep working). Best-effort;
    ``False`` when there's no live provider."""
    provider = _METER_PROVIDER
    if provider is None:
        return False
    try:
        return bool(provider.force_flush())
    except Exception:  # noqa: BLE001
        return False


def _tool_status(output: str) -> str:
    """Coarse success class for a tool result, from the console's stable leading
    markers (``✗`` error, ``⏹`` cancelled) — else ``ok``. A bounded label."""
    text = (output or "").lstrip()
    if text.startswith("✗"):
        return "error"
    if text.startswith("⏹"):
        return "cancelled"
    return "ok"


def record_tool_run(
    *,
    tool: str,
    decision: str = "",
    operator_decision: str = "",
    provenance: dict[str, Any] | None = None,
    duration_ms: float = 0.0,
    output: str = "",
    host: str = "",
    status: str = "",
) -> None:
    """Record one runnable/tool run: increment ``runspec.tool.runs`` and observe
    ``runspec.tool.duration`` (ms), labelled with a bounded attribute set. No-op
    when metrics are off, so a chokepoint calls it unconditionally. Never raises.

    Called from two disjoint execution paths (no double-count): the agent tool
    gate (``_gated_run_tool_async`` — agent runnables + built-ins, deriving status
    from the result's leading marker via ``output``) and the human/automation
    runnable path (``invoke_runnable`` — Forms / MCP-server / schedule / trigger,
    which passes an explicit ``status`` from the exit code). ``status`` wins when
    given; otherwise it's derived from ``output``."""
    if not _METRICS_ENABLED or _TOOL_RUNS is None:
        return
    try:
        attrs: dict[str, Any] = {
            "runspec.tool.name": tool,
            "runspec.tool.status": status or _tool_status(output),
        }
        if decision:
            attrs["runspec.tool.autonomy_decision"] = decision
        if operator_decision:
            attrs["runspec.tool.operator_decision"] = operator_decision
        if host:
            attrs["runspec.host"] = host
        attrs.update(_provenance_attrs(provenance))
        _TOOL_RUNS.add(1, attrs)
        _TOOL_DURATION.record(float(duration_ms), attrs)
    except Exception:  # noqa: BLE001 — metrics are best-effort, never break a turn
        pass


def record_turn(
    *,
    kind: str,
    provenance: dict[str, Any] | None = None,
    duration_ms: float = 0.0,
    usage: dict[str, int] | None = None,
    status: str = "ok",
) -> None:
    """Record one completed agent turn: increment ``runspec.agent.turns``, observe
    ``runspec.agent.turn.duration`` (ms), and add token usage to
    ``runspec.agent.turn.tokens`` split by ``gen_ai.token.type`` (input/output).
    No-op when metrics are off. Never raises."""
    if not _METRICS_ENABLED or _TURN_RUNS is None:
        return
    try:
        attrs: dict[str, Any] = {
            "runspec.turn.kind": kind,
            "runspec.turn.status": status,
        }
        attrs.update(_provenance_attrs(provenance))
        attrs.update(_disclaimer_metric_label())
        _TURN_RUNS.add(1, attrs)
        _TURN_DURATION.record(float(duration_ms), attrs)
        for key in ("input", "output"):
            count = int((usage or {}).get(key, 0) or 0)
            if count:
                _TURN_TOKENS.add(count, {**attrs, "gen_ai.token.type": key})
    except Exception:  # noqa: BLE001
        pass


def init_metrics_for_test(reader: Any) -> None:
    """Wire an injected metric reader (e.g. ``InMemoryMetricReader``) — the
    network-free test seam. Resets prior metrics state."""
    global _METER_PROVIDER, _METER, _METRICS_ENABLED
    from opentelemetry.sdk.metrics import MeterProvider

    provider = MeterProvider(metric_readers=[reader])
    _METER_PROVIDER = provider
    _METER = provider.get_meter("runspec-console-test")
    _create_instruments(_METER)
    _METRICS_ENABLED = True


def reset_metrics_for_test() -> None:
    """Tear metrics back down to the disabled no-op state (test cleanup)."""
    global _METER_PROVIDER, _METER, _METRICS_ENABLED
    global _TOOL_RUNS, _TOOL_DURATION, _TURN_RUNS, _TURN_DURATION, _TURN_TOKENS
    shutdown_metrics()
    _METER_PROVIDER = None
    _METER = None
    _METRICS_ENABLED = False
    _TOOL_RUNS = _TOOL_DURATION = _TURN_RUNS = _TURN_DURATION = _TURN_TOKENS = None


def send_test_metric() -> dict[str, Any]:
    """Emit one representative tool-run + turn metric using the real recording
    helpers, then :func:`flush_metrics` so it reaches the backend right away — the
    metrics twin of :func:`send_test_trace`. Returns a UI summary dict; ``ok=False``
    with a ``reason`` when metrics are not active."""
    global _LAST_METRIC_EXPORT
    if not metrics_enabled():
        return {"ok": False, "reason": "metrics are not active"}
    _LAST_METRIC_EXPORT = None
    try:
        record_tool_run(
            tool="run_runnable",
            decision="run",
            duration_ms=42.0,
            output="hello world\n",
            host="local",
        )
        record_turn(
            kind="test",
            duration_ms=100.0,
            usage={"input": 42, "output": 17},
            status="ok",
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"emit failed: {exc}"}
    flushed = flush_metrics()
    export = last_metric_export()
    if export is not None and not export["ok"]:
        return {
            "ok": False,
            "reason": (
                "the backend rejected the export"
                + (f": {export['error']}" if export["error"] else "")
                + " — check the endpoint (a metrics URL usually ends in"
                " /v1/metrics) and auth headers"
            ),
            "export": export,
        }
    return {
        "ok": True,
        "export": export,
        "instruments": [
            "runspec.tool.runs",
            "runspec.tool.duration",
            "runspec.agent.turns",
            "runspec.agent.turn.duration",
            "runspec.agent.turn.tokens",
        ],
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
        # GenAI (LangSmith / Arize AX / generic OTel): the turn is an agent step,
        # not the chat call itself — that's the child llm span.
        "gen_ai.operation.name": "invoke_agent",
        # OpenInference (Phoenix / Arize) — Phoenix classifies spans by this, and
        # does NOT read gen_ai.*, so without it the turn renders as an unknown span.
        "openinference.span.kind": "AGENT",
        "runspec.turn.kind": kind,
        "runspec.chat_id": chat_id,
    }
    if model:
        attrs["gen_ai.request.model"] = model
    attrs.update(_provenance_attrs(provenance))
    attrs.update(_disclaimer_attrs())
    with _TRACER.start_as_current_span("runspec.agent.turn", attributes=attrs) as span:
        try:
            yield span
        except Exception as exc:  # noqa: BLE001
            record_exception(span, exc)
            raise


class _TurnHandle:
    """Opaque handle for a manually-scoped turn span (see ``begin_turn``)."""

    __slots__ = ("span", "token", "session_token")

    def __init__(self, span: Any, token: Any, session_token: Any = None) -> None:
        self.span = span
        self.token = token
        self.session_token = session_token


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
            "gen_ai.operation.name": "invoke_agent",
            "openinference.span.kind": "AGENT",
            "runspec.turn.kind": kind,
            "runspec.chat_id": chat_id,
        }
        if chat_id:
            attrs["session.id"] = chat_id  # OpenInference session (Phoenix / Arize)
        if model:
            attrs["gen_ai.request.model"] = model
        attrs.update(_provenance_attrs(provenance))
        attrs.update(_disclaimer_attrs())
        span = _TRACER.start_span("runspec.agent.turn", attributes=attrs)
        token = _otel_context.attach(_otel_trace.set_span_in_context(span))
        return _TurnHandle(span, token, _SESSION_ID.set(chat_id or ""))
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
            if handle.session_token is not None:
                _SESSION_ID.reset(handle.session_token)
        except Exception:  # noqa: BLE001 — reset from another context
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
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "openinference.span.kind": "LLM",  # Phoenix renders this as an LLM span
    }
    if provider:
        attrs["gen_ai.provider.name"] = provider
    if model:
        attrs["gen_ai.request.model"] = model
        attrs["llm.model_name"] = model  # OpenInference model key (Phoenix)
    session = _SESSION_ID.get()
    if session:
        attrs["session.id"] = session
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
    display_name: str = "",
    command: str = "",
) -> Iterator[Any]:
    """Child span for one tool call. Created in the async gate *before* the
    ``asyncio.to_thread`` dispatch so the copied context carries it into the
    worker thread and the tool stays parented to this turn's trace.

    ``display_name`` is the real tool the operator sees when ``tool_name`` is a
    search-mode wrapper (``run_runnable``/``use_tool``) — the span is named for it
    instead of the wrapper. ``command`` is the invoked subcommand/verb, appended
    to the span name and recorded as an attribute. Both are name/structure only,
    so they are set regardless of ``capture_content`` (which gates arg *values*)."""
    if not enabled():
        yield None
        return
    # The span-facing name: the real tool + subcommand, not a wrapper. Falls back
    # to the raw tool_name so a direct tool is unchanged.
    name = display_name or tool_name
    label = f"{name} {command}".strip() if command else name
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": name,
        "openinference.span.kind": "TOOL",  # Phoenix renders this as a tool span
        "tool.name": name,  # OpenInference tool key
        "runspec.chat_id": chat_id,
    }
    session = chat_id or _SESSION_ID.get()
    if session:
        attrs["session.id"] = session
    if command:
        attrs["runspec.tool.command"] = command
    if name != tool_name:
        # Keep the wrapper visible for debugging when we renamed the span.
        attrs["runspec.tool.wrapper"] = tool_name
    if decision:
        attrs["runspec.tool.autonomy_decision"] = decision
    if tool_call_id:
        attrs["gen_ai.tool.call.id"] = tool_call_id
        attrs["tool_call.id"] = tool_call_id  # OpenInference tool-call id
    attrs.update(_provenance_attrs(provenance))
    with _TRACER.start_as_current_span(
        f"execute_tool {label}", attributes=attrs
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


# ── disclaimer enrichment ─────────────────────────────────────────────────────
#
# When telemetry is on AND an AI-use disclaimer is enabled, every agent turn's
# span/metric carries whether the operator has acknowledged the current wording.
# Enrichment only — telemetry is opt-in and is **not** the audit record (that's the
# audit log + the Windows event log). The Bridge injects a provider (a closure over
# its live disclaimer state) so this module stays decoupled and always-current; a
# missing/failing provider yields no attributes. High-cardinality detail (the
# acknowledgement timestamp) rides spans only, never a metric label.
_DISCLAIMER_PROVIDER: Any = None


def set_disclaimer_provider(provider: Any) -> None:
    """Register (or clear, with ``None``) the callable the turn span/metric helpers
    consult for disclaimer state. The provider takes no args and returns a dict
    ``{state, acknowledged_at?, version?}`` or ``None``."""
    global _DISCLAIMER_PROVIDER
    _DISCLAIMER_PROVIDER = provider


def _disclaimer_state() -> dict[str, Any] | None:
    """Best-effort read of the injected disclaimer provider; ``None`` on absence or
    any failure (never raises into a turn)."""
    provider = _DISCLAIMER_PROVIDER
    if provider is None:
        return None
    try:
        result = provider()
        return result if isinstance(result, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _disclaimer_attrs() -> dict[str, Any]:
    """Span attributes for the disclaimer state: ``runspec.disclaimer.state`` plus
    ``acknowledged_at`` / ``version`` when acknowledged. Empty when no disclaimer is
    enabled. Structure only (no prompt/tool content), so set regardless of
    ``capture_content``."""
    st = _disclaimer_state()
    if not st or not st.get("state"):
        return {}
    out: dict[str, Any] = {"runspec.disclaimer.state": str(st["state"])}
    if st.get("acknowledged_at"):
        out["runspec.disclaimer.acknowledged_at"] = str(st["acknowledged_at"])
    if st.get("version"):
        out["runspec.disclaimer.version"] = str(st["version"])
    return out


def _disclaimer_metric_label() -> dict[str, Any]:
    """The **bounded** metric label for the disclaimer state — just the tri-state
    (``acknowledged`` / ``pending`` / ``not_required``), which has 3 values. The
    unbounded acknowledgement timestamp is deliberately kept off metrics (it would
    explode the time series) and rides spans only."""
    st = _disclaimer_state()
    if not st or not st.get("state"):
        return {}
    return {"runspec.disclaimer.state": str(st["state"])}


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
        # OpenInference token keys (Phoenix reads these, not gen_ai.usage.*).
        prompt_toks = int(usage.get("input", 0))
        completion_toks = int(usage.get("output", 0))
        span.set_attribute("llm.token_count.prompt", prompt_toks)
        span.set_attribute("llm.token_count.completion", completion_toks)
        span.set_attribute("llm.token_count.total", prompt_toks + completion_toks)
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


def add_prompt(messages: Any, system: str = "") -> None:
    """Attach the outbound prompt to the active LLM span as ``gen_ai.prompt`` —
    **only when capture_content is on** (privacy). Serialised via
    ``trace.jsonsafe_messages`` so SDK content blocks don't leak raw reprs.

    ``system`` (the adapter's system prompt, when supplied) is captured as a
    leading ``system`` message, so the search-mode catalogue index — which now
    rides in the system prompt rather than the first user message — still shows
    up in the captured prompt."""
    if not _CAPTURE_CONTENT:
        return
    span = current_span()
    if span is None:
        return
    try:
        from .trace import jsonsafe_messages

        safe = jsonsafe_messages(messages) if isinstance(messages, list) else messages
        if system and isinstance(safe, list):
            safe = [{"role": "system", "content": system}, *safe]
        payload = _truncate_content(json.dumps(safe, default=str, ensure_ascii=False))
        span.set_attribute("gen_ai.prompt", payload)  # legacy/generic + LangSmith
        # OpenInference (Phoenix): the generic input value + per-message role/content.
        span.set_attribute("input.value", payload)
        span.set_attribute("input.mime_type", "application/json")
        _set_oi_messages(span, "llm.input_messages", safe)
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
        payload = _truncate_content(str(text))
        span.set_attribute("gen_ai.completion", payload)  # legacy/generic + LangSmith
        # OpenInference (Phoenix): the generic output value + one assistant message.
        span.set_attribute("output.value", payload)
        span.set_attribute("llm.output_messages.0.message.role", "assistant")
        span.set_attribute("llm.output_messages.0.message.content", payload)
    except Exception:  # noqa: BLE001
        pass


def _set_oi_messages(span: Any, prefix: str, messages: Any) -> None:
    """Best-effort per-message OpenInference attributes
    (``<prefix>.<i>.message.role`` / ``.content``) for a simple ``[{role, content}]``
    list. Skips a message whose content isn't a plain string (the full structure
    still rides ``input.value``); never raises."""
    if not isinstance(messages, list):
        return
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role:
            span.set_attribute(f"{prefix}.{i}.message.role", str(role))
        if isinstance(content, str):
            span.set_attribute(
                f"{prefix}.{i}.message.content", _truncate_content(content)
            )


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
        payload = _truncate_content(
            json.dumps(scrubbed, default=str, ensure_ascii=False)
        )
        span.set_attribute("gen_ai.tool.call.arguments", payload)  # generic + LangSmith
        # OpenInference (Phoenix): the tool-call arguments + generic input value.
        span.set_attribute("tool_call.function.arguments", payload)
        span.set_attribute("input.value", payload)
        span.set_attribute("input.mime_type", "application/json")
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


def _truncate_content(text: str) -> str:
    """Cap a captured *content* value (prompt / completion / tool arguments) at
    ``_MAX_CONTENT_CHARS``. ``0`` (the default) means no limit — the full text is
    kept so a trace is faithful, not a 4000-char slice. Only a configured ceiling
    truncates (with a marker)."""
    limit = _MAX_CONTENT_CHARS
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…[truncated]"
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
