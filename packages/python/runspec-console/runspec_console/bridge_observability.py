"""bridge_observability.py — ObservabilityMixin: manage a local Phoenix server.

Thin JS-API wrappers over ``phoenix.PhoenixServer`` (constructed on the Bridge in
``__init__``). Starting local Phoenix also live-routes the console's OTLP span
exporter to the local server (via ``observability.reconfigure``) so agent turns
show up in the embedded trace UI with zero config; stopping restores the saved
``[telemetry]`` route. Methods are composed onto Bridge via mixin inheritance and
run on the full Bridge instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .bridge_const import logger

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


class ObservabilityMixin:
    def phoenix_status(self) -> dict[str, Any]:
        """Whether Phoenix is installed and, if launched, its URL/port.

        ``available`` False (with ``reason``) means the ``phoenix`` extra isn't
        installed — the tab shows the install hint. ``running``/``url``/``port``
        report a live console-managed (or adopted) server."""
        return self._phoenix_server.status()

    def phoenix_start(self) -> dict[str, Any]:
        """Launch the local Phoenix server (idempotent) and route agent traces to
        it. Returns the resulting status dict."""
        from . import config as config_mod

        status = self._phoenix_server.start(config_mod.read_phoenix())
        if status.get("running"):
            self._route_traces_to_phoenix(int(status.get("port") or 0))
        self._dispatch("runspec:phoenix_changed", status)
        return status

    def phoenix_stop(self) -> dict[str, Any]:
        """Stop the console-managed Phoenix server and restore the saved
        ``[telemetry]`` trace route. Returns the resulting status dict."""
        status = self._phoenix_server.stop()
        self._restore_saved_traces()
        self._dispatch("runspec:phoenix_changed", status)
        return status

    def phoenix_open(self) -> dict[str, Any]:
        """Open the running Phoenix UI in the operator's default browser — the
        fallback when the embedded iframe can't render (framing blocked). Opens the
        server's own known URL (never a caller-supplied one), http(s) only."""
        url = self._phoenix_server.url()
        ok = False
        if url.startswith(("http://", "https://")):
            import webbrowser

            try:
                ok = bool(webbrowser.open(url, new=2))
            except Exception:  # noqa: BLE001
                logger.warning("phoenix: could not open %s in browser", url)
        return {"ok": ok, "url": url}

    # ── internals ─────────────────────────────────────────────────────────────

    def _route_traces_to_phoenix(self, port: int) -> None:
        """Live-route the OTLP exporter to the local Phoenix (loopback, no auth),
        overlaying only ``enabled`` + ``endpoint`` onto the saved telemetry config
        so service name / sampling / capture_content carry over. Not persisted —
        a transient route for the duration of the local session."""
        if not port:
            return
        from . import config as config_mod
        from . import observability
        from . import phoenix as phoenix_mod

        cfg = config_mod.read_telemetry()
        cfg.update(
            {
                "enabled": True,
                "endpoint": phoenix_mod.traces_endpoint(port),
                # Loopback to a local collector — no auth, no private CA.
                "headers": {},
                "api_key_command": None,
                "ca_bundle": None,
                "tls_verify": True,
            }
        )
        try:
            observability.reconfigure(cfg)
        except Exception:  # noqa: BLE001 — telemetry must never break the tab
            logger.warning(
                "phoenix: could not route traces to local server", exc_info=True
            )

    def _restore_saved_traces(self) -> None:
        """Re-init tracing from the persisted ``[telemetry]`` config (or leave it a
        no-op when telemetry is off) after a local Phoenix session ends."""
        from . import config as config_mod
        from . import observability

        try:
            observability.reconfigure(config_mod.read_telemetry())
        except Exception:  # noqa: BLE001
            logger.warning(
                "phoenix: could not restore saved trace route", exc_info=True
            )
