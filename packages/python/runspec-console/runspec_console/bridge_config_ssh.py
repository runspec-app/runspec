"""bridge_config_ssh.py — ConfigSshMixin: extracted from bridge.py (behavior-preserving).

Methods composed onto Bridge via mixin inheritance; they run on the full Bridge
instance, so ``self`` carries all shared state.
"""

from __future__ import annotations

import os
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
    logger,
)
from .bridge_utils import (
    _normalize_path,
    _public_key_from_private,
    _write_ppk_v2,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bridge import Bridge  # noqa: F401


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

    def brand_theme(self) -> dict[str, Any]:
        """The validated `[console.theme]` for the frontend (read-only white-label).

        Returns only the keys the UI renders, each cleaned: `accent_color` (a hex
        colour → Ant `colorPrimary`), `brand_name` (sidebar title), `watermark_text`,
        and `watermark_logo` (an http(s)/`data:image` URL). An absent or invalid
        value is dropped, so `{}` means "no theme — the default look". Source is
        config only (set by a config seed or by hand); there is no Settings editor.
        See docs/design/console-theme.md."""
        from .config import theme_config, valid_accent, valid_image_ref

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
        return out

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
        latest = latest_version(package=package)
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

        key_dir = Path(os.environ.get("APPDATA", "~")).expanduser() / "runspec-console"
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

        # Same idempotent push the key-rotation path uses (grep -qF || append).
        # public_key is single-quote-wrapped; an OpenSSH key contains no quotes.
        push_cmd = (
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
            f"grep -qF '{public_key}' ~/.ssh/authorized_keys || "
            f"printf '%s\\n' '{public_key}' >> ~/.ssh/authorized_keys"
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
        threading.Thread(target=self._refresh_cycle, daemon=True).start()
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
        threading.Thread(target=self._refresh_cycle, daemon=True).start()

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
        self._rotating = True
        try:
            return self._do_rotate_ssh_key()
        finally:
            self._rotating = False

    def _do_rotate_ssh_key(self) -> dict[str, Any]:
        from .executor import ssh_run

        key_dir = Path(os.environ.get("APPDATA", "~")).expanduser() / "runspec-console"
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

        # Steps 3 & 4 — push then verify each host
        per_host: list[dict[str, Any]] = []
        for host in remote_hosts:
            name = host.get("name", "?")
            ssh_target = host.get("ssh", "")
            host_idf = host.get("identityFile") or global_idf

            # Skip disconnected hosts — pushing would just fail and block the whole
            # rotation. They keep only the old key and must be updated manually once
            # reconnected (surfaced in per_host + the manual snippet in the UI).
            if not connected_snapshot.get(name, False):
                per_host.append(
                    {
                        "host": name,
                        "pushed": False,
                        "verified": False,
                        "skipped": True,
                        "error": "disconnected — new key not pushed; update manually",
                    }
                )
                continue

            # Skip hosts with a different per-host key — their session uses a different key
            if host.get("identityFile") and host.get("identityFile") != global_idf:
                per_host.append(
                    {
                        "host": name,
                        "pushed": False,
                        "verified": False,
                        "skipped": True,
                        "error": "per-host key override — rotate manually",
                    }
                )
                continue

            # Push new public key using old key
            push_cmd = (
                f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
                f"touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
                f"grep -qF '{public_key}' ~/.ssh/authorized_keys || "
                f"printf '%s\\n' '{public_key}' >> ~/.ssh/authorized_keys"
            )
            exit_code, _out, stderr = ssh_run(
                ssh_target,
                push_cmd,
                identity_file=host_idf,
                global_ssh_config=global_ssh,
            )
            if exit_code != 0:
                per_host.append(
                    {
                        "host": name,
                        "pushed": False,
                        "verified": False,
                        "skipped": False,
                        "error": stderr.strip() or f"push failed (exit {exit_code})",
                    }
                )
                continue

            # Verify new key works
            v_code, v_out, v_err = ssh_run(
                ssh_target,
                "echo ok",
                identity_file=str(new_path),
                global_ssh_config=global_ssh,
            )
            verified = v_code == 0 and "ok" in v_out
            per_host.append(
                {
                    "host": name,
                    "pushed": True,
                    "verified": verified,
                    "skipped": False,
                    "error": ""
                    if verified
                    else (
                        v_err.strip()
                        or v_out.strip()
                        or f"verify failed (exit {v_code})"
                    ),
                }
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
