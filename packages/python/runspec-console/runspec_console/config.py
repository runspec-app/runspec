"""
config.py — app-level paths, config file locations, and config I/O helpers.

All persistent state lives under %APPDATA%\runspec-console\ on Windows.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tomli_w

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

logger = logging.getLogger(__name__)

# Serialises every read-modify-write of config.toml so concurrent Settings-tab
# saves can't interleave and clobber each other's sections. Re-entrant so
# update_config() can call read_config()/write_config() while holding it.
_CONFIG_LOCK = threading.RLock()

# Where the in-app "Documentation" icon points. Overridable per-install via
# `[console] docs_url` in config.toml so a fork / internal mirror can retarget it.
DEFAULT_DOCS_URL = "https://runspec.app/"

# How many `<name>.bak-<ts>` backups Config Sync keeps per file before pruning the
# oldest (`[config_sync] backups_keep`). 0 means keep all (never prune).
DEFAULT_BACKUPS_KEEP = 5


def app_dir() -> Path:
    env = os.environ.get("APPDATA")
    base = Path(env) if env else Path.home() / "AppData" / "Roaming"
    d = base / "runspec-console"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hosts_path() -> Path:
    return app_dir() / "runspec_hosts.toml"


def filters_path() -> Path:
    return app_dir() / "runspec_filters.toml"


def memory_path() -> Path:
    """Machine-local agent memory (`runspec_memory.toml`) — accrued facts the agent
    writes via the `remember` tool. **Never** synced (not in Config Sync's
    KNOWN_FILES): it's local knowledge, not shared config."""
    return app_dir() / "runspec_memory.toml"


def skills_path() -> Path:
    """Curated agent runbooks (`runspec_skills.toml`) — a `[[skill]]` collection.
    Shareable: rides Config Sync (KNOWN_FILES), merged by `id` like filters."""
    return app_dir() / "runspec_skills.toml"


def credentials_path() -> Path:
    return app_dir() / "runspec_credentials.toml"


def config_path() -> Path:
    return app_dir() / "config.toml"


def triggers_path() -> Path:
    return app_dir() / "runspec_triggers.toml"


def rota_path() -> Path:
    """The working-hours rota (`runspec_rota.toml`). Its own file since the rota
    gates **both** triggers and schedules — it's no longer trigger-specific.
    Machine/person-local, like the mailbox folder: **never** synced (not in
    gitsync.KNOWN_FILES). Reads fall back to the legacy `[rota]` in
    `runspec_triggers.toml` for installs that predate the split."""
    return app_dir() / "runspec_rota.toml"


def trigger_state_path() -> Path:
    """Durable per-trigger thread dedup state for ``once_per_thread`` (internal —
    not user-edited config). JSON, since it's just an id→bag map."""
    return app_dir() / "runspec_trigger_state.json"


def _read_trigger_file() -> dict[str, Any]:
    """Full parse of ``runspec_triggers.toml`` ({} on missing/unreadable)."""
    path = triggers_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return dict(tomllib.load(f))
    except Exception:
        return {}


def read_triggers() -> list[dict[str, Any]]:
    """The ``[triggers.<name>]`` tables ([] on missing/unreadable — optional).

    One canonical shape, mirroring ``runspec.toml``'s named runnable sections:
    a named table per trigger, ``[triggers.<name>]``, with its sub-tables
    namespaced under the same name (``[triggers.<name>.match]`` / ``.map`` /
    ``.reply``). The table key *is* the trigger's name, so no ``name`` keyword is
    needed (and any in-table ``name`` is ignored — the key wins). Grouping each
    trigger under one prefix means a misplaced key can't bleed into the trigger
    above it.
    """
    raw = _read_trigger_file().get("triggers")
    if not isinstance(raw, dict):
        return []  # missing, or the unsupported [[triggers]] array form
    out: list[dict[str, Any]] = []
    for key, val in raw.items():
        if isinstance(val, dict):
            trig = dict(val)
            trig["name"] = key  # the table key is authoritative
            out.append(trig)
    return out


def trigger_file_error() -> str | None:
    """A human message if ``runspec_triggers.toml`` exists but isn't valid TOML.

    A single trigger's syntax error makes ``tomllib`` reject the *whole* file, so
    every trigger silently disappears. Surfacing this lets the UI say *why*
    instead of just showing zero triggers.
    """
    path = triggers_path()
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        return f"runspec_triggers.toml is not valid TOML — all triggers disabled until fixed: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"runspec_triggers.toml could not be read: {exc}"
    if isinstance(data.get("triggers"), list):
        return (
            "runspec_triggers.toml uses the old [[triggers]] array form, which is "
            "no longer supported. Use a named table per trigger instead: "
            "[triggers.<name>] with [triggers.<name>.match] / .map / .reply."
        )
    return None


def _read_rota_file() -> dict[str, Any]:
    """Full parse of ``runspec_rota.toml`` ({} on missing/unreadable)."""
    path = rota_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return dict(tomllib.load(f))
    except Exception:
        return {}


def read_rota() -> dict[str, Any]:
    """The ``[rota]`` working-hours table ({} when unset).

    Prefers the dedicated ``runspec_rota.toml``; falls back to the legacy
    ``[rota]`` in ``runspec_triggers.toml`` so installs from before the split keep
    their working hours until the next save migrates them across."""
    rota = _read_rota_file().get("rota")
    if isinstance(rota, dict):
        return dict(rota)
    legacy = _read_trigger_file().get("rota")  # pre-split location
    return dict(legacy) if isinstance(legacy, dict) else {}


def _write_rota_file(rota: dict[str, Any]) -> None:
    rota_path().write_text(_rota_to_toml(rota) if rota else "", encoding="utf-8")


def _write_triggers_only(triggers: list[dict[str, Any]]) -> None:
    """Write the triggers file as triggers-only — the rota lives in its own file
    now, so this also strips any legacy ``[rota]`` still embedded here."""
    triggers_path().write_text(
        _triggers_to_toml(triggers) if triggers else "", encoding="utf-8"
    )


def write_triggers(triggers: list[dict[str, Any]]) -> None:
    """Save the named ``[triggers.<name>]`` tables. Re-persists the current rota
    to ``runspec_rota.toml`` first (migrating a legacy embedded one) so rewriting
    the triggers file triggers-only can never drop the working hours."""
    _write_rota_file(read_rota())
    _write_triggers_only(triggers)


def write_triggers_file(rota: dict[str, Any], triggers: list[dict[str, Any]]) -> None:
    """Write both the rota (→ ``runspec_rota.toml``) and the ``[triggers.<name>]``
    tables (→ ``runspec_triggers.toml``) in one pass (used by Config Sync, which
    merges both before writing)."""
    _write_rota_file(rota)
    _write_triggers_only(triggers)


def write_rota(rota: dict[str, Any]) -> None:
    """Save the ``[rota]`` working-hours table to its own ``runspec_rota.toml``
    (it gates both triggers and schedules), keeping the triggers file
    triggers-only — which also clears any legacy ``[rota]`` still embedded there."""
    _write_rota_file(rota)
    _write_triggers_only(read_triggers())


def read_config() -> dict[str, Any]:
    """Parse config.toml, or ``{}`` when it's missing.

    An **unparseable** file is moved aside to ``config.toml.corrupt-<ts>`` (and
    logged) rather than silently treated as empty — otherwise the next write
    would overwrite the only copy, turning a transient corruption into permanent
    data loss. The app then starts from empty config; the operator can merge the
    backup back in.
    """
    with _CONFIG_LOCK:
        path = config_path()
        if not path.exists():
            return {}
        try:
            with open(path, "rb") as f:
                return dict(tomllib.load(f))
        except Exception:
            _backup_corrupt(path)
            return {}


def write_config(data: dict[str, Any]) -> None:
    """Serialise ``data`` to config.toml atomically (temp file + os.replace).

    Uses ``tomli_w`` so nested tables, arrays-of-tables, and string escaping are
    all emitted as valid TOML (the old hand-rolled serialiser produced invalid
    output for anything beyond one-level-flat sections, which then failed to
    re-parse and wiped the file). ``None`` values are dropped — TOML has no null.
    """
    with _CONFIG_LOCK:
        text = tomli_w.dumps(_strip_none(data))
        path = config_path()
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def update_config(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Atomically read-modify-write config.toml under the lock.

    ``mutate`` is called with the current config dict and edits it in place; the
    result is written back. The whole read→edit→write runs while holding
    ``_CONFIG_LOCK``, so two concurrent updates can't lose each other's changes.
    Returns the new config.
    """
    with _CONFIG_LOCK:
        cfg = read_config()
        mutate(cfg)
        write_config(cfg)
        return cfg


def _strip_none(value: Any) -> Any:
    """Recursively drop ``None`` values (TOML can't represent null)."""
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value


def _backup_corrupt(path: Path) -> None:
    """Move an unparseable config.toml aside so it isn't clobbered by the next
    write; best-effort and never raises into the caller."""
    try:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        path.replace(backup)
        logger.error(
            "config.toml was unparseable; backed it up to %s and starting from "
            "empty config. Merge any settings back from that file.",
            backup,
        )
    except Exception:
        logger.exception("config.toml is unparseable and could not be backed up")


def docs_url() -> str:
    """Documentation URL: `[console] docs_url` from config, else the default."""
    console = read_config().get("console")
    if isinstance(console, dict):
        url = console.get("docs_url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return DEFAULT_DOCS_URL


def read_chat() -> dict[str, Any]:
    """The ``[chat]`` room-source table from config.toml ({} when unset).

    Connection config for the chat trigger source: ``host`` (the SSH host the
    room web app runs on), ``port`` (its localhost port on that host, reached
    over the tunnel), ``room``, and the console's display ``user``.
    """
    chat = read_config().get("chat")
    return dict(chat) if isinstance(chat, dict) else {}


def read_mailbox() -> dict[str, Any] | None:
    """The single ``[mailbox]`` the Outlook source monitors, or ``None`` when no
    ``[mailbox]`` table is configured (email monitoring off).

    One inbox, with an optional folder inside it::

        [mailbox]
        address = "helpdesk@corp.com"   # blank/absent → the default account
        folder  = "Tickets/Open"        # blank/absent/"inbox" → the Inbox
        label   = "Helpdesk"            # optional display label
        once_per_thread = true          # default reply-suppression for triggers

    A present-but-empty ``[mailbox]`` means "watch my default account's Inbox".
    Normalisation: ``address`` blank/missing → ``""`` (the default account);
    ``folder`` blank, missing, or ``"inbox"`` (case-insensitive) → ``"Inbox"``.
    ``once_per_thread`` (default ``False``) is the mailbox-wide default for
    suppressing replies — a trigger inherits it unless it sets its own
    ``once_per_thread`` (see triggers.py). There is no queue here — queues are
    teams, managed in the room DB (see docs/design/teams-and-email-monitoring.md)."""
    raw = read_config().get("mailbox")
    if not isinstance(raw, dict):
        return None
    address = str(raw.get("address") or "").strip()
    folder = str(raw.get("folder") or "").strip()
    label = str(raw.get("label") or "").strip()
    if not folder or folder.lower() == "inbox":
        folder = "Inbox"
    return {
        "address": address,
        "folder": folder,
        "label": label,
        "once_per_thread": bool(raw.get("once_per_thread", False)),
    }


def write_mailbox(mailbox: dict[str, Any] | None) -> None:
    """Replace the ``[mailbox]`` table in config.toml (or drop it when ``mailbox``
    is ``None``), preserving every other section. Only set fields are emitted, so
    "watch my own Inbox" round-trips to an empty ``[mailbox]`` table."""

    def mutate(cfg: dict[str, Any]) -> None:
        if not mailbox:
            cfg.pop("mailbox", None)
            return
        address = str(mailbox.get("address") or "").strip()
        folder = str(mailbox.get("folder") or "").strip()
        label = str(mailbox.get("label") or "").strip()
        entry: dict[str, Any] = {}
        if address:
            entry["address"] = address
        if folder and folder.lower() != "inbox":
            entry["folder"] = folder
        if label:
            entry["label"] = label
        if mailbox.get("once_per_thread"):
            entry["once_per_thread"] = True
        cfg["mailbox"] = entry

    update_config(mutate)


def read_watches() -> list[dict[str, Any]]:
    """Additional **watch-only** mailboxes/folders the Outlook source monitors, on
    top of the primary ``[mailbox]`` (:func:`read_mailbox`). An array of tables::

        [[watch]]                  # my own Inbox (blank address = default account)

        [[watch]]
        address = "alerts@corp.com"
        folder  = "Inbox"
        label   = "Alerts"

    Each entry is normalised exactly like ``[mailbox]`` (``address`` blank/missing →
    ``""`` = the default account; ``folder`` blank/missing/``"inbox"`` → ``"Inbox"``)
    and returned as ``{address, folder, label}``.

    These feed ``new_mail`` triggers **only** — they carry no room/helpdesk role:
    the room handshake and the email-verify "send-as" identity stay tied to the
    primary ``[mailbox]`` alone (see ``sources/chat.py`` ``_send_hello``). So a
    person can watch their own Inbox here while the shared helpdesk mailbox remains
    the room's identity. There is no per-watch ``once_per_thread`` — a trigger sets
    its own override if it wants reply suppression on an extra folder.

    Non-dict entries are skipped; ``[]`` when no ``[[watch]]`` is present."""
    raw = read_config().get("watch")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        address = str(item.get("address") or "").strip()
        folder = str(item.get("folder") or "").strip()
        label = str(item.get("label") or "").strip()
        if not folder or folder.lower() == "inbox":
            folder = "Inbox"
        out.append({"address": address, "folder": folder, "label": label})
    return out


def write_watches(watches: list[dict[str, Any]] | None) -> None:
    """Replace the ``[[watch]]`` array of additional watch-only mailboxes (or drop
    it when ``watches`` is empty/``None``), preserving every other section. Only
    set fields are emitted per entry, so a bare "watch my own Inbox" entry
    round-trips to an empty ``[[watch]]`` table."""

    def mutate(cfg: dict[str, Any]) -> None:
        entries: list[dict[str, Any]] = []
        for watch in watches or []:
            address = str(watch.get("address") or "").strip()
            folder = str(watch.get("folder") or "").strip()
            label = str(watch.get("label") or "").strip()
            entry: dict[str, Any] = {}
            if address:
                entry["address"] = address
            if folder and folder.lower() != "inbox":
                entry["folder"] = folder
            if label:
                entry["label"] = label
            entries.append(entry)
        if entries:
            cfg["watch"] = entries
        else:
            cfg.pop("watch", None)

    update_config(mutate)


def read_docs_dir() -> str:
    """The ``[docs] dir`` — where ``generate_docs`` writes the runbook wiki ('' when
    unset; the bridge then defaults to ``app_dir()/docs``). Point it at a local
    clone of the config repo's team folder to commit the docs back."""
    docs = read_config().get("docs")
    return str(docs.get("dir", "")).strip() if isinstance(docs, dict) else ""


def write_docs_dir(path: str) -> None:
    """Persist (or clear) the ``[docs] dir`` setting, leaving other sections alone."""

    def mutate(cfg: dict[str, Any]) -> None:
        p = str(path or "").strip()
        if p:
            cfg.setdefault("docs", {})["dir"] = p
        else:
            cfg.pop("docs", None)

    update_config(mutate)


def read_sync() -> dict[str, Any]:
    """The ``[config_sync]`` table from config.toml ({} when unset).

    Config Sync's settings: which repo to pull config from, at what ref, with
    which credential. Shape::

        [config_sync]
        url           = "https://github.com/acme/runspec-config.git"
        ref           = "v1.4.0"          # tag (preferred) or branch
        credential_id = "config-repo"     # a Credentials-tab entry
        subpath       = ""                # optional dir within the repo
        layers        = ["_base", "teams/emea"]  # ordered overlay dirs (low→high)
        files         = ["config.toml", "runspec_hosts.toml", ...]
        preserve      = ["config.toml:mailbox.folder", ...]  # keep local on pull
        backups_keep  = 5                 # per-file backup retention (0 = keep all)
        last_ref      = "v1.4.0"          # stamped by a successful pull
        last_commit   = "a1b2c3d…"
        last_synced   = "2026-06-16T04:30:00Z"

    ``layers`` are repo subdirectories overlaid in order (each later layer wins);
    when empty, ``subpath`` is used as a single layer (back-compat). The secret is
    never here — it lives in the keyring under ``credential_id``.
    """
    raw = read_config().get("config_sync")
    return dict(raw) if isinstance(raw, dict) else {}


def write_sync(sync: dict[str, Any]) -> None:
    """Replace the ``[config_sync]`` table, preserving every other section.

    Used both when the operator saves the Config Sync settings and to stamp
    ``last_*`` after a pull. Empty/blank scalars are dropped; ``files`` is kept
    only when non-empty (an empty list means "use the default set")."""

    def mutate(cfg: dict[str, Any]) -> None:
        entry: dict[str, Any] = {}
        for key in (
            "url",
            "ref",
            "credential_id",
            "subpath",
            "ca_bundle",
            "last_ref",
            "last_commit",
            "last_synced",
        ):
            val = str(sync.get(key) or "").strip()
            if val:
                entry[key] = val
        layers = [str(s).strip() for s in (sync.get("layers") or []) if str(s).strip()]
        if layers:
            entry["layers"] = layers
        files = [str(f).strip() for f in (sync.get("files") or []) if str(f).strip()]
        if files:
            entry["files"] = files
        preserve = [
            str(p).strip() for p in (sync.get("preserve") or []) if str(p).strip()
        ]
        if preserve:
            entry["preserve"] = preserve
        # Only written when overridden — the default keeps the file clean.
        keep = backups_keep_value(sync)
        if keep != DEFAULT_BACKUPS_KEEP:
            entry["backups_keep"] = keep
        # Only written when disabled — default (verify on) keeps the file clean.
        if sync.get("tls_verify", True) is False:
            entry["tls_verify"] = False
        if entry:
            cfg["config_sync"] = entry
        else:
            cfg.pop("config_sync", None)

    update_config(mutate)


def backups_keep_value(sync: dict[str, Any]) -> int:
    """`[config_sync] backups_keep` coerced to a non-negative int.

    The per-file cap on `<name>.bak-<ts>` backups Config Sync retains; the oldest
    beyond it are pruned after a pull. Falls back to ``DEFAULT_BACKUPS_KEEP`` when
    unset or unparseable; ``0`` means keep every backup (never prune). Negatives
    clamp to 0."""
    raw = sync.get("backups_keep")
    if raw is None:
        return DEFAULT_BACKUPS_KEEP
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_BACKUPS_KEEP


def confirm_notifications_enabled() -> bool:
    """`[console] confirm_notifications` — raise a desktop toast when an agent
    tool call hits the autonomy gate. Defaults to True; set false to silence the
    OS notification while keeping the in-app Approve/Deny card."""
    console = read_config().get("console")
    if isinstance(console, dict):
        val = console.get("confirm_notifications")
        if isinstance(val, bool):
            return val
    return True


def room_notifications_enabled() -> bool:
    """`[console] room_notifications` — raise a desktop toast + taskbar flash when
    a chat-room/helpdesk message arrives and the console isn't in the foreground.
    Defaults to True; set false to silence the OS notification while keeping the
    in-app toast and unread badges."""
    console = read_config().get("console")
    if isinstance(console, dict):
        val = console.get("room_notifications")
        if isinstance(val, bool):
            return val
    return True


def _toml_str(v: str) -> str:
    """Escape a Python string as a TOML basic string literal.

    TOML basic strings (double-quoted) recognise backslash escapes — meaning
    a Windows path like ``C:\\Program Files\\PuTTY\\plink.exe`` written
    verbatim is invalid TOML (``\\P`` is not a recognised escape) and
    ``tomllib`` will refuse to read the file back. Escape ``\\`` / ``"`` and the
    common control chars (so a multiline ``rules`` string survives) before
    wrapping in quotes, so the round-trip preserves the original string.
    """
    escaped = (
        v.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _toml_scalar(v: Any) -> str:
    """Render a scalar (or list of scalars) as a TOML value."""
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_scalar(x) for x in v) + "]"
    return str(v)


def _triggers_to_toml(triggers: list[dict[str, Any]]) -> str:
    """Serialise named ``[triggers.<name>]`` tables (the recommended shape), each
    with optional nested ``match`` / ``map`` / ``reply`` sub-tables.

    The trigger's ``name`` becomes the table key, so it is *not* re-emitted as a
    scalar — ``read_triggers`` reconstructs it from the key. Scalars are written
    before sub-tables (TOML requires a table's own keys to precede its child
    tables). Keys are quoted, so a name with dots/spaces stays one table; a
    fallback name is synthesised (and duplicates de-duped) so output is always
    valid TOML."""
    blocks: list[str] = []
    seen: set[str] = set()
    for i, t in enumerate(triggers):
        name = str(t.get("name") or f"trigger{i + 1}")
        key = name
        n = 2
        while key in seen:  # keep table keys unique even if names collide
            key = f"{name}-{n}"
            n += 1
        seen.add(key)
        lines = [f"[triggers.{_toml_str(key)}]"]
        dict_subs = [(k, v) for k, v in t.items() if isinstance(v, dict)]
        # A list whose items are all dicts is an array-of-tables (e.g. `actions`),
        # emitted as repeated [[triggers.<key>.<name>]] blocks below. An empty or
        # scalar list (e.g. `runnables`) is written inline as a normal scalar.
        list_table_subs = [
            (k, v)
            for k, v in t.items()
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)
        ]
        list_table_keys = {k for k, _ in list_table_subs}
        for k, v in t.items():
            if isinstance(v, dict) or v is None or k == "name" or k in list_table_keys:
                continue
            lines.append(f"{k} = {_toml_scalar(v)}")
        for sub_name, sub in dict_subs:
            lines.append(f"[triggers.{_toml_str(key)}.{sub_name}]")
            for kk, vv in sub.items():
                if vv is not None:
                    lines.append(f"{kk} = {_toml_scalar(vv)}")
        for sub_name, items in list_table_subs:
            for item in items:
                lines.append(f"[[triggers.{_toml_str(key)}.{sub_name}]]")
                nested = [(k, v) for k, v in item.items() if isinstance(v, dict)]
                for kk, vv in item.items():
                    if isinstance(vv, dict) or vv is None:
                        continue
                    lines.append(f"{kk} = {_toml_scalar(vv)}")
                # A nested table (e.g. an action's `map`) attaches to the most
                # recent [[...]] element — valid TOML, reads back per-element.
                for nname, nsub in nested:
                    lines.append(f"[triggers.{_toml_str(key)}.{sub_name}.{nname}]")
                    for kk, vv in nsub.items():
                        if vv is not None:
                            lines.append(f"{kk} = {_toml_scalar(vv)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _rota_to_toml(rota: dict[str, Any]) -> str:
    """Serialise the ``[rota]`` table — scalar keys (e.g. ``enabled``) first, then
    a ``[rota.<day>]`` sub-table per weekday ({on, start, end})."""
    lines = ["[rota]"]
    subtables = [(k, v) for k, v in rota.items() if isinstance(v, dict)]
    for k, v in rota.items():
        if isinstance(v, dict) or v is None:
            continue
        lines.append(f"{k} = {_toml_scalar(v)}")
    for name, sub in subtables:
        lines.append(f"[rota.{name}]")
        for kk, vv in sub.items():
            if vv is not None:
                lines.append(f"{kk} = {_toml_scalar(vv)}")
    return "\n".join(lines) + "\n"
