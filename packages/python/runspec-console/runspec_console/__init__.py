"""runspec-console — desktop console for running runspec runnables via chat.

Public plugin API for custom LLM providers. An out-of-tree (e.g. proprietary,
in-house) adapter implements ``ModelAdapter`` and is made available under a
provider name in one of three ways:

  * advertise the ``runspec_console.adapters`` entry-point group (recommended —
    ``pip install`` the wheel and the provider appears), or
  * call ``register_adapter(name, factory)`` from a module imported at startup
    (e.g. one listed under ``[plugins] modules`` in the config), or
  * set ``provider = "your_pkg.module:AdapterClass"`` in the ``[llm]`` config.

``ModelAdapter`` / ``ChatResponse`` / ``ToolCall`` are dependency-free, so a
plugin depends on runspec-console only for these classes — no SDKs leak in.

``observability`` is the optional OpenTelemetry tracing side-channel: a custom
adapter can enrich the active LLM span with provider-specific detail via
``observability.set_attributes(...)`` / ``add_completion(...)`` (all no-ops when
telemetry is off or the ``otel`` extra is absent — so no hard OTel dependency
leaks into a plugin). See ``docs/console-telemetry.md``.
"""

from runspec_console import observability
from runspec_console.adapters.base import (
    ChatResponse,
    ModelAdapter,
    ToolCall,
    available_providers,
    register_adapter,
)

__all__ = [
    "ModelAdapter",
    "ChatResponse",
    "ToolCall",
    "register_adapter",
    "available_providers",
    "observability",
]
