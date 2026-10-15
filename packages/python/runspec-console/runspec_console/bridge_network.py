"""bridge_network.py — the console-wide ``[http]`` proxy (Settings → Network).

Resolves the proxy credential from the keychain and installs the settings in
:mod:`runspec_console.netproxy`, which every outbound HTTP client reads. Also the
JS API behind the Network tab: read, save and test the proxy.
"""

from __future__ import annotations

import logging
from typing import Any

from . import config as config_mod
from . import netproxy

logger = logging.getLogger(__name__)

_DEFAULT_TEST_URL = "https://pypi.org/simple/"


class NetworkMixin:
    def _http_proxy_settings(self, cfg: dict[str, Any]) -> netproxy.ProxySettings:
        """Settings for an ``[http]``-shaped dict, with its credential resolved."""
        auth: tuple[str, str] | None = None
        cid = str(cfg.get("credential") or "").strip()
        if cid:
            try:
                cred = self._mcp_credential(cid)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — a keychain problem never breaks HTTP
                logger.warning("http proxy: could not read credential %r", cid)
                cred = None
            if cred and cred.get("username"):
                auth = (str(cred["username"]), str(cred.get("secret") or ""))
            elif cred:
                logger.warning(
                    "http proxy: credential %r has no username; sending no proxy auth",
                    cid,
                )
        return netproxy.from_config(cfg, auth)

    def _apply_http_proxy(self) -> None:
        """Install the saved ``[http]`` proxy for every outbound HTTP client."""
        settings = self._http_proxy_settings(config_mod.read_http())
        netproxy.configure(settings)
        logger.info("http proxy: %s", netproxy.describe(settings))

    def get_http_proxy_config(self) -> dict[str, Any]:
        """The ``[http]`` proxy for the Network tab: ``{mode, proxy, no_proxy,
        credential, effective}``. ``mode`` is ``system`` / ``manual`` /
        ``direct``; ``effective`` is a password-free line describing what is in
        force right now."""
        cfg = config_mod.read_http()
        proxy = cfg.get("proxy") or ""
        mode = (
            "system"
            if not proxy
            else ("direct" if proxy == netproxy.DIRECT else "manual")
        )
        return {
            "mode": mode,
            "proxy": "" if mode != "manual" else proxy,
            "no_proxy": list(cfg.get("no_proxy") or []),
            "credential": cfg.get("credential") or "",
            "effective": netproxy.describe(),
        }

    @staticmethod
    def _http_table(payload: dict[str, Any]) -> dict[str, Any]:
        """Validate a Network-tab payload into an ``[http]`` table (``{}`` for
        the default system mode). Raises ``ValueError`` on bad input."""
        if not isinstance(payload, dict):
            raise ValueError("proxy settings must be an object")
        mode = str(payload.get("mode") or "system").strip().lower()
        if mode not in ("system", "manual", "direct"):
            raise ValueError("mode must be system, manual or direct")
        table: dict[str, Any] = {}
        if mode == "direct":
            table["proxy"] = netproxy.DIRECT
        elif mode == "manual":
            proxy = netproxy.normalize_proxy_url(payload.get("proxy"))
            if not proxy or proxy == netproxy.DIRECT:
                raise ValueError("enter a proxy URL, e.g. http://proxy.corp:8080")
            err = netproxy.validate_proxy_url(proxy)
            if err:
                raise ValueError(err)
            table["proxy"] = proxy
        no_proxy = netproxy.split_no_proxy(payload.get("no_proxy"))
        if no_proxy:
            table["no_proxy"] = no_proxy
        cred = str(payload.get("credential") or "").strip()
        if cred and mode == "manual":
            table["credential"] = cred
        return table

    def save_http_proxy_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate and write ``[http]`` to config.toml, apply it immediately
        (no restart), and return the fresh :meth:`get_http_proxy_config`.

        Clients built from now on use the new proxy (the cached model adapter
        is dropped so the next turn rebuilds it); an MCP server's open session
        keeps its connection until it reconnects."""
        table = self._http_table(payload)

        def _mutate(cfg: dict[str, Any]) -> None:
            if table:
                cfg["http"] = table
            else:
                cfg.pop("http", None)

        config_mod.update_config(_mutate)
        self._apply_http_proxy()
        # The model client is built once and cached; drop it so the next turn
        # builds one on the new proxy.
        if hasattr(self, "_adapter"):
            self._adapter = None
        return self.get_http_proxy_config()

    def test_http_proxy(self, payload: dict[str, Any], url: str = "") -> dict[str, Any]:
        """Fetch ``url`` (default pypi.org) through the proxy settings in
        ``payload`` — the unsaved form values — and report what happened:
        ``{ok, url, status?, route, error?}``. Nothing is saved."""
        from . import webfetch

        target = str(url or "").strip() or _DEFAULT_TEST_URL
        try:
            settings = self._http_proxy_settings(self._http_table(payload))
        except ValueError as e:
            return {"ok": False, "url": target, "route": "", "error": str(e)}
        route = netproxy.proxy_for(target, settings)
        route_text = (
            "system (environment variables)"
            if route is None
            else ("direct" if route == "" else f"via {netproxy.redact(route)}")
        )
        web = config_mod.read_web()
        try:
            res = webfetch.fetch_url(
                target,
                timeout=min(float(web.get("timeout") or 20), 20.0),
                max_bytes=4096,
                ca_bundle=web.get("ca_bundle"),
                tls_verify=bool(web.get("tls_verify", True)),
                proxy_settings=settings,
            )
        except webfetch.WebFetchError as e:
            return {"ok": False, "url": target, "route": route_text, "error": str(e)}
        return {
            "ok": True,
            "url": target,
            "route": route_text,
            "status": res.get("status"),
        }
