"""bridge_config_ssh.py — ConfigSshMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import random
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any


from . import bridge_utils as utils_mod
from . import config as config_mod
from .config import (
    read_config,
    update_config,
)

from .bridge_const import (
    DEFAULT_SYSTEM_PROMPT,
    logger,
    AI_OFF_MESSAGE,
)
from .bridge_utils import (
    DEFAULT_KEY_MARKER,
    _normalize_path,
    _public_key_from_private,
    _write_ppk_v2,
    key_marker,
    valid_key_marker,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


# Comment appended to every console-generated public key, so a rotation can
# identify (and prune) the keys *it* put in a host's authorized_keys without
# touching the operator's own keys. The live marker is ``key_marker()``
# (bridge_utils): the white-label package name — the same source that names the
# config folder — so two branded installs on one machine (each with its own key
# + config dir) prune only their *own* keys; a default install keeps the
# historic ``runspec-console``. A key line reads
# ``ssh-ed25519 <base64-blob> <marker>`` and the marker is matched as the exact
# trailing field (never a substring, so ``runspec-console`` cannot match
# ``runspec-console-corp``). Matching by the leading base64 is not safe — every
# ed25519 key shares the same ``AAAAC3NzaC1lZDI1NTE5AAAAI`` prefix (the
# algorithm + length header), so it would also match the operator's own keys.
# Do not change the default string — a host still carrying keys tagged with an
# older marker would no longer be recognised. Keys a white-label install pushed
# before the marker followed the package name carry ``runspec-console``; those
# are still pruned via the backup-blob match (``_stale_key_blobs``), so nothing
# is orphaned.
_KEY_MARKER = DEFAULT_KEY_MARKER

# Cap on how many historical (backup-derived) key blobs are threaded into a
# single push command, so the remote command stays a sane length on a console
# that has rotated dozens of times. The marker prunes the rest regardless; the
# newest backups are the ones most likely to still be sitting on a host.
_MAX_STALE_BLOBS = 50


def _key_blob(line: str) -> str:
    """The base64 key material (2nd field) of an OpenSSH public-key line — the
    stable identity of a key, independent of its trailing comment. ``""`` for a
    blank/malformed line or one carrying key options (where field 2 isn't the
    blob); runspec keys never carry options, so this identifies ours reliably."""
    parts = line.split()
    return parts[1] if len(parts) >= 2 and parts[0].startswith("ssh-") else ""


def _build_rotation_push_cmd(
    new_line: str,
    old_blob: str,
    remove_blobs: list[str],
    *,
    marker: str | None = None,
) -> str:
    """Build the per-host rotation push command: prune the console's *stale* keys
    from ``authorized_keys`` (marker-tagged, or an exact blob-match to a key the
    console generated before), then ensure the new key is present — atomically,
    keeping the operator's own keys and the current key untouched.

    Safety properties:
      * The current key (``old_blob``) and the new key are always kept, so the
        push never removes the key the live session authenticates with — no
        lockout even if the rotation later fails to commit.
      * A line is only ever *dropped* when it is plainly one of ours (marker
        comment, or blob in the console's own backup set); everything else —
        including keys carrying command/from options — is preserved verbatim.
      * Writes through a temp file in ``~/.ssh`` and only overwrites
        ``authorized_keys`` once the rebuild is non-empty (``test -s``), copying
        the bytes back (``cat >``) so the file's existing perms / owner / SELinux
        context are preserved. A failure at any step leaves the original intact.

    ``marker`` is the comment this install tags its keys with (default:
    ``key_marker()`` — the white-label package name); only lines whose *last
    field* equals it are treated as ours, so another branded install's keys
    survive. All interpolated values (base64 blobs, the marker) come from a fixed
    charset with no shell/awk metacharacters — the marker is re-validated here as
    defence in depth — so single-quoting the awk program is safe.
    """
    marker = marker or key_marker()
    if not valid_key_marker(marker):
        marker = DEFAULT_KEY_MARKER
    new_blob = _key_blob(new_line)
    keep = [b for b in (old_blob, new_blob) if b]
    begin = "".join(f'keep["{b}"]=1;' for b in keep)
    begin += "".join(f'rm["{b}"]=1;' for b in remove_blobs)
    prog = (
        "BEGIN{" + begin + "}"
        "{"
        "if(NF>=2&&($2 in keep)){print;next}"  # never drop current / new
        f'if(NF>=3&&$NF=="{marker}"){{next}}'  # drop keys tagged with OUR marker
        "if(NF>=2&&($2 in rm)){next}"  # drop keys we generated before
        "print"  # keep everything else (operator's own keys)
        "}"
    )
    return (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        'ak="$HOME/.ssh/authorized_keys" && '
        'tmp="$(mktemp "$HOME/.ssh/ak.XXXXXX" 2>/dev/null || echo "$HOME/.ssh/ak.$$")" && '
        f'awk \'{prog}\' "$ak" > "$tmp" && '
        f"{{ grep -qF '{new_blob}' \"$tmp\" || printf '%s\\n' '{new_line}' >> \"$tmp\"; }} && "
        'test -s "$tmp" && cat "$tmp" > "$ak" && rm -f "$tmp"'
    )


class ConfigSshMixin:
    def get_config(self) -> dict[str, Any]:
        return read_config()

    def config_dir(self) -> str:
        """Return the resolved config directory path (e.g. C:/Users/jason/AppData/Roaming/runspec-console)."""
        from .config import config_path

        return _normalize_path(str(config_path().parent))

    def get_mailbox(self) -> dict[str, Any] | None:
        """The single ``[mailbox]`` the Outlook source monitors (Settings →
        Triggers): ``{address, folder, label, once_per_thread}``, or ``None`` when
        unconfigured."""
        from .config import read_mailbox

        return read_mailbox()

    def save_mailbox(self, mailbox: dict[str, Any] | None) -> None:
        """Replace (or clear, when ``mailbox`` is falsy) the ``[mailbox]`` table and
        restart the trigger engine so the Outlook source re-resolves the watched
        folder. Persisting always succeeds; a restart failure (e.g. no Outlook on
        this machine) is logged, not surfaced, so the config still saves."""
        from .config import write_mailbox

        write_mailbox(mailbox or None)
        try:
            self.restart_triggers()
        except Exception:
            logger.debug("restart_triggers after mailbox save failed", exc_info=True)

    def get_watches(self) -> list[dict[str, Any]]:
        """The additional **watch-only** mailboxes (Settings → Mailbox) the Outlook
        source monitors on top of the primary ``[mailbox]``. Each is
        ``{address, folder, label}``; ``[]`` when none. These feed ``new_mail``
        triggers only — they carry no room/helpdesk or verify-from role."""
        from .config import read_watches

        return read_watches()

    def save_watches(self, watches: list[dict[str, Any]] | None) -> None:
        """Replace the ``[[watch]]`` extras and restart the trigger engine so the
        Outlook source re-resolves its watched folders. Persisting always succeeds;
        a restart failure (e.g. no Outlook on this machine) is logged, not
        surfaced, so the config still saves."""
        from .config import write_watches

        write_watches(watches or None)
        try:
            self.restart_triggers()
        except Exception:
            logger.debug("restart_triggers after watches save failed", exc_info=True)

    # ── version / docs / branding ────────────────────────────────────────────

    def _get_branding(self) -> Any:
        """The app's white-label identity, memoised (invalidated on config save).

        Derived from the active LLM plugin's distribution metadata — title, docs
        URL, and the package the update check targets all follow whichever package
        supplies the configured provider. See branding.py.
        """
        if self._branding is None:
            from .branding import resolve_branding

            self._branding = resolve_branding(self.get_config())
        return self._branding

    def app_title(self) -> str:
        """The window/app title — branded to the active LLM plugin, else default."""
        return self._get_branding().title

    def ai_enabled(self) -> bool:
        """Whether the AI/agent features are on (`[console] ai`, default true).

        Read once at bring-up; the frontend hides the AI surfaces when false and
        the backend refuses agent turns. Restart to apply a config change."""
        return bool(getattr(self, "_ai_enabled", True))

    def brand_theme(self) -> dict[str, Any]:
        """The validated `[console.theme]` for the frontend (read-only white-label).

        Returns only the keys the UI renders, each cleaned: `accent_color` (a hex
        colour → Ant `colorPrimary`), `brand_name` (sidebar title), `watermark_text`,
        `watermark_logo` (an http(s)/`data:image` URL), and `watermark_style`
        (`tiled` / `banner` / `center`). An absent or invalid value is dropped, so
        `{}` means "no theme — the default look". Source is config only (set by a
        config seed or by hand); there is no Settings editor.
        See docs/design/console-theme.md."""
        from .config import (
            theme_config,
            valid_accent,
            valid_image_ref,
            valid_watermark_style,
        )

        theme = theme_config()
        out: dict[str, Any] = {}
        accent = valid_accent(theme.get("accent_color"))
        if accent:
            out["accent_color"] = accent
        name = theme.get("brand_name")
        if isinstance(name, str) and name.strip():
            out["brand_name"] = name.strip()
        text = theme.get("watermark_text")
        if isinstance(text, str) and text.strip():
            out["watermark_text"] = text.strip()
        logo = valid_image_ref(theme.get("watermark_logo"))
        if logo:
            out["watermark_logo"] = logo
        style = valid_watermark_style(theme.get("watermark_style"))
        if style:
            out["watermark_style"] = style
        return out

    # ── first-use AI disclaimer ([console.disclaimer]) ────────────────────────
    #
    # A blocking acknowledgement notice for AI-enabled deployments that require it.
    # Off for a vanilla install and hard-off when [console] ai = false (it's an
    # AI-use notice); a config seed / Config Sync turns it on with corporate
    # wording. Read-only from config — there is no Settings editor, so an operator
    # can't click their own compliance notice off. See docs/design/console-disclaimer.md.

    def _compute_disclaimer_state(self) -> dict[str, Any]:
        """Resolve the disclaimer's live state from config + the machine-local
        acknowledgement. The single source of truth behind ``disclaimer()``,
        ``acknowledge_disclaimer()`` and the telemetry provider — kept live (no
        cache) so a Config-Sync hot-reload of the wording flips the state
        correctly. Never raises; a read failure resolves to "not required"."""
        try:
            cfg = config_mod.read_disclaimer()
            # Hard-off when AI is disabled: a non-AI console never shows it.
            enabled = bool(cfg["enabled"]) and bool(getattr(self, "_ai_enabled", True))
            current_hash = (
                config_mod.disclaimer_hash(cfg["text"], cfg["version"])
                if enabled
                else ""
            )
            ack = config_mod.read_disclaimer_ack()
            acked_hash = str(ack.get("hash") or "")
            # always_show: re-prompt every launch regardless of the stored ack.
            # The record is still kept (acknowledge_disclaimer writes it), but the
            # gate treats the operator as un-acknowledged so it re-shows. The
            # frontend's in-session decision still dismisses it for that session.
            always_show = bool(cfg.get("always_show", False))
            acknowledged = (
                enabled
                and not always_show
                and bool(acked_hash)
                and acked_hash == current_hash
            )
            if not enabled:
                state = "not_required"
            elif acknowledged:
                state = "acknowledged"
            else:
                state = "pending"
            return {
                "config": cfg,
                "enabled": enabled,
                "needs_ack": enabled and not acknowledged,
                "state": state,
                "current_hash": current_hash,
                "acknowledged_at": (
                    str(ack.get("acknowledged_at") or "") if acknowledged else ""
                ),
                "acked_version": str(ack.get("version") or "") if acknowledged else "",
            }
        except Exception:  # noqa: BLE001 — never let the gate computation raise
            logger.debug("disclaimer state computation failed", exc_info=True)
            return {
                "config": {},
                "enabled": False,
                "needs_ack": False,
                "state": "not_required",
                "current_hash": "",
                "acknowledged_at": "",
                "acked_version": "",
            }

    def disclaimer(self) -> dict[str, Any]:
        """The disclaimer popup's state for the frontend gate.

        Returns ``{enabled, needs_ack, title, text, acknowledge_label, version}``.
        The UI shows the blocking modal when ``needs_ack`` is true (enabled **and**
        the current wording hasn't been acknowledged on this machine). ``enabled``
        is false — and ``needs_ack`` false — for a vanilla install, when
        ``[console.disclaimer]`` is unset/empty, or when ``[console] ai = false``.
        Called once at mount (a local read: config + one tiny file + a hash), so it
        never touches the network and adds nothing to startup."""
        st = self._compute_disclaimer_state()
        cfg = st["config"]
        return {
            "enabled": bool(st["enabled"]),
            "needs_ack": bool(st["needs_ack"]),
            "title": cfg.get("title", "Notice"),
            "text": cfg.get("text", ""),
            "acknowledge_label": cfg.get(
                "acknowledge_label", config_mod.DEFAULT_DISCLAIMER_ACK_LABEL
            ),
            "version": cfg.get("version", ""),
        }

    def acknowledge_disclaimer(self) -> dict[str, Any]:
        """Record that this operator accepted the current disclaimer wording.

        Writes the machine-local acknowledgement (hash + version + timestamp + OS
        user), appends a line to the console audit log (always, every platform),
        and writes a Windows Application event-log entry (silent, best-effort,
        Windows-only) — see docs/design/console-disclaimer.md for why the event
        log is tamper-*evident*, not tamper-*proof*. When telemetry is configured,
        subsequent turns carry the acknowledgement on their spans/metrics via the
        provider. Returns ``{ok, needs_ack}``; a disabled disclaimer is a no-op."""
        st = self._compute_disclaimer_state()
        if not st["enabled"]:
            return {"ok": True, "needs_ack": False}
        cfg = st["config"]
        ts = utils_mod._iso_now()
        user = self._current_user()
        version = cfg.get("version", "")
        try:
            config_mod.write_disclaimer_ack(
                {
                    "hash": st["current_hash"],
                    "version": version,
                    "acknowledged_at": ts,
                    "user": user,
                }
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "could not persist disclaimer acknowledgement", exc_info=True
            )
            return {"ok": False, "needs_ack": True}
        # Audit trail (always) + Windows event log (silent, best-effort).
        try:
            self._audit_disclaimer_ack(
                hash_=st["current_hash"], version=version, user=user, ts=ts
            )
        except Exception:  # noqa: BLE001 — auditing is best-effort
            logger.debug("disclaimer audit-log write failed", exc_info=True)
        try:
            from . import eventlog

            eventlog.write_application_event(
                source=config_mod.app_slug(),
                message=(
                    f"AI-use disclaimer acknowledged by {user} "
                    f"(version={version or 'none'}, hash={st['current_hash']}) at {ts}"
                ),
            )
        except Exception:  # noqa: BLE001 — event-log write is best-effort
            logger.debug("disclaimer event-log write failed", exc_info=True)
        return {"ok": True, "needs_ack": False}

    def _disclaimer_telemetry(self) -> dict[str, Any] | None:
        """Provider for ``observability`` — the disclaimer facts to stamp on each
        agent turn's span/metric when telemetry is on. Returns ``None`` when the
        disclaimer isn't enabled (nothing to record). Enrichment only: telemetry is
        opt-in and is **not** the audit record (that's the audit log + event log).
        Never raises."""
        try:
            st = self._compute_disclaimer_state()
            if not st["enabled"]:
                return None
            out: dict[str, Any] = {"state": st["state"]}
            if st["acknowledged_at"]:
                out["acknowledged_at"] = st["acknowledged_at"]
            if st["acked_version"]:
                out["version"] = st["acked_version"]
            return out
        except Exception:  # noqa: BLE001
            return None

    def app_version(self) -> str:
        """The version shown in the footer — the **branded** package's when it
        resolves, else runspec-console's own. **Never blank.**

        A white-label wrapper points `[llm] provider` at its adapter, so branding
        targets that distribution. If that distribution's version can't be read
        via importlib.metadata (an editable/renamed install, a provider that
        can't be traced to a dist), fall back to runspec-console's version so the
        footer still shows something useful instead of going empty."""
        from .version import PACKAGE_NAME, installed_version

        try:
            package = self._get_branding().version_package or PACKAGE_NAME
        except Exception:  # noqa: BLE001 - branding must never blank the footer
            package = PACKAGE_NAME
        return installed_version(package) or installed_version(PACKAGE_NAME)

    def check_for_update(self) -> dict[str, Any]:
        """Compare the installed version against the newest one the venv's
        configured package index offers (PyPI, or an internal proxy/mirror if
        this venv's pip points at one). Best-effort.

        Targets the branded package (the distribution supplying the active LLM
        provider), so a white-labelled install checks for *its* updates, not
        runspec-console's.

        ``ok`` is False when the latest version couldn't be reached (offline /
        blocked network policy) — the UI then shows just the version with no
        update indicator. The check lives behind its own bridge method (it shells
        out to pip) so the version renders instantly while this resolves in the
        background.
        """
        from .version import installed_version, is_newer, latest_version

        package = self._get_branding().version_package
        current = installed_version(package)
        # Slow machines / internal mirrors can outrun the default pip timeout;
        # the wait is configurable ([console] update_check_timeout) so the footer
        # can still resolve to "up to date" instead of sticking on the check.
        latest = latest_version(
            package=package, timeout=config_mod.update_check_timeout()
        )
        if latest is None:
            return {
                "ok": False,
                "current": current,
                "latest": None,
                "update_available": False,
            }
        return {
            "ok": True,
            "current": current,
            "latest": latest,
            "update_available": is_newer(latest, current),
        }

    def docs_url(self) -> str:
        """The documentation URL the docs icon opens.

        ``[console] docs_url`` wins; otherwise the active LLM plugin's package
        metadata (Documentation/Homepage URL); otherwise the runspec default.
        """
        return self._get_branding().docs_url

    def open_docs(self) -> None:
        """Open the documentation site in the user's default browser."""
        try:
            webbrowser.open(self.docs_url())
        except Exception:
            logger.debug("open_docs failed", exc_info=True)

    def save_config(self, data: dict[str, Any]) -> None:
        old = self.get_config()
        ssh = data.get("ssh")
        if isinstance(ssh, dict):
            data = {**data, "ssh": utils_mod._normalize_ssh_section(ssh)}
        llm = data.get("llm")
        if isinstance(llm, dict) and isinstance(llm.get("system"), str):
            # A system prompt equal to the built-in default is not a customisation
            # — drop it so the install keeps tracking future default changes (the
            # Settings field is pre-filled with the default; a Config-Sync'd
            # config.toml carrying an old default gets the same treatment).
            if llm["system"].strip() == DEFAULT_SYSTEM_PROMPT.strip():
                data = {**data, "llm": {k: v for k, v in llm.items() if k != "system"}}
        config_mod.write_config(data)
        self._after_config_change(old, data)

    def _after_config_change(self, old: dict[str, Any], new: dict[str, Any]) -> None:
        """React to a persisted config change (shared by save_config and the
        atomic SSH-key commits): rebuild the adapter, re-apply the SSH pool, and
        re-join the room when [chat] changed or the pool was rebuilt."""
        self._adapter = None
        self._branding = None  # provider may have changed → rederive the brand
        pool_before = self._ssh_pool
        self._apply_ssh_config(old.get("ssh"), new.get("ssh"))
        if old.get("chat") != new.get("chat") or self._ssh_pool is not pool_before:
            self.restart_chat()

    def _resolve_telemetry_credentials(
        self, cfg: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return :func:`config.read_telemetry` with any keychain-backed credential
        secrets folded into the trace **and** metrics ``headers`` — so
        ``observability`` stays keyring-free and just consumes finished headers.

        Mirrors the MCP-client convention (``[[mcp.servers]] credential`` /
        ``header_credentials``): a section's ``credential`` id resolves to a secret
        header (``userpass`` ⇒ ``Authorization: Basic``; ``token`` ⇒ ``Bearer``;
        or the raw secret under ``credential_header`` — e.g. ``x-api-key`` for
        LangSmith), and ``header_credentials`` fills several named headers. An
        explicit header the operator set always wins. Because the console is a
        single-user machine app, each operator sets up their **own** API key as a
        Credential during onboarding; only the id lives in config, the secret in
        the OS keychain. Best-effort: an unresolvable id is skipped, never raised —
        the export just goes out with whatever headers did resolve."""
        from . import mcp_client

        if cfg is None:
            cfg = config_mod.read_telemetry()

        def _enrich(section: dict[str, Any]) -> None:
            headers = dict(section.get("headers") or {})
            cid = str(section.get("credential") or "").strip()
            if cid:
                try:
                    cred = self._mcp_credential(cid)
                except Exception:  # noqa: BLE001 — a lookup must never break init
                    cred = None
                if cred:
                    mcp_client.apply_credential_header(headers, section, cred)
            try:
                mcp_client.apply_header_credentials(
                    headers, section.get("header_credentials"), self._mcp_credential
                )
            except Exception:  # noqa: BLE001
                pass
            section["headers"] = headers

        out = dict(cfg)
        _enrich(out)  # traces
        metrics = dict(out.get("metrics") or {})
        _enrich(metrics)
        out["metrics"] = metrics
        return out

    def send_test_trace(self) -> dict[str, Any]:
        """Emit one representative agent-turn trace (``turn → LLM call → tool
        call``) to the configured OTLP backend and flush it immediately, so the
        operator can verify the telemetry pipeline end-to-end — connectivity *and*
        span shape/rendering — without running a real agent turn.

        Reconfigures the tracer from the currently-saved ``[telemetry]`` config
        first, so a just-saved endpoint / enable / capture-content is picked up
        without a restart. Returns a UI-friendly result dict: ``ok`` plus either a
        ``reason`` (when it can't run) or ``trace_id`` / ``spans`` / ``endpoint`` /
        ``capture_content`` / ``flushed`` (on success)."""
        from . import observability

        if not getattr(self, "_ai_enabled", True):
            return {"ok": False, "reason": AI_OFF_MESSAGE}
        ok, reason = observability.available()
        if not ok:
            return {"ok": False, "reason": reason}
        cfg = self._resolve_telemetry_credentials()
        if not cfg.get("enabled"):
            return {
                "ok": False,
                "reason": "Enable tracing and Save first, then send a test trace.",
            }
        # Re-point the live exporter at the latest saved config (endpoint /
        # headers / capture_content) without a restart — this is what reconfigure
        # exists for. A failure here means the endpoint/config is bad.
        if not observability.reconfigure(cfg):
            return {
                "ok": False,
                "reason": (
                    "Tracing failed to initialise — check the endpoint and the "
                    "console log."
                ),
            }
        result = observability.send_test_trace()
        result.setdefault("endpoint", cfg.get("endpoint") or "<OTEL_* env / default>")
        return result

    def send_test_metric(self) -> dict[str, Any]:
        """Emit one representative tool-run + turn metric to verify the **metrics**
        OTLP pipeline — the metrics twin of :meth:`send_test_trace`, for the
        Settings → Telemetry "Send test metric" button.

        Reconfigures the meter from the currently-saved ``[telemetry.metrics]``
        config first (so a just-saved endpoint / enable is picked up without a
        restart), then records + force-flushes one of each instrument. Returns a
        UI-friendly result dict: ``ok`` plus either a ``reason`` or ``instruments``
        / ``endpoint`` / ``flushed``.

        **Not** AI-gated (unlike :meth:`send_test_trace`): metrics cover runnable
        runs, so they're useful on an AI-off fleet console too."""
        from . import observability

        ok, reason = observability.metrics_available()
        if not ok:
            return {"ok": False, "reason": reason}
        cfg = self._resolve_telemetry_credentials()
        mcfg = cfg.get("metrics") or {}
        if not mcfg.get("enabled"):
            return {
                "ok": False,
                "reason": "Enable metrics and Save first, then send a test metric.",
            }
        if not observability.reconfigure_metrics(cfg):
            return {
                "ok": False,
                "reason": (
                    "Metrics failed to initialise — check the endpoint and the "
                    "console log."
                ),
            }
        result = observability.send_test_metric()
        result.setdefault("endpoint", mcfg.get("endpoint") or "<OTEL_* env / default>")
        return result

    def _commit_ssh_fields(self, **fields: Any) -> None:
        """Atomically merge ``fields`` into the ``[ssh]`` section under the config
        lock (read-modify-write can't race another Settings save), then re-apply.
        Used by the key generate/rotate paths instead of a get_config + save_config
        round-trip that could clobber a concurrent edit."""
        old = self.get_config()

        def mutate(cfg: dict[str, Any]) -> None:
            cfg["ssh"] = utils_mod._normalize_ssh_section(
                {**cfg.get("ssh", {}), **fields}
            )

        new = update_config(mutate)
        self._after_config_change(old, new)

    def _apply_ssh_config(
        self, old_ssh: dict[str, Any] | None, new_ssh: dict[str, Any] | None
    ) -> None:
        """React to an ``[ssh]`` change with the least disruption to live
        connections. The pool consumes only ``[ssh]`` (never ``[refresh]``), so
        an unrelated save (LLM, refresh tuning…) reaches here with no change and
        does nothing.

        A full rebuild — which drops every connection — happens only when
        pooling is toggled on/off or ``max_concurrent`` changes (the handshake
        semaphore can't be resized in place). Every other change is applied
        without a drop: behavioural tunables update in place, and
        connection-shaping fields (user / proxy / identity) migrate lazily via
        the connection key on next use.
        """
        if old_ssh == new_ssh:
            return
        from .ssh_pool import pool_settings

        old_s = pool_settings(old_ssh)
        new_s = pool_settings(new_ssh)
        if (old_s["enabled"], old_s["max_concurrent"]) != (
            new_s["enabled"],
            new_s["max_concurrent"],
        ):
            old_pool = self._ssh_pool
            self._ssh_pool = self._build_ssh_pool()
            if old_pool is not None:
                old_pool.close_all()
        elif self._ssh_pool is not None:
            self._ssh_pool.update_settings(new_s)

    def _generate_keypair_to_path(self, dest: Path) -> dict[str, Any]:
        """Write a new ed25519 key pair to dest (OpenSSH PEM + PPK v2). Does not touch config."""
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
            from cryptography.hazmat.primitives import serialization as _serial
        except ImportError:
            return {
                "ok": False,
                "public_key": "",
                "key_path": "",
                "message": "pip install cryptography — required for key generation",
            }
        dest.parent.mkdir(parents=True, exist_ok=True)
        private_key = Ed25519PrivateKey.generate()

        dest.write_bytes(
            private_key.private_bytes(
                encoding=_serial.Encoding.PEM,
                format=_serial.PrivateFormat.OpenSSH,
                encryption_algorithm=_serial.NoEncryption(),
            )
        )

        raw_priv = private_key.private_bytes(
            encoding=_serial.Encoding.Raw,
            format=_serial.PrivateFormat.Raw,
            encryption_algorithm=_serial.NoEncryption(),
        )
        raw_pub = private_key.public_key().public_bytes(
            encoding=_serial.Encoding.Raw,
            format=_serial.PublicFormat.Raw,
        )
        _write_ppk_v2(Path(str(dest) + ".ppk"), raw_priv, raw_pub)

        pub = (
            private_key.public_key()
            .public_bytes(
                encoding=_serial.Encoding.OpenSSH,
                format=_serial.PublicFormat.OpenSSH,
            )
            .decode()
            .strip()
        )
        # Tag the key with this install's marker so rotation can recognise and
        # prune the keys the console itself placed on a host (see _KEY_MARKER /
        # key_marker). The comment is ignored by sshd for authentication.
        marker = key_marker()
        if _key_blob(pub) and not pub.endswith(f" {marker}"):
            pub = f"{pub} {marker}"
        # Persist the public key next to the private key so it can be copied
        # again later (e.g. to authorise it on a host after the fact).
        try:
            Path(str(dest) + ".pub").write_text(pub + "\n", encoding="utf-8")
        except Exception:
            pass
        return {
            "ok": True,
            "public_key": pub,
            "key_path": _normalize_path(str(dest)),
            "message": "",
        }

    def generate_ssh_key(self) -> dict[str, Any]:
        """Generate a fresh ed25519 key pair, backing up any existing key.

        Commits to config immediately — use rotate_ssh_key() when you want the
        safe automated push to remote hosts before committing.
        """
        import time
        from datetime import timezone

        # The console's config dir (branded %APPDATA%\<package>), not a hardcoded
        # runspec-console — otherwise a white-label install writes its SSH key to a
        # stray folder separate from the rest of its state.
        key_dir = config_mod.app_dir()
        canonical = key_dir / "runspec_ed25519"

        if canonical.exists():
            backup = key_dir / f"runspec_ed25519.bak.{int(time.time())}"
            canonical.rename(backup)

        result = self._generate_keypair_to_path(canonical)
        if not result["ok"]:
            return {**result, "committed": False, "per_host": []}

        self._commit_ssh_fields(
            identityFile=_normalize_path(str(canonical)),
            key_created_at=datetime.now(timezone.utc).isoformat(),
        )

        return {
            "ok": True,
            "committed": True,
            "per_host": [],
            "public_key": result["public_key"],
            "key_path": _normalize_path(str(canonical)),
            "message": f"Key generated at {_normalize_path(str(canonical))}",
        }

    def _public_key_for(self, identity_file: str) -> dict[str, Any]:
        """Resolve the OpenSSH public key line for an identity file.

        Reads the persisted ``<identityFile>.pub`` if present; otherwise derives
        it from the private key (and writes the ``.pub`` for next time). Returns
        ``{ok, public_key, message}`` — ``ok=False`` when no key is configured
        or the private file is missing. Shared by ``get_public_key`` (global key)
        and ``copy_ssh_id`` (a host's effective key, which may be a per-host
        override).
        """
        if not identity_file:
            return {"ok": False, "public_key": "", "message": "No SSH key configured."}
        priv = Path(identity_file).expanduser()
        pub_file = Path(str(priv) + ".pub")
        if pub_file.is_file():
            return {
                "ok": True,
                "public_key": pub_file.read_text(encoding="utf-8").strip(),
                "message": "",
            }
        if not priv.is_file():
            return {
                "ok": False,
                "public_key": "",
                "message": f"Key file not found: {identity_file}",
            }
        try:
            pub = _public_key_from_private(priv)
        except Exception as exc:
            return {
                "ok": False,
                "public_key": "",
                "message": f"Could not read public key: {exc}",
            }
        try:
            pub_file.write_text(pub + "\n", encoding="utf-8")
        except Exception:
            pass
        return {"ok": True, "public_key": pub, "message": ""}

    def get_public_key(self) -> dict[str, Any]:
        """Return the current SSH public key, so it can be copied at any time.

        Reads the persisted ``<identityFile>.pub`` if present; otherwise derives
        it from the private key (and writes the ``.pub`` for next time). This is
        what lets the UI show the public key long after generation, even for
        keys created before ``.pub`` files were written.
        """
        cfg = self.get_config()
        idf = (cfg.get("ssh") or {}).get("identityFile", "")
        return self._public_key_for(idf)

    @staticmethod
    def _effective_ssh_user(entry: dict[str, Any], global_ssh: dict[str, Any]) -> str:
        """The SSH username that will actually be used for ``entry``, resolved the
        way ``_make_ssh_client`` resolves it: the ``user@`` of the host's ssh
        string, else the global ``[ssh] user``. Empty when neither is set — in
        which case paramiko falls back to the local OS login name (and, if
        ``use_ssh_config`` is enabled, ``~/.ssh/config`` may still supply one).
        Used to give a clear "no username" diagnostic instead of a misleading
        "check the password" on a copy-key auth failure."""
        ssh_str = str(entry.get("ssh") or "").strip()
        host_user = ssh_str.split("@", 1)[0].strip() if "@" in ssh_str else ""
        return host_user or str(global_ssh.get("user") or "").strip()

    def _push_key_over_password(
        self, entry: dict[str, Any], password: str, global_ssh: dict[str, Any]
    ) -> dict[str, Any]:
        """Append this machine's public key to one host's ``authorized_keys`` over
        a one-off password connection. Returns ``{ok, error}`` — it does **not**
        re-probe connectivity or dispatch events, so a bulk caller can batch those
        once at the end. Shared by ``copy_ssh_id`` (single host) and
        ``copy_ssh_id_all`` (fleet bootstrap).

        Pushes the host's *effective* key — a per-host ``identityFile`` overrides
        the global one, matching how ``_check_connected`` / ``_make_ssh_client``
        pick the key the host's own key-auth sessions will use. Pushing the wrong
        key would leave the host red even after a "successful" copy.
        """
        effective_idf = entry.get("identityFile") or global_ssh.get("identityFile", "")
        key_res = self._public_key_for(effective_idf)
        if not key_res["ok"]:
            # No key at all → point the user at Generate key (we never auto-create
            # one here; a surprise key could orphan keys other hosts authorize).
            error = (
                "No SSH key configured — generate one first, then copy it."
                if not effective_idf
                else key_res["message"]
            )
            return {"ok": False, "error": error}
        public_key = key_res["public_key"]

        # No SSH username anywhere → the connection would fall back to this
        # machine's local login name and the remote would reject it as an auth
        # failure, which reads as "wrong password" and is baffling to debug. Catch
        # it up front with an actionable message — unless ~/.ssh/config is enabled,
        # where a `User` directive may still supply one (let it try, then the
        # auth-failure handler below gives the same hint if it really was missing).
        if not self._effective_ssh_user(entry, global_ssh) and not global_ssh.get(
            "use_ssh_config"
        ):
            return {
                "ok": False,
                "error": (
                    "No SSH username is set for this host. Set one — edit the host "
                    "as user@hostname, or set a default under [ssh] user — before "
                    "copying the key."
                ),
            }

        # Same idempotent push the key-rotation path uses (grep -qF || append).
        # public_key is single-quote-wrapped; an OpenSSH key contains no quotes.
        push_cmd = (
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
            f"grep -qF '{public_key}' ~/.ssh/authorized_keys || "
            f"printf '%s\\n' '{public_key}' >> ~/.ssh/authorized_keys"
        )

        # A ``jump`` host is unreachable by paramiko (the legacy-crypto case this
        # whole feature exists for), so the password bootstrap can't go through
        # ``_make_ssh_client`` — route it over the system-ssh transport instead.
        jump = str(entry.get("jump") or "").strip()
        if jump:
            return self._push_key_over_jump(
                entry, password, global_ssh, public_key, push_cmd, jump
            )

        import paramiko

        from .executor import _make_ssh_client, friendly_ssh_error

        # Connect with the password via the shared client builder so this goes
        # through the same [ssh] proxy / host-key / ssh_config handling as every
        # other connection — essential when the whole fleet sits behind an HTTP
        # CONNECT proxy. Password auth disables agent/key fallback internally.
        try:
            client = _make_ssh_client(entry["ssh"], None, global_ssh, password=password)
        except paramiko.AuthenticationException:
            # With no username configured, the likely cause isn't the password —
            # the connection used this machine's local login name. Say so.
            if not self._effective_ssh_user(entry, global_ssh):
                return {
                    "ok": False,
                    "error": (
                        "Authentication failed, and no SSH username is set for this "
                        "host — the connection used your local login name. Set a "
                        "username (user@hostname, or a default [ssh] user) and retry."
                    ),
                }
            return {"ok": False, "error": "Authentication failed — check the password."}
        except paramiko.BadHostKeyException:
            return {
                "ok": False,
                "error": "Remote host key changed — refusing to connect.",
            }
        except Exception as exc:  # noqa: BLE001 — surface a friendly one-liner
            return {"ok": False, "error": friendly_ssh_error(exc)}

        try:
            _stdin, stdout, stderr = client.exec_command(push_cmd)
            exit_code = stdout.channel.recv_exit_status()
            err = stderr.read().decode(errors="replace")
        finally:
            client.close()

        if exit_code != 0:
            return {
                "ok": False,
                "error": (
                    "Could not update authorized_keys: "
                    f"{err.strip() or f'exit {exit_code}'}"
                ),
            }
        return {"ok": True, "error": ""}

    def _push_key_over_jump(
        self,
        entry: dict[str, Any],
        password: str,
        global_ssh: dict[str, Any],
        public_key: str,
        push_cmd: str,
        jump: str,
    ) -> dict[str, Any]:
        """Copy-key bootstrap for a ``jump`` host (paramiko can't reach it).

        With ``sshpass`` present, feed the password to system ssh over ``-J`` and
        push the key in-app. Without it — the stock-Windows case — surface the
        exact ``ssh-copy-id`` command to run once in a terminal (which uses the
        operator's ``~/.ssh/config``), instead of a confusing paramiko failure.
        """
        from . import executor as executor_mod

        if executor_mod.has_sshpass():
            code, _out, err = executor_mod.ssh_jump_copy_id(
                entry["ssh"],
                jump,
                push_cmd,
                password,
                identity_file=entry.get("identityFile"),
                global_ssh_config=global_ssh,
            )
            if code == 0:
                return {"ok": True, "error": ""}
            return {
                "ok": False,
                "error": (
                    "Could not authorise over the jump host: "
                    f"{err.strip() or f'ssh exited {code}'}"
                ),
            }

        effective_idf = entry.get("identityFile") or global_ssh.get("identityFile", "")
        manual = executor_mod.jump_copy_id_command(entry["ssh"], jump, effective_idf)
        return {
            "ok": False,
            "error": (
                "This is a jump host, reached with the system ssh client because "
                "paramiko can't negotiate its legacy crypto. Automated password "
                "copy needs `sshpass`, which isn't installed on this machine. Run "
                "this once in a terminal to authorise the console, then reconnect:"
                f"\n\n    {manual}"
            ),
        }

    def copy_ssh_id(self, host_name: str, password: str) -> dict[str, Any]:
        """Copy this machine's public key to a host's ``authorized_keys`` over a
        one-off password connection — the in-app equivalent of ``ssh-copy-id``.

        Bootstraps key-based SSH for a freshly added host: until our key is in
        the remote's ``~/.ssh/authorized_keys`` key auth fails and the host shows
        disconnected, forcing the user out to PuTTY/another client. This logs in
        once with the supplied password, appends the key (idempotently), then
        re-probes so the sidebar dot flips green. The password is used only for
        this single connection — never stored, cached, or logged.
        """
        entry = self._host_entry(host_name)
        if entry is None or not entry.get("ssh"):
            return {
                "ok": False,
                "connected": False,
                "message": f"{host_name} is not a reachable remote host",
            }
        if not (password or "").strip():
            return {
                "ok": False,
                "connected": False,
                "message": "Password required to copy the key.",
            }

        global_ssh = self.get_config().get("ssh") or {}
        res = self._push_key_over_password(entry, password, global_ssh)
        if not res["ok"]:
            return {"ok": False, "connected": False, "message": res["error"]}

        # Re-probe just this host so we can report the result now and flip the
        # dot immediately; a background full cycle refreshes its runnables too.
        connected = self._check_connected(entry)
        with self._lock:
            self._connected_cache[host_name] = connected
        self._dispatch("runspec:hosts_updated", {})
        # Targeted: forget just this host's discovery stamp, then run a normal
        # cycle — no fleet-wide re-discovery for one key push.
        self._invalidate_discovery([host_name])
        self._kick_refresh()
        return {
            "ok": True,
            "connected": connected,
            "message": (
                f"Public key copied — {host_name} is now connected."
                if connected
                else "Public key copied — verifying connection…"
            ),
        }

    def copy_ssh_id_all(
        self, password: str, targets: list[str] | None = None
    ) -> dict[str, Any]:
        """Bulk ``ssh-copy-id``: authorise this machine on every selected remote
        host over a one-off password connection.

        Built for initial fleet bootstrap — when hosts arrive from a Config-Sync
        pull none of them trust our key yet, so they all show disconnected and
        each would otherwise need the single-host dialog one at a time. One
        password is tried against every target (the common shared-bootstrap-
        credential case); the per-host results report which succeeded so the
        operator can retry the rest individually. The password is used only for
        these connections — never stored, cached, or logged.

        ``targets`` selects host names; omit it to target every configured remote
        host that isn't already connected (i.e. the ones still missing the key).
        Pushes happen sequentially, then connectivity is re-probed per host and a
        single ``hosts_updated`` event + refresh cycle flips the sidebar at the
        end.
        """
        if not (password or "").strip():
            return {
                "ok": False,
                "per_host": [],
                "message": "Password required to copy the key.",
            }

        self._reload_hosts()
        global_ssh = self.get_config().get("ssh") or {}

        # Snapshot connectivity once (same source as the green dot) to pick the
        # default target set without holding the lock across the slow SSH loop.
        with self._lock:
            connected_snapshot = dict(self._connected_cache)

        remote = [h for h in self._hosts if h.get("ssh")]
        by_name = {h.get("name"): h for h in remote}
        if targets:
            names = [n for n in targets if n in by_name]
        else:
            names = [
                h.get("name")
                for h in remote
                if not connected_snapshot.get(h.get("name"), False)
            ]

        if not names:
            return {
                "ok": False,
                "per_host": [],
                "message": "No matching remote hosts to copy the key to.",
            }

        per_host: list[dict[str, Any]] = []
        for name in names:
            entry = by_name[name]
            res = self._push_key_over_password(entry, password, global_ssh)
            if res["ok"]:
                connected = self._check_connected(entry)
                with self._lock:
                    self._connected_cache[name] = connected
                per_host.append(
                    {"host": name, "ok": True, "connected": connected, "error": ""}
                )
            else:
                per_host.append(
                    {
                        "host": name,
                        "ok": False,
                        "connected": False,
                        "error": res["error"],
                    }
                )

        self._dispatch("runspec:hosts_updated", {})
        # Targeted: only the hosts that just got the key need re-discovery.
        self._invalidate_discovery([str(n) for n in names])
        self._kick_refresh()

        ok_n = sum(1 for h in per_host if h["ok"])
        fail_n = len(per_host) - ok_n
        if ok_n and not fail_n:
            message = f"Public key copied to all {ok_n} host(s)."
        elif ok_n:
            message = (
                f"Public key copied to {ok_n} of {len(per_host)} host(s) "
                f"— {fail_n} failed."
            )
        else:
            message = f"Could not copy the key to any of the {len(per_host)} host(s)."
        return {"ok": ok_n > 0, "per_host": per_host, "message": message}

    @staticmethod
    def _cleanup_staged_rotation(new_path: Path) -> None:
        """Best-effort removal of the staged rotation key files (``.new``,
        ``.new.ppk``, ``.new.pub``). A successful commit renames them onto the
        canonical key, so this is a no-op then; on any failure return (e.g. one
        host's banner blip aborting the commit) it clears the litter that used to
        be left behind on disk."""
        for p in (new_path, Path(str(new_path) + ".ppk"), Path(str(new_path) + ".pub")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.debug(
                    "could not remove staged rotation file %s", p, exc_info=True
                )

    def rotate_ssh_key(self) -> dict[str, Any]:
        """Safely rotate the SSH key: generate new, push to all remote hosts using the
        old key, verify the new key works, then swap. Active sessions stay alive
        throughout because the old key is not replaced until all hosts are verified."""
        if getattr(self, "_rotating", False):
            return {
                "ok": False,
                "committed": False,
                "public_key": "",
                "key_path": "",
                "per_host": [],
                "message": "Rotation already in progress",
            }
        # Rotation must see the whole fleet: a scoped host profile hides its
        # out-of-profile hosts (they hold no standing connection), so those hosts
        # would be skipped, keep only the OLD key, and be locked out the moment the
        # new key commits. Only allow it under the built-in All hosts scope.
        active_profile = self._active_profile_name()
        if active_profile:
            return {
                "ok": False,
                "committed": False,
                "public_key": "",
                "key_path": "",
                "per_host": [],
                "message": (
                    f"Key rotation needs the All hosts profile active (currently "
                    f"'{active_profile}'). A scoped profile would orphan its "
                    f"out-of-profile hosts on the old key. Switch to All hosts and "
                    f"retry. (Copy key to hosts still works for a selection.)"
                ),
            }
        self._rotating = True
        try:
            return self._do_rotate_ssh_key()
        finally:
            self._rotating = False
            # Never leave staged .new* key files behind on a failed/aborted rotate.
            self._cleanup_staged_rotation(config_mod.app_dir() / "runspec_ed25519.new")

    def _rotate_emit(self, level: str, message: str) -> None:
        """Surface a rotation-progress line in the Dev tab's ``ssh`` category so a
        long fleet-wide rotation is observable while it runs (it used to emit
        nothing until it finished). Best-effort — never breaks the rotation, and
        a no-op on a bridge without the Dev-tab plumbing (unit-test stubs)."""
        emit = getattr(self, "_emit_ssh_log", None)
        if emit is None:
            return
        try:
            emit({"level": level, "logger": "ssh.rotate", "message": message})
        except Exception:
            pass

    def _rotate_progress(self, **payload: Any) -> None:
        """Dispatch a ``runspec:rotate_progress`` event so the UI can show a live
        per-host progress bar during rotation. Best-effort / stub-safe."""
        dispatch = getattr(self, "_dispatch", None)
        if dispatch is None:
            return
        try:
            dispatch("runspec:rotate_progress", payload)
        except Exception:
            pass

    def _rotate_ssh_run(
        self,
        ssh_target: str,
        command: str,
        *,
        identity_file: str | None,
        global_ssh_config: dict[str, Any],
        retries: int,
    ) -> tuple[int, str, str]:
        """``executor.ssh_run`` with a bounded retry on a *transient connection*
        failure (banner/session/timeout under a bastion throttle).

        Only a connect/exec-level failure (``ssh_run`` returns ``-1``) is retried,
        and only when the error looks transient — a command that ran and returned
        non-zero (a real push/verify failure) is returned as-is. This is what
        stops the "9 of 96, different hosts each run" transient losses: a blip on
        one host's handshake no longer becomes a hard rotation failure.
        """
        from . import executor as executor_mod

        code, out, err = -1, "", ""
        for attempt in range(retries + 1):
            code, out, err = executor_mod.ssh_run(
                ssh_target,
                command,
                identity_file=identity_file,
                global_ssh_config=global_ssh_config,
            )
            if code != -1 or attempt >= retries:
                break
            if not executor_mod.is_transient_ssh_error(err):
                break
            delay = 1.0 * (attempt + 1) + random.uniform(0, 1.0)
            self._rotate_emit(
                "DEBUG",
                f"{ssh_target}: transient SSH error ({err.strip()}); "
                f"retrying in {delay:.1f}s (attempt {attempt + 2}/{retries + 1})",
            )
            time.sleep(delay)
        return code, out, err

    def _stale_key_blobs(self, key_dir: Path, exclude: set[str]) -> list[str]:
        """Base64 blobs of keys the console generated in the past — the historical
        pile a rotation should prune from remote ``authorized_keys``.

        Sourced from the console's own backup files under ``key_dir``: the
        ``*.pub.bak.*`` public backups (read directly) and the ``*.bak.*`` private
        backups (public key derived), so it catches even the pre-marker keys that
        carry no identifying comment. ``exclude`` (the current + new blobs) is
        removed so we never list a key we must keep. Newest-first, capped at
        ``_MAX_STALE_BLOBS`` to bound the remote command length. Best-effort — an
        unreadable/undecodable backup is skipped, never fatal to a rotation."""
        blobs: list[str] = []
        seen: set[str] = set(exclude)

        def _add(blob: str) -> None:
            if blob and blob not in seen:
                seen.add(blob)
                blobs.append(blob)

        # Newest backups first (they're likeliest to still be on a host).
        def _ts(p: Path) -> str:
            return p.name.rsplit(".", 1)[-1]

        pub_baks = sorted(
            key_dir.glob("runspec_ed25519*.pub.bak.*"), key=_ts, reverse=True
        )
        for p in pub_baks:
            try:
                _add(_key_blob(p.read_text(encoding="utf-8")))
            except OSError:
                continue
            if len(blobs) >= _MAX_STALE_BLOBS:
                return blobs

        # Private backups without a matching .pub.bak — derive the public blob.
        priv_baks = sorted(
            (
                p
                for p in key_dir.glob("runspec_ed25519.bak.*")
                if p.suffix != ".ppk" and ".pub." not in p.name
            ),
            key=_ts,
            reverse=True,
        )
        for p in priv_baks:
            _add(self._derive_pub_blob(p))
            if len(blobs) >= _MAX_STALE_BLOBS:
                break
        return blobs

    def _derive_pub_blob(self, private_path: Path) -> str:
        """The base64 public blob of an OpenSSH private key file, or ``""`` if it
        can't be read/parsed (a corrupt or non-ed25519 backup is simply skipped)."""
        try:
            from cryptography.hazmat.primitives import serialization as _serial

            data = private_path.read_bytes()
            key = _serial.load_ssh_private_key(data, password=None)
            pub = (
                key.public_key()
                .public_bytes(
                    encoding=_serial.Encoding.OpenSSH,
                    format=_serial.PublicFormat.OpenSSH,
                )
                .decode()
            )
            return _key_blob(pub)
        except Exception:
            return ""

    def _rotate_push_verify_one(
        self,
        host: dict[str, Any],
        *,
        push_cmd: str,
        new_path: Path,
        global_ssh: dict[str, Any],
        global_idf: str,
        retries: int,
        jitter: float,
    ) -> dict[str, Any]:
        """Push the new public key to one host (over the old key) then verify the
        new key authenticates — each step retried on a transient blip. Returns the
        per-host result row. Safe to run concurrently across hosts.

        ``push_cmd`` is the host-independent prune-and-append command built once
        per rotation (see ``_build_rotation_push_cmd``): it installs the new key
        *and* removes the stale keys the console left on the host before, keeping
        the operator's own keys and the current key untouched."""
        if jitter:
            time.sleep(random.uniform(0, jitter))  # spread the connects

        name = host.get("name", "?")
        ssh_target = host.get("ssh", "")
        host_idf = host.get("identityFile") or global_idf
        self._rotate_emit("DEBUG", f"{name}: pushing new key")

        exit_code, _out, stderr = self._rotate_ssh_run(
            ssh_target,
            push_cmd,
            identity_file=host_idf,
            global_ssh_config=global_ssh,
            retries=retries,
        )
        if exit_code != 0:
            err = stderr.strip() or f"push failed (exit {exit_code})"
            self._rotate_emit("WARNING", f"{name}: push failed — {err}")
            return {
                "host": name,
                "pushed": False,
                "verified": False,
                "skipped": False,
                "error": err,
            }

        v_code, v_out, v_err = self._rotate_ssh_run(
            ssh_target,
            "echo ok",
            identity_file=str(new_path),
            global_ssh_config=global_ssh,
            retries=retries,
        )
        verified = v_code == 0 and "ok" in v_out
        if verified:
            self._rotate_emit("DEBUG", f"{name}: new key verified")
        else:
            self._rotate_emit(
                "WARNING",
                f"{name}: verify failed — {v_err.strip() or v_out.strip()}",
            )
        return {
            "host": name,
            "pushed": True,
            "verified": verified,
            "skipped": False,
            "error": ""
            if verified
            else (v_err.strip() or v_out.strip() or f"verify failed (exit {v_code})"),
        }

    def _do_rotate_ssh_key(self) -> dict[str, Any]:
        # The console's config dir (branded %APPDATA%\<package>), not a hardcoded
        # runspec-console — same as _regenerate_ssh_key above.
        key_dir = config_mod.app_dir()
        canonical = key_dir / "runspec_ed25519"
        new_path = key_dir / "runspec_ed25519.new"

        # Step 1 — generate to side path (old key untouched)
        result = self._generate_keypair_to_path(new_path)
        if not result["ok"]:
            return {
                "ok": False,
                "committed": False,
                "public_key": "",
                "key_path": "",
                "per_host": [],
                "message": result["message"],
            }

        public_key = result["public_key"]
        cfg = self.get_config()
        global_ssh = dict(cfg.get("ssh", {}))
        global_idf = global_ssh.get("identityFile", "")

        # Step 2 — collect push targets
        self._reload_hosts()
        remote_hosts = [h for h in self._hosts if h.get("ssh")]

        # Snapshot connection status once (same source as the green dot in the UI)
        # so the slow SSH loop below doesn't hold the lock. Every entry here has an
        # `ssh` target, so a never-probed remote defaults to disconnected (False).
        with self._lock:
            connected_snapshot = dict(self._connected_cache)

        if not remote_hosts:
            self._commit_key_rotation(canonical, new_path, global_ssh, cfg)
            return {
                "ok": True,
                "committed": True,
                "per_host": [],
                "public_key": public_key,
                "key_path": _normalize_path(str(canonical)),
                "message": "Key rotated — no remote hosts configured. Add the public key to each host's authorized_keys before reconnecting.",
            }

        if global_idf and not Path(global_idf).expanduser().exists():
            if new_path.exists():
                new_path.unlink()
            return {
                "ok": False,
                "committed": False,
                "public_key": "",
                "key_path": "",
                "per_host": [],
                "message": f"Current key file not found: {global_idf}. Restore the file or generate a fresh key.",
            }

        # Steps 3 & 4 — push then verify each host.
        #
        # Two classes up front (no SSH): hosts to *skip* (disconnected, or a
        # per-host key override) and hosts to *process*. Skips keep only the old
        # key and are surfaced in per_host + the manual snippet in the UI.
        skipped: list[dict[str, Any]] = []
        to_process: list[dict[str, Any]] = []
        for host in remote_hosts:
            name = host.get("name", "?")
            if not connected_snapshot.get(name, False):
                skipped.append(
                    {
                        "host": name,
                        "pushed": False,
                        "verified": False,
                        "skipped": True,
                        "error": "disconnected — new key not pushed; update manually",
                    }
                )
            elif host.get("identityFile") and host.get("identityFile") != global_idf:
                skipped.append(
                    {
                        "host": name,
                        "pushed": False,
                        "verified": False,
                        "skipped": True,
                        "error": "per-host key override — rotate manually",
                    }
                )
            else:
                to_process.append(host)

        total = len(to_process)
        self._rotate_emit(
            "INFO",
            f"key rotation starting — pushing new key to {total} connected host(s), "
            f"{len(skipped)} skipped",
        )
        self._rotate_progress(done=0, total=total, host="", phase="start")

        # Push + verify each host concurrently, bounded by the same handshake cap
        # the refresh cycle honours (sshd MaxStartups) rather than one-at-a-time —
        # each worker does its host's push then verify sequentially, so at most
        # ``workers`` handshakes are ever in flight. Per-host jitter (only when
        # hosts queue behind the bound) spreads the connects; a transient
        # banner/session blip on push or verify is retried before it counts as a
        # host failure. Results are merged back in the original host order.
        from concurrent.futures import ThreadPoolExecutor
        from .ssh_pool import pool_settings

        ssh_settings = pool_settings(cfg.get("ssh"))
        workers = max(1, min(ssh_settings["max_concurrent"], total or 1))
        try:
            retries = max(0, int(global_ssh.get("rotate_retries", 2)))
        except (TypeError, ValueError):
            retries = 2
        try:
            rotate_jitter = float(global_ssh.get("rotate_jitter", 3))
        except (TypeError, ValueError):
            rotate_jitter = 3.0
        jitter = rotate_jitter if total > workers else 0.0

        # Build the host-independent prune-and-append push command once. It
        # installs the new key AND removes the stale keys the console left behind
        # on earlier rotations (identified by the runspec marker comment, or an
        # exact blob-match to a key in the console's own backup files) — so
        # authorized_keys stops accumulating a new runspec key every rotation.
        # The current key (old_blob) and the new key are always kept, so the push
        # can never remove the key the live session authenticates with.
        old_blob = _key_blob(self._public_key_for(global_idf).get("public_key", ""))
        new_blob = _key_blob(public_key)
        remove_blobs = self._stale_key_blobs(
            key_dir, exclude={b for b in (old_blob, new_blob) if b}
        )
        marker = key_marker()
        push_cmd = _build_rotation_push_cmd(
            public_key, old_blob, remove_blobs, marker=marker
        )
        self._rotate_emit(
            "DEBUG", f"rotation prunes keys tagged {marker!r} on each host"
        )
        if remove_blobs:
            self._rotate_emit(
                "INFO",
                f"rotation will prune up to {len(remove_blobs)} stale runspec "
                "key(s) from each host's authorized_keys",
            )

        done_lock = threading.Lock()
        progress = {"done": 0}

        def _process(host: dict[str, Any]) -> dict[str, Any]:
            result = self._rotate_push_verify_one(
                host,
                push_cmd=push_cmd,
                new_path=new_path,
                global_ssh=global_ssh,
                global_idf=global_idf,
                retries=retries,
                jitter=jitter,
            )
            with done_lock:
                progress["done"] += 1
                done_n = progress["done"]
            self._rotate_progress(
                done=done_n,
                total=total,
                host=result["host"],
                phase="verified" if result["verified"] else "failed",
            )
            return result

        processed: list[dict[str, Any]] = []
        if to_process:
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="ssh-rotate"
            ) as ex:
                processed = list(ex.map(_process, to_process))

        # Merge skips + processed back into the input host order for a stable UI.
        by_name = {r["host"]: r for r in (skipped + processed)}
        per_host: list[dict[str, Any]] = [
            by_name[h.get("name", "?")]
            for h in remote_hosts
            if h.get("name", "?") in by_name
        ]
        verified_now = sum(1 for r in processed if r["verified"])
        self._rotate_emit(
            "INFO",
            f"key rotation push/verify complete — {verified_now}/{total} verified",
        )

        # Step 5 — commit gate: all non-skipped hosts must be verified, AND at least
        # one host must have actually been verified. The second clause prevents
        # silently committing a key that no reachable host authorizes (every remote
        # skipped because disconnected) — that would lock us out everywhere.
        any_verified = any(h["verified"] for h in per_host)
        can_commit = (
            all(h["verified"] or h["skipped"] for h in per_host) and any_verified
        )

        if not any_verified:
            return {
                "ok": True,
                "committed": False,
                "public_key": public_key,
                "key_path": "",
                "per_host": per_host,
                "message": (
                    "No connected host could be verified — old key kept. "
                    "Connect at least one host and retry, or use Generate new key "
                    "to swap manually."
                ),
            }

        if not can_commit:
            failed = [
                h["host"] for h in per_host if not h["verified"] and not h["skipped"]
            ]
            return {
                "ok": True,
                "committed": False,
                "public_key": public_key,
                "key_path": "",
                "per_host": per_host,
                "message": f"Push or verification failed on: {', '.join(failed)}. Old key still active.",
            }

        # Step 6 — commit
        self._commit_key_rotation(canonical, new_path, global_ssh, cfg)
        verified_n = sum(1 for h in per_host if h["verified"])
        skipped_n = sum(1 for h in per_host if h["skipped"])
        message = f"Key rotated — verified on {verified_n} host(s)."
        if skipped_n:
            message += (
                f" {skipped_n} host(s) skipped (disconnected or per-host key) "
                "— update them manually."
            )
        return {
            "ok": True,
            "committed": True,
            "public_key": public_key,
            "key_path": _normalize_path(str(canonical)),
            "per_host": per_host,
            "message": message,
        }

    def _commit_key_rotation(
        self, canonical: Path, new_path: Path, global_ssh: dict, cfg: dict
    ) -> None:
        from datetime import timezone

        ts = int(time.time())
        if canonical.exists():
            canonical.rename(canonical.parent / f"runspec_ed25519.bak.{ts}")
        new_path.rename(canonical)

        # Rotate the PPK alongside the OpenSSH key
        canonical_ppk = Path(str(canonical) + ".ppk")
        new_ppk = Path(str(new_path) + ".ppk")
        if canonical_ppk.exists():
            canonical_ppk.rename(canonical.parent / f"runspec_ed25519.ppk.bak.{ts}")
        if new_ppk.exists():
            new_ppk.rename(canonical_ppk)

        # Rotate the .pub alongside too
        canonical_pub = Path(str(canonical) + ".pub")
        new_pub = Path(str(new_path) + ".pub")
        if canonical_pub.exists():
            canonical_pub.rename(canonical.parent / f"runspec_ed25519.pub.bak.{ts}")
        if new_pub.exists():
            new_pub.rename(canonical_pub)

        self._commit_ssh_fields(
            identityFile=_normalize_path(str(canonical)),
            key_created_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── PuTTY integration ─────────────────────────────────────────────────────

    def puttygen_path(self) -> str:
        """Locate puttygen.exe in common PuTTY install locations or on PATH."""
        found = utils_mod._find_putty_exe("puttygen.exe")
        return str(found) if found else ""

    def launch_puttygen(self) -> None:
        """Launch puttygen.exe in the background (fire and forget)."""
        import subprocess as _sp

        path = self.puttygen_path()
        if not path:
            return
        try:
            _sp.Popen([path], close_fds=True)
        except Exception:
            pass

    def open_putty_url(self, url: str) -> None:
        """Open a URL in the user's default browser."""
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def browse_ssh_binary(self) -> str:
        """Open a native file picker for an SSH binary; returns the chosen path or ''.

        The path is normalised to forward slashes before returning so the UI
        input field and config.toml see a consistent representation.
        """
        import webview

        windows = getattr(webview, "windows", None) or []
        if not windows:
            return ""
        try:
            result = windows[0].create_file_dialog(
                webview.FileDialog.OPEN,
                file_types=("Executables (*.exe)",),
            )
        except Exception:
            return ""
        if not result:
            return ""
        if isinstance(result, (list, tuple)):
            chosen = str(result[0]) if result else ""
        else:
            chosen = str(result)
        return _normalize_path(chosen)

    def browse_key_file(self) -> str:
        """Open a native file picker for an SSH key file; returns the path or ''.

        Used by the SSH-key credential form for the private/public key fields. No
        extension filter — key files have no standard suffix. Path normalised to
        forward slashes for a consistent UI / config representation."""
        import webview

        windows = getattr(webview, "windows", None) or []
        if not windows:
            return ""
        try:
            result = windows[0].create_file_dialog(webview.FileDialog.OPEN)
        except Exception:
            return ""
        if not result:
            return ""
        if isinstance(result, (list, tuple)):
            chosen = str(result[0]) if result else ""
        else:
            chosen = str(result)
        return _normalize_path(chosen)

    def browse_sound_file(self) -> str:
        """Open a native file picker filtered to audio cues (.wav/.mp3); returns
        the path or ''. Used by Settings → Notifications to point a sound
        category at the operator's own clip. Path normalised to forward slashes
        for a consistent UI / config representation."""
        import webview

        windows = getattr(webview, "windows", None) or []
        if not windows:
            return ""
        try:
            result = windows[0].create_file_dialog(
                webview.FileDialog.OPEN,
                file_types=("Audio files (*.wav;*.mp3)",),
            )
        except Exception:
            return ""
        if not result:
            return ""
        if isinstance(result, (list, tuple)):
            chosen = str(result[0]) if result else ""
        else:
            chosen = str(result)
        return _normalize_path(chosen)

    def browse_folder(self) -> str:
        """Open a native folder picker; returns the chosen directory or ''.

        Used by Settings → Repos to point a repo's checkout at a custom dev folder
        instead of the default AppData location. Path normalised to forward slashes
        for a consistent UI / config representation."""
        import webview

        windows = getattr(webview, "windows", None) or []
        if not windows:
            return ""
        try:
            result = windows[0].create_file_dialog(webview.FileDialog.FOLDER)
        except Exception:
            return ""
        if not result:
            return ""
        if isinstance(result, (list, tuple)):
            chosen = str(result[0]) if result else ""
        else:
            chosen = str(result)
        return _normalize_path(chosen)

    # ── hosts file management ─────────────────────────────────────────────────
