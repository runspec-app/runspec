r"""
config.py — app-level paths, config file locations, and config I/O helpers.

By default persistent state lives under the writable per-user app-data root
(%APPDATA%\ on Windows, ~/AppData/Roaming/ elsewhere) in a folder named for the
white-label package: `%APPDATA%\<dist-name>\`, defaulting to `runspec-console`.
A package that ships a config seed brands the folder with its own distribution
name (e.g. `jason-console`) — the same source that names the window title — so
two different white-label installs are isolated under one writable root without
any flag (see :func:`app_dir` / :func:`_app_slug`). The app-data root is used
deliberately rather than the venv, which can be read-only for a non-admin
operator. Both are overridden by `runspec-console --config-dir <dir>` / the
RUNSPEC_CONSOLE_DIR env var (see :func:`set_app_dir`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
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

from .tomlsafe import basic_string, dump_scalar

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

# `[telemetry]` defaults — OpenTelemetry (OTLP) tracing of the agent. Off by
# default (it exports data to an external backend, so it's strictly opt-in).
DEFAULT_TELEMETRY_PROTOCOL = "http/protobuf"
DEFAULT_TELEMETRY_SERVICE_NAME = "runspec-console"
# Default seconds between OTLP metric exports (PeriodicExportingMetricReader).
DEFAULT_METRIC_EXPORT_INTERVAL = 60

# [mcp_server] — the console's own in-process MCP server (an external coding
# agent such as Claude Code connects to it). Localhost-only by default.
DEFAULT_MCP_SERVER_HOST = "127.0.0.1"
DEFAULT_MCP_SERVER_PORT = 8765
_MCP_SERVER_AUTONOMY_LEVELS = ("autonomous", "confirm", "supervised", "manual")
# How the MCP server presents runnables with subcommands: "leaf" (default,
# serve-identical — one tool per subcommand leaf) or "runnable" (one tool per
# runnable with a `command` arg — far fewer tools for a multi-verb fleet).
_MCP_SERVER_TOOL_STYLES = ("leaf", "runnable")
DEFAULT_MCP_SERVER_TOOL_STYLE = "leaf"


# An explicit config directory pinned by `runspec-console --config-dir <dir>`
# (see app.main). It wins over APPDATA and the RUNSPEC_CONSOLE_DIR env var, and
# is used directly (not with `runspec-console` appended) so a test / throwaway
# instance keeps its whole state — hosts, credentials, triggers, config.toml —
# isolated from the real profile. None means "use the default location".
_APP_DIR_OVERRIDE: Path | None = None


def set_app_dir(path: str | os.PathLike[str] | None) -> None:
    """Pin the console's config directory, overriding the default location.

    Wired to `runspec-console --config-dir <dir>`. Must be called before the
    Bridge is built (it reads config on construction). Pass None to clear the
    override and fall back to RUNSPEC_CONSOLE_DIR / APPDATA."""
    global _APP_DIR_OVERRIDE
    _APP_DIR_OVERRIDE = Path(path).expanduser() if path else None


# The config folder name under the app-data base, resolved once from installed
# metadata (never config.toml, which lives inside the folder) and cached for the
# session. Reset to None to force re-resolution (tests).
_APP_SLUG: str | None = None


def _appdata_base() -> Path:
    """The writable per-user app-data root — ``%APPDATA%`` on Windows,
    ``~/AppData/Roaming`` elsewhere."""
    env = os.environ.get("APPDATA")
    return Path(env) if env else Path.home() / "AppData" / "Roaming"


def _app_slug() -> str:
    """The config folder name under :func:`_appdata_base`.

    Defaults to ``runspec-console``; a white-label package that ships a config
    seed brands the folder with **its own distribution name** (e.g.
    ``jason-console``) — the *same* source that names the window title, so the two
    always agree. Two different white-label installs get separate folders under
    one writable root; the same package shares one. Derived from installed
    metadata only (`configseed.branding_dist_name`), so it is stable, cheap, and
    non-circular (it never reads config.toml, which lives inside the folder).
    Cached for the session; best-effort (any failure ⇒ the default)."""
    global _APP_SLUG
    if _APP_SLUG is None:
        name = None
        try:
            from . import configseed

            name = configseed.branding_dist_name()
        except Exception:
            name = None
        _APP_SLUG = name or "runspec-console"
    return _APP_SLUG


def app_slug() -> str:
    """Public accessor for the install's branded slug (see :func:`_app_slug`) —
    the config folder name **and** the SSH key marker (``bridge_utils.key_marker``)
    derive from it, so the two always agree. Delegates at call time (tests
    monkeypatch ``_app_slug``)."""
    return _app_slug()


def app_dir() -> Path:
    if _APP_DIR_OVERRIDE is not None:
        d = _APP_DIR_OVERRIDE
    elif env_override := os.environ.get("RUNSPEC_CONSOLE_DIR"):
        # An env-var twin of --config-dir, handy for test harnesses that launch
        # the console as a subprocess. Same "use the directory directly" rule.
        d = Path(env_override).expanduser()
    else:
        # Under the writable app-data root — a venv can be read-only for a
        # non-admin operator, so config never lives inside it — in a folder named
        # for the white-label package so separate installs don't share one
        # profile. The folder name matches the window title's source.
        d = _appdata_base() / _app_slug()
    d.mkdir(parents=True, exist_ok=True)
    return d


def hosts_path() -> Path:
    return app_dir() / "runspec_hosts.toml"


def groups_path() -> Path:
    """Canonical host-group registry (`runspec_groups.toml`) — the controlled
    vocabulary of group names shared across consoles via Config Sync. Host→group
    membership lives on the host entry; this file just pins the legal names so a
    shared runbook targeting a group behaves the same on every console."""
    return app_dir() / "runspec_groups.toml"


def profiles_path() -> Path:
    """Canonical host-profile registry (`runspec_profiles.toml`) — the controlled
    vocabulary of profile (working-set) names, shared across consoles via Config
    Sync like the group registry. Host→profile membership lives on the host
    entry's `profiles` array; this file just pins the legal names. See
    `docs/design/host-profiles.md`."""
    return app_dir() / "runspec_profiles.toml"


def active_profile_path() -> Path:
    """Machine-local active-profile pointer (`runspec_active_profile.toml`) — which
    host profile *this* console currently has loaded. Personal, moment-to-moment
    state (like the rota / repo checkout paths), so it is **never** synced (not in
    Config Sync's KNOWN_FILES). Absent/blank `active` = the built-in *All hosts*
    scope (every host in scope, i.e. today's behavior)."""
    return app_dir() / "runspec_active_profile.toml"


def active_room_path() -> Path:
    """Machine-local active-room pointer (`runspec_active_room.toml`) — which
    room-configured host *this* console connects to (single-select; only one room
    at a time). Personal, per-console state, so it is **never** synced (not in
    Config Sync's KNOWN_FILES). Absent = the default (auto-connect the sole room
    host, else none); blank `active` = explicitly *no room*; a host name = that
    host's room only."""
    return app_dir() / "runspec_active_room.toml"


def seed_state_path() -> Path:
    """Machine-local record of the package config-seed last applied
    (`runspec_seed_state.toml`) — the `{distribution: version}` stamp the seed
    apply compares against to stay idempotent (a changed version re-asserts).
    Depends on what's installed on *this* machine, so it is **never** synced (not
    in Config Sync's KNOWN_FILES), like the active-profile pointer and repo
    checkout paths. See `docs/design/config-seed.md`."""
    return app_dir() / "runspec_seed_state.toml"


def disclaimer_ack_path() -> Path:
    """Machine-local record that this operator acknowledged the AI-use disclaimer
    (`runspec_disclaimer_ack.toml`) — the content hash + version + timestamp of the
    wording they accepted. Personal, per-console state (like the active-profile
    pointer and seed state), so it is **never** synced (not in Config Sync's
    KNOWN_FILES): the acknowledgement is this machine's, not shared config. Absent
    ⇒ never acknowledged; a stored hash that no longer matches the current wording
    ⇒ re-prompt. See docs/design/console-disclaimer.md."""
    return app_dir() / "runspec_disclaimer_ack.toml"


def filters_path() -> Path:
    return app_dir() / "runspec_filters.toml"


def self_service_path() -> Path:
    """The self-service catalog registry (`runspec_self_service.toml`) — a
    `[[self_service]]` collection naming which discovered runnables are offered to
    visitors in the helpdesk room. This replaces the runspec.toml `discoverable =
    ["self-service"]` marking: self-service is a front-end concern, designated
    here (and edited from Settings) rather than baked into each runnable. Rides
    Config Sync (KNOWN_FILES), merged by `name` like the other collection files."""
    return app_dir() / "runspec_self_service.toml"


def memory_path() -> Path:
    """Machine-local agent memory (`runspec_memory.toml`) — accrued facts the agent
    writes via the `remember` tool. **Never** synced (not in Config Sync's
    KNOWN_FILES): it's local knowledge, not shared config."""
    return app_dir() / "runspec_memory.toml"


def runbooks_path() -> Path:
    """Curated agent runbooks (`runspec_runbooks.toml`) — a `[[runbook]]` collection.
    Shareable: rides Config Sync (KNOWN_FILES), merged by `id` like filters.

    Back-compat: runbooks were called *skills* (`runspec_skills.toml`) before the
    rename. If only the legacy file exists, migrate it once by renaming it to the
    new path so existing runbooks aren't orphaned; nothing is lost if neither
    exists (a fresh install just creates the new file on first save)."""
    path = app_dir() / "runspec_runbooks.toml"
    if not path.exists():
        legacy = app_dir() / "runspec_skills.toml"
        if legacy.exists():
            try:
                legacy.rename(path)
            except OSError:
                return legacy  # couldn't migrate — keep reading the legacy file
    return path


def saved_runs_path() -> Path:
    """The saved-run registry (`runspec_saved_runs.toml`) — a `[[saved_run]]`
    collection of deterministic presets (runnable + target + fixed args + open
    params). Rides Config Sync (KNOWN_FILES), merged by `id` like runbooks."""
    return app_dir() / "runspec_saved_runs.toml"


def sounds_registry_path() -> Path:
    """The named-sound registry (`runspec_sounds.toml`) — a `[[sound]]` collection
    mapping a stable `id` + `label` to an audio `file` on this machine, so the
    operator, a trigger/schedule, and the agent can all name a sound by id
    (`sound:<id>`) instead of a raw path. The registry **rides Config Sync**
    (KNOWN_FILES), merged by `id` like runbooks — but the audio files it points at
    are binary and **machine-local** (like a `pack` folder), so a synced id
    resolves to a cue only on a machine where that file is present, degrading
    gracefully to silence elsewhere."""
    return app_dir() / "runspec_sounds.toml"


def sounds_store_dir() -> Path:
    """Where `import_sound` copies an operator-added audio file so the registry is
    self-contained (survives the source file moving). Under AppData — **never**
    synced, exactly like the registry's `file` paths and a `pack` folder."""
    return app_dir() / "sounds"


def credentials_path() -> Path:
    return app_dir() / "runspec_credentials.toml"


def repos_path() -> Path:
    """The git-repo registry (`runspec_repos.toml`) — the controlled list of repos
    the agent's repo_* tools may clone/read/edit. Rides Config Sync (KNOWN_FILES),
    merged by `name` like the other collection files."""
    return app_dir() / "runspec_repos.toml"


def repos_root() -> Path:
    """Parent dir for local repo checkouts (one subdir per registry `name`). Local
    working copies — **never** synced; the registry that lists them is."""
    return app_dir() / "repos"


def repo_paths_path() -> Path:
    """Machine-local per-repo checkout-location overrides (`runspec_repo_paths.toml`).
    A repo normally checks out under `repos_root()/{name}` (AppData); for one the
    operator actively develops, they can point its checkout at their own dev folder
    instead. That choice is **machine-local** — the path is specific to this
    machine's filesystem, so it lives here and is **never** synced (not in Config
    Sync's KNOWN_FILES), unlike the registry that lists the repos."""
    return app_dir() / "runspec_repo_paths.toml"


def conversations_dir() -> Path:
    """Directory holding saved chat conversations (one `{id}.json` per
    conversation). **Machine-local** — transcripts can contain sensitive content
    and are instance-specific, so they're **never** synced (not in Config Sync's
    KNOWN_FILES). Bounded by the `[chat_history]` retention settings."""
    return app_dir() / "conversations"


def improve_reports_dir() -> Path:
    """Directory holding saved **Improve** reports (one `{ts}.json` per report) —
    the effectiveness/problem-report analyses produced by the Improve tab.
    **Machine-local** — a report is derived from this install's own transcripts +
    run history, so it is **never** synced (not in Config Sync's KNOWN_FILES),
    like conversations and History. The developer takes the report and folds any
    durable learnings into their Config Sync repo / seed on their own terms."""
    return app_dir() / "improve"


def eval_reports_dir() -> Path:
    """Directory holding saved **Evals** reports (one `{ts}.json` per run) — the
    tool-selection eval sweeps produced by the Evals tab. **Machine-local** (like
    Improve reports): a report reflects *this* install's configured agent surface
    (its tool_mode, hidden tools, model, effort), so it is **never** synced."""
    return app_dir() / "evals" / "reports"


def evals_path() -> Path:
    """The **Evals** fixture store: one `runspec_evals.toml` holding `[[suite]]`
    entries (each a named suite with its `[[suite.eval]]` cases). A single named
    file — not a directory — so it **rides Config Sync**: it's in gitsync's
    KNOWN_FILES and merges by suite `name` (COLLECTION_FILES), so a team shares one
    golden suite distributed from the versioned repo, an operator's own local
    suites survive the merge, and it can seed a fresh install."""
    return app_dir() / "runspec_evals.toml"


def workspaces_root() -> Path:
    """Parent dir for per-session agent workspaces (one subdir per chat
    conversation, trigger event, or schedule run). Large runnable outputs get
    spilled here so the agent can interrogate them via the ``workspace_*`` file
    tools without the raw bytes ever entering the model context. **Machine-local**
    — spilled output can be sensitive and is only meaningful on the console that
    ran the tool, so it's **never** synced (not in Config Sync's KNOWN_FILES).
    Chat workspaces are pruned alongside the conversation they belong to
    (``[chat_history]`` retention); headless (trigger/schedule) workspaces are
    pruned by ``[workspace] headless_retention_days``."""
    return app_dir() / "workspaces"


def workspace_dir(session_id: str) -> Path:
    """The workspace directory for one session id (a chat ``conversation_id``, a
    ``trigger-<event_id>``, or a ``schedule-<run_id>``). Never trust the id
    straight into a path — sanitised in :mod:`runspec_console.workspaces`."""
    from .workspaces import _safe_id

    return workspaces_root() / _safe_id(session_id)


def read_workspace() -> dict[str, Any]:
    """The ``[workspace]`` settings, with defaults applied.

    ``spill_threshold`` (chars, default 32_768) — a runnable's tool output larger
    than this is written to the session workspace instead of inlined; the model
    gets a short envelope + head preview and reaches for the ``workspace_*``
    tools. Set to 0 to disable spill and keep the pre-workspace cap-and-truncate
    behaviour. ``inline_max`` (chars, default 131_072) — the ceiling for a call's
    ``deliver = "inline"`` request (hand the raw output back whole); above it the
    call falls back to ``auto`` (spill). ``headless_retention_days`` (default 7) — how long
    trigger/schedule workspaces live after the isolated turn finishes (chat
    workspaces follow ``[chat_history]`` retention).

    ``code_exec`` (default true) — whether the ``run_python_sandboxed`` /
    ``run_sql_sandboxed`` agent tools are offered at all; a locked-down deployment
    sets it false to drop them entirely (a hard off-switch *on top of* the WASM
    sandbox and the confirm gate). ``exec_timeout`` (seconds, default 30) — the
    wall-clock ceiling for a sandboxed run. The sandbox itself (runtime,
    memory/fuel limits, WASI binary) is configured under ``[sandbox]`` — see
    :func:`read_sandbox`."""
    raw = read_config().get("workspace")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    return {
        "spill_threshold": max(0, int(cfg.get("spill_threshold", 32_768) or 0)),
        "inline_max": max(1, int(cfg.get("inline_max", 131_072) or 131_072)),
        "headless_retention_days": max(
            0, int(cfg.get("headless_retention_days", 7) or 0)
        ),
        "code_exec": bool(cfg.get("code_exec", True)),
        "exec_timeout": max(1, int(cfg.get("exec_timeout", 30) or 30)),
    }


def read_web() -> dict[str, Any]:
    """The ``[web]`` settings for the ``fetch_url`` agent tool, with defaults.

    ``enabled`` (default true) — whether the agent's read-only web-page fetch tool
    is offered at all; a locked-down deployment sets it false to drop the
    capability (no outbound HTTP from the agent). ``timeout`` (seconds, default
    20) and ``max_bytes`` (default 2_000_000) bound a single fetch. ``ca_bundle``
    (path) and ``tls_verify`` (default true) mirror the Config-Sync HTTPS knobs
    for internal hosts behind a private CA / TLS-intercepting proxy."""
    raw = read_config().get("web")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    ca = str(cfg.get("ca_bundle") or "").strip()
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "timeout": max(1.0, float(cfg.get("timeout", 20) or 20)),
        "max_bytes": max(1, int(cfg.get("max_bytes", 2_000_000) or 2_000_000)),
        "ca_bundle": ca or None,
        "tls_verify": bool(cfg.get("tls_verify", True)),
    }


def read_automation() -> dict[str, Any]:
    """The ``[automation]`` settings for the trigger/schedule engine, with defaults.

    ``max_workers`` (default 2, clamped 1–32) — the size of the thread pool the
    :class:`~runspec_console.triggers.TriggerEngine` runs trigger and schedule
    fires on. Each fire (an agent turn or a runnable run) holds a worker for its
    whole duration, so on a busy console — several triggers matching at once, or a
    burst of scheduled jobs — the default of 2 serialises everything past the
    second. Raise it to run more concurrently; per-trigger serialisation (one
    in-flight run per trigger name) is unaffected. Mirrors the ``[refresh]
    workers`` knob for the fleet sweep."""
    raw = read_config().get("automation")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    return {
        "max_workers": min(32, max(1, int(cfg.get("max_workers", 2) or 2))),
    }


def read_marketplace() -> dict[str, Any]:
    """The ``[marketplace]`` settings for the Marketplace tab, with defaults.

    - ``enabled`` (default true): master switch — false hides the tab and makes
      every marketplace bridge method refuse.
    - ``auto_pip`` (default true): derive index repos from the console venv's
      own pip configuration (``pip.ini`` / ``pip.conf`` / ``PIP_INDEX_URL`` /
      ``PIP_EXTRA_INDEX_URL``) — a console pointed at a private index gets a
      marketplace for it with no further config. pip's default pypi.org counts
      when nothing sets an index.
    - ``repos`` (``[[marketplace.repos]]``): declared indexes — ``name``,
      ``url`` (a simple-API root or the index base), ``enabled``, ``scan``
      (list the root index and keep names matching ``patterns``; default true),
      ``packages`` (explicit names, read even when not scanning), ``patterns``
      (name globs, default ``["runspec-*"]``), ``credential`` (a Credentials id
      for Basic auth — the secret stays in the keychain), ``pip_arg``
      (``auto`` / ``none`` / ``index-url`` / ``extra-index-url``: how the repo
      is handed to the target's ``pip install``), ``trusted_host``, ``tags``.
    - ``patterns`` (default ``["runspec-*"]``): the default scan globs.
    - ``exclude`` (default the runspec library + apps): distributions that
      depend on ``runspec`` but are not runnable packages — never listed.
    - ``hide`` (default ``[]``): name globs of **runnable** packages to hide
      from the catalogue — a curation knob for an adapter / white-label deploy
      that ships internal wrappers and doesn't want the public package offered
      too (e.g. ``["runspec-jira", "runspec-confluence"]``). Additive and
      glob-capable (``runspec-*``); matched case-insensitively on the
      normalised name. Rides Config Sync / the config seed like the rest of
      ``[marketplace]``, so the adapter distributes one versioned list.
    - ``require_runspec`` (default true): only list distributions that depend on
      ``runspec`` (or carry the ``runspec-runnable`` keyword). False lists every
      explicitly named ``packages`` entry regardless.
    - ``include_prerelease`` (default false): consider pre-release versions.
    - ``cache_ttl`` (seconds, default 86400): how long a fetched catalogue is
      reused before the tab re-reads the indexes (Refresh forces it).
    - ``timeout`` / ``max_index_bytes`` / ``max_wheel_bytes``: bounds on one
      index fetch (pypi.org's root listing is ~45 MB; a wheel is downloaded only
      when an index publishes no PEP 658 metadata).
    - ``ca_bundle`` / ``tls_verify``: mirror the ``[web]`` / Config-Sync HTTPS
      knobs for a private-CA / TLS-intercepting index host.
    """
    from . import marketplace as mp

    raw = read_config().get("marketplace")
    cfg = dict(raw) if isinstance(raw, dict) else {}

    def _strs(key: str, default: list[str]) -> list[str]:
        v = cfg.get(key)
        if not isinstance(v, list):
            return list(default)
        return [str(x).strip() for x in v if str(x).strip()]

    repos_raw = cfg.get("repos")
    repos = (
        [r for r in repos_raw if isinstance(r, dict)]
        if isinstance(repos_raw, list)
        else []
    )
    ca = str(cfg.get("ca_bundle") or "").strip()
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "auto_pip": bool(cfg.get("auto_pip", True)),
        "repos": repos,
        "patterns": _strs("patterns", list(mp.DEFAULT_PATTERNS)),
        "exclude": _strs("exclude", list(mp.DEFAULT_EXCLUDE)),
        "hide": _strs("hide", []),
        "require_runspec": bool(cfg.get("require_runspec", True)),
        "include_prerelease": bool(cfg.get("include_prerelease", False)),
        "cache_ttl": max(0.0, float(cfg.get("cache_ttl", mp.DEFAULT_CACHE_TTL_S) or 0)),
        "timeout": max(
            1.0, float(cfg.get("timeout", mp.DEFAULT_TIMEOUT_S) or mp.DEFAULT_TIMEOUT_S)
        ),
        "max_index_bytes": max(
            1,
            int(
                cfg.get("max_index_bytes", mp.DEFAULT_MAX_INDEX_BYTES)
                or mp.DEFAULT_MAX_INDEX_BYTES
            ),
        ),
        "max_wheel_bytes": max(
            1,
            int(
                cfg.get("max_wheel_bytes", mp.DEFAULT_MAX_WHEEL_BYTES)
                or mp.DEFAULT_MAX_WHEEL_BYTES
            ),
        ),
        "ca_bundle": ca or None,
        "tls_verify": bool(cfg.get("tls_verify", True)),
    }


def write_marketplace(section: dict[str, Any] | None) -> None:
    """Replace the ``[marketplace]`` table in config.toml, writing only the
    non-default keys (an all-defaults section round-trips to no block at all).
    Every other config section is preserved."""
    from . import marketplace as mp

    sec = dict(section or {})

    def mutate(cfg: dict[str, Any]) -> None:
        table: dict[str, Any] = {}
        if sec.get("enabled") is False:
            table["enabled"] = False
        if sec.get("auto_pip") is False:
            table["auto_pip"] = False
        patterns = [
            str(p).strip() for p in (sec.get("patterns") or []) if str(p).strip()
        ]
        if patterns and patterns != list(mp.DEFAULT_PATTERNS):
            table["patterns"] = patterns
        if "exclude" in sec and isinstance(sec.get("exclude"), list):
            exclude = [str(p).strip() for p in sec["exclude"] if str(p).strip()]
            if exclude != list(mp.DEFAULT_EXCLUDE):
                table["exclude"] = exclude
        hide = [str(p).strip() for p in (sec.get("hide") or []) if str(p).strip()]
        if hide:
            table["hide"] = hide
        if sec.get("require_runspec") is False:
            table["require_runspec"] = False
        if sec.get("include_prerelease") is True:
            table["include_prerelease"] = True
        ttl = sec.get("cache_ttl")
        if ttl is not None and float(ttl) != float(mp.DEFAULT_CACHE_TTL_S):
            table["cache_ttl"] = float(ttl)
        for key, default in (
            ("timeout", mp.DEFAULT_TIMEOUT_S),
            ("max_index_bytes", mp.DEFAULT_MAX_INDEX_BYTES),
            ("max_wheel_bytes", mp.DEFAULT_MAX_WHEEL_BYTES),
        ):
            v = sec.get(key)
            if v is not None and float(v) != float(default):
                table[key] = type(default)(v)
        ca = str(sec.get("ca_bundle") or "").strip()
        if ca:
            table["ca_bundle"] = ca
        if sec.get("tls_verify") is False:
            table["tls_verify"] = False
        repos = []
        for r in sec.get("repos") or []:
            if not isinstance(r, dict):
                continue
            repo = mp.repo_from_dict(r)
            if repo is not None:
                repos.append(mp.repo_to_dict(repo))
        if repos:
            table["repos"] = repos
        if table:
            cfg["marketplace"] = table
        else:
            cfg.pop("marketplace", None)

    update_config(mutate)


def marketplace_cache_path() -> Path:
    """The machine-local catalogue cache (never synced)."""
    return app_dir() / "marketplace_cache.json"


def read_voice() -> dict[str, Any]:
    """The ``[voice]`` settings for in-app dictation (the composer's mic button,
    Moonshine Voice via the ``[voice]`` extra), with defaults.

    - ``enabled`` (default true): master switch — false hides the mic button.
    - ``language`` (default ``"en"``): Moonshine language code. English is the
      MIT-licensed streaming model; other languages may fall under Moonshine's
      non-commercial Community License — check before enabling fleet-wide.
    - ``model`` (default ``medium-streaming``): streaming model size —
      ``tiny-streaming`` / ``base-streaming`` / ``small-streaming`` /
      ``medium-streaming`` (a bare size such as ``"small"`` is accepted).
    - ``model_dir``: path to a **vendored** model directory. Set on a fleet that
      blocks outbound downloads — the engine then never fetches anything. Unset
      ⇒ Moonshine downloads the model from ``download.moonshine.ai`` on first use
      into its own cache (``MOONSHINE_VOICE_CACHE`` overrides the location).
    - ``device``: capture device index or name (default: system default mic).
    - ``update_interval`` (default 0.5 s): how often partial text refreshes.
    - ``silence_timeout`` (default 8 s): the mic turns itself off after this many
      seconds with no recognised speech; ``0`` keeps listening until the operator
      stops it or sends.
    - ``keep_loaded`` (default true): keep the speech engine resident between
      sessions so the second mic press opens instantly (costs the model's RAM
      while idle); ``false`` reloads it on every press and frees it after.

    Edited from Settings → Voice (``VoiceMixin.save_voice_config``).
    """
    raw = read_config().get("voice")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    model_dir = str(cfg.get("model_dir") or "").strip()
    device: Any = cfg.get("device")
    if isinstance(device, str):
        device = device.strip() or None
    elif not isinstance(device, int) or isinstance(device, bool):
        device = None
    try:
        interval = float(cfg.get("update_interval", 0.5) or 0.5)
    except (TypeError, ValueError):
        interval = 0.5
    try:
        silence = float(cfg.get("silence_timeout", 8.0))
    except (TypeError, ValueError):
        silence = 8.0
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "language": str(cfg.get("language") or "en").strip() or "en",
        "model": str(cfg.get("model") or "").strip(),
        "model_dir": model_dir or None,
        "device": device,
        "update_interval": min(5.0, max(0.1, interval)),
        "silence_timeout": min(300.0, max(0.0, silence)),
        "keep_loaded": bool(cfg.get("keep_loaded", True)),
    }


def read_improve() -> dict[str, Any]:
    """The ``[improve]`` settings for the Improve (evaluation + problem-report)
    feature, with defaults applied.

    Lives in ``config.toml``, so it rides Config Sync / the config seed
    automatically — a white-label deploy can bundle ``developer_emails`` and
    ``product_name`` in its seed so a fresh install already knows who its reports
    are for.

    - ``enabled`` (default true): whether the Improve tab + agent tool are offered.
    - ``product_name``: brand shown in the report + mail subject (falls back to the
      resolved app title when blank).
    - ``developer_emails``: recipients the report is addressed to and the mailto
      pre-fills. The console never emails on its own — it only opens the operator's
      default mail client (a mailto), and the developers fold any learnings back
      into their sync repo / seed however they want.
    - ``window_days`` (default 30): how far back to mine conversations + history.
    - ``max_conversations`` (default 200): cap on conversations sampled per run.
    - ``max_incidents`` (default 40): cap on flagged incidents in a report.
    - ``llm_synthesis`` (default true): whether the LLM-judge recommendation pass is
      offered (still needs a configured provider to actually run)."""
    raw = read_config().get("improve")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    emails = cfg.get("developer_emails")
    if isinstance(emails, str):
        parts = [e.strip() for e in re.split(r"[,;\s]+", emails) if e.strip()]
    elif isinstance(emails, list):
        parts = [str(e).strip() for e in emails if str(e).strip()]
    else:
        parts = []
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "product_name": str(cfg.get("product_name") or "").strip(),
        "developer_emails": parts,
        "window_days": max(1, int(cfg.get("window_days", 30) or 30)),
        "max_conversations": max(1, int(cfg.get("max_conversations", 200) or 200)),
        "max_incidents": max(1, int(cfg.get("max_incidents", 40) or 40)),
        "llm_synthesis": bool(cfg.get("llm_synthesis", True)),
    }


def read_local() -> dict[str, Any]:
    """The ``[local]`` settings for the local machine's venv auto-discovery.

    ``venv_globs`` (default ``[]``) — glob patterns whose matched directories are
    each scanned for a local venv (probed for ``Scripts/runspec.exe`` /
    ``bin/runspec``) and surfaced under the synthetic ``local`` host alongside the
    console's own venv. Empty ⇒ no scanning ⇒ only the console's own venv (today's
    behavior, byte-identical). This lives in config.toml on purpose so the
    *convention* rides Config Sync / the config seed — one versioned glob (e.g.
    ``C:\\venvs\\*``) works on every identical machine, each resolving its own real
    venvs, unlike per-machine absolute paths which can't be controlled fleet-wide.
    See :func:`hosts.resolve_local_venvs`."""
    raw = read_config().get("local")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    globs_raw = cfg.get("venv_globs")
    globs = (
        [str(g).strip() for g in globs_raw if str(g).strip()]
        if isinstance(globs_raw, list)
        else []
    )
    return {"venv_globs": globs}


def read_tool_visibility() -> dict[str, Any]:
    """The ``[tool_visibility]`` settings for hiding runnable tools from surfaces.

    Two independent glob lists let the operator curate which runnables appear
    where (matched against the runnable *name*, with optional ``group:<glob>`` /
    ``host:<glob>`` qualifiers to hide a whole group/host):

    - ``hidden_from_agent`` — dropped from the chat/trigger/schedule **agent
      toolset** only; the runnable stays usable in the Forms tab and the
      room/self-service catalog.
    - ``hidden_from_all`` — dropped **everywhere** in the console: the agent
      toolset *and* the Forms tab *and* the catalogs.

    So agent surfaces filter by the *union* of both lists; Forms/catalog surfaces
    filter by ``hidden_from_all`` only. Empty/missing ⇒ hide nothing (today's
    behavior, byte-identical). This is **curation, not a safety block** — a hidden
    runnable is still installed and directly runnable (unlike the denylist, which
    refuses execution). It lives in config.toml on purpose so the lists ride
    Config Sync / the config seed — a corporate seed can ship a baseline
    ``[tool_visibility]`` to hide capabilities a fleet can't use (e.g. Office 365
    runnables where Graph is blocked). See :func:`bridge_utils.runnable_hidden`."""
    raw = read_config().get("tool_visibility")
    cfg = dict(raw) if isinstance(raw, dict) else {}

    def _list(key: str) -> list[str]:
        val = cfg.get(key)
        if not isinstance(val, list):
            return []
        return [str(p).strip() for p in val if str(p).strip()]

    return {
        "hidden_from_agent": _list("hidden_from_agent"),
        "hidden_from_all": _list("hidden_from_all"),
    }


def read_sandbox() -> dict[str, Any]:
    """The ``[sandbox]`` settings for the WASM/WASI code-execution sandbox behind
    ``run_python_sandboxed`` / ``run_sql_sandboxed`` (see :mod:`sandbox`).

    ``enabled`` (default true) — a kill switch mirroring ``[workspace] code_exec``;
    either being false drops the tools. ``mem_mb`` (default 512, clamped ≥ 16) —
    the guest memory ceiling. ``fuel`` (default unset/``None``) — an optional
    wasmtime CPU-fuel cap; when unset the wall-clock ``[workspace] exec_timeout``
    is the runaway guard. ``wasm_path`` — an explicit path to a WASI CPython
    ``python.wasm`` (overrides the app-data cache; for air-gapped installs).
    ``auto_download`` (default true) — fetch the pinned binary on first use when no
    local one is found. ``url`` / ``sha256`` override the pinned download source
    (private mirror)."""
    raw = read_config().get("sandbox")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    fuel_raw = cfg.get("fuel")
    fuel: int | None
    try:
        fuel = int(str(fuel_raw)) if fuel_raw not in (None, "", False) else None
    except (TypeError, ValueError):
        fuel = None
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "mem_mb": max(16, int(cfg.get("mem_mb", 512) or 512)),
        "fuel": fuel if (fuel is None or fuel > 0) else None,
        "wasm_path": str(cfg.get("wasm_path") or "").strip() or None,
        "auto_download": bool(cfg.get("auto_download", True)),
        "url": str(cfg.get("url") or "").strip() or None,
        "sha256": str(cfg.get("sha256") or "").strip() or None,
    }


def read_telemetry() -> dict[str, Any]:
    """The ``[telemetry]`` settings for OpenTelemetry (OTLP) agent tracing.

    ``enabled`` defaults **false** — telemetry exports spans to an external
    backend (LangSmith / Langfuse / Phoenix / Jaeger / Tempo …), so it is
    strictly opt-in. ``endpoint`` is the OTLP traces URL; ``headers`` is a TOML
    sub-table of extra HTTP headers (API key / project for LangSmith).
    ``protocol`` (``http/protobuf``) / ``service_name`` / ``sample_ratio``
    (clamped 0–1) tune the exporter and sampler. ``ca_bundle`` (path) and
    ``tls_verify`` mirror the Config-Sync / web HTTPS knobs for a private CA /
    TLS-intercepting proxy. ``capture_content`` defaults **false**: with it off,
    spans carry structure only (models, token counts, tool names, decisions,
    durations) — never prompt / completion / tool-argument *text*, which may hold
    secrets. ``max_content_chars`` bounds each captured content attribute (prompt
    / completion / tool arguments); **0 (default) = no limit**, so a trace shows
    the *full* prompt rather than a slice — raise a ceiling only to cap span size.
    Any value the OTel SDK also reads from a standard ``OTEL_*`` env var
    is passed through only when set here, so config wins and env fills the gaps.

    ``api_key_command`` (with ``api_key_header``, default ``x-api-key``) resolves
    the backend's API key **at runtime** from the command's stdout and injects it
    under that header — so a config seed / git-sync ships the *command* (e.g.
    ``keyring get …`` / ``vault read …`` / a bundled venv helper), never the key
    in the clear. Mirrors ``[llm] api_key_command`` (see ``adapters/base.py``).

    ``[telemetry.metrics]`` (nested) is the parallel **OTLP metrics** export
    (per-tool-run and per-turn counters/histograms; see ``observability`` +
    :func:`_telemetry_metrics_config`). It is a **separate signal with its own
    endpoint and auth**, defaulting off, so metrics can go to a *different*
    backend than traces — e.g. traces direct to LangSmith (which ingests OTLP
    traces, not metrics) while metrics go to a Grafana / Prometheus OTLP endpoint
    or a local collector. See the nested block under the returned ``metrics`` key.

    ``service_name`` defaults to the install's **branded app slug**
    (:func:`app_slug` — the same source that names the config directory and the
    window title), so a white-label build reports its own brand as the OTLP
    ``service.name`` with no extra config; a default install still gets
    ``runspec-console``. Set it explicitly to override. ``label`` is the
    per-instance identifier that becomes ``service.instance.id`` on every span and
    metric — it defaults to the machine hostname (so each console is its own clean
    time series), and you set it to run **several distinct instances** (e.g. more
    than one console on one machine) as separate, named streams. The console also
    stamps ``host.name`` and ``process.owner`` (OS username) on the resource
    automatically (see ``observability._build_resource``)."""
    raw = read_config().get("telemetry")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    hdrs = cfg.get("headers")
    ca = str(cfg.get("ca_bundle") or "").strip() or None
    tls = bool(cfg.get("tls_verify", True))
    # Default the OTLP service.name to the branded app slug (config-dir / window-
    # title source), so a white-label install is self-identifying; an explicit
    # value in config.toml still wins.
    service = str(cfg.get("service_name") or app_slug()).strip()
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "endpoint": str(cfg.get("endpoint") or "").strip() or None,
        "headers": (
            {str(k): str(v) for k, v in hdrs.items()} if isinstance(hdrs, dict) else {}
        ),
        "protocol": str(cfg.get("protocol") or DEFAULT_TELEMETRY_PROTOCOL).strip(),
        "service_name": service,
        # Per-instance label → service.instance.id (blank ⇒ hostname at init).
        "label": str(cfg.get("label") or "").strip(),
        "sample_ratio": min(1.0, max(0.0, float(cfg.get("sample_ratio", 1.0) or 1.0))),
        "ca_bundle": ca,
        "tls_verify": tls,
        "capture_content": bool(cfg.get("capture_content", False)),
        "max_content_chars": max(0, int(cfg.get("max_content_chars", 0) or 0)),
        "api_key_command": str(cfg.get("api_key_command") or "").strip() or None,
        "api_key_header": str(cfg.get("api_key_header") or "x-api-key").strip(),
        # Keychain-backed auth (mirrors [[mcp.servers]] credential): a named
        # Credentials id whose secret is injected as a header at export init —
        # userpass ⇒ Authorization: Basic, token ⇒ Authorization: Bearer, or the
        # raw secret under `credential_header` (e.g. x-api-key for LangSmith).
        # `header_credentials` sends several secrets under several headers. Only
        # ids are stored; the secret stays in the OS keychain. The console
        # resolves these before init (bridge `_resolve_telemetry_credentials`),
        # so a per-user API key set up during onboarding drives the export.
        "credential": str(cfg.get("credential") or "").strip(),
        "credential_header": str(cfg.get("credential_header") or "").strip(),
        "header_credentials": _read_header_credentials(cfg.get("header_credentials")),
        # Native eval export: when the OTLP endpoint is an Arize Phoenix instance,
        # also POST each eval verdict as a span annotation (`/v1/span_annotations`)
        # so pass/fail shows as a first-class evaluation, not just a filterable
        # attribute. Off by default; a no-op for a non-Phoenix backend.
        "eval_annotations": bool(cfg.get("eval_annotations", False)),
        "metrics": _telemetry_metrics_config(
            cfg.get("metrics"),
            parent_ca=ca,
            parent_tls=tls,
            service_name=service,
        ),
    }


def _telemetry_metrics_config(
    raw: Any, *, parent_ca: str | None, parent_tls: bool, service_name: str
) -> dict[str, Any]:
    """Normalise the nested ``[telemetry.metrics]`` block into a metrics-export
    config dict, with defaults applied.

    Metrics are a **distinct OTLP signal with an independent destination**, so
    ``endpoint`` and auth (``headers`` / ``api_key_command`` / ``api_key_header``)
    are metrics-specific and do **not** inherit from the trace ``[telemetry]``
    values. This is deliberate and security-motivated: the common split — traces
    to LangSmith, metrics to a Grafana/Prometheus OTLP endpoint — must not silently
    ship the *traces* backend's API key to the *metrics* backend. Point metrics
    somewhere and you configure its auth there.

    The exceptions are the two **non-secret transport-trust** knobs, which *do*
    fall back to the trace values because a private CA / TLS-intercepting proxy is
    an environment property, not a per-backend secret:

    - ``ca_bundle`` — inherits the trace ``ca_bundle`` when unset here.
    - ``tls_verify`` — inherits the trace ``tls_verify`` when unset here.

    ``service_name`` is shared with traces (it names the app, not the backend).
    ``endpoint`` empty ⇒ ``None`` ⇒ the OTLP exporter falls through to its own
    ``OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`` / default (``…/v1/metrics``). Note the
    OTLP/HTTP metrics URL path (``/v1/metrics``) differs from traces
    (``/v1/traces``), which is a second reason the endpoints can't be shared.

    ``enabled`` defaults **false** (independent of trace ``enabled``, so metrics
    and traces switch on separately). ``export_interval`` (seconds, default 60,
    min 1) is how often the ``PeriodicExportingMetricReader`` flushes."""
    cfg = dict(raw) if isinstance(raw, dict) else {}
    hdrs = cfg.get("headers")
    ca = str(cfg.get("ca_bundle") or "").strip()
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "endpoint": str(cfg.get("endpoint") or "").strip() or None,
        "headers": (
            {str(k): str(v) for k, v in hdrs.items()} if isinstance(hdrs, dict) else {}
        ),
        # Transport trust: inherit from [telemetry] unless overridden here.
        "ca_bundle": (ca or None) if ca else parent_ca,
        "tls_verify": bool(cfg.get("tls_verify", parent_tls)),
        # Auth: metrics-specific, no cross-signal fallback (see docstring).
        "api_key_command": str(cfg.get("api_key_command") or "").strip() or None,
        "api_key_header": str(cfg.get("api_key_header") or "x-api-key").strip(),
        # Keychain-backed auth, metrics-specific (no cross-signal fallback) — the
        # same convention as the trace section: a Credentials id + optional header,
        # resolved to a secret header before init. So metrics → Grafana can carry
        # its own key while traces → LangSmith carry theirs.
        "credential": str(cfg.get("credential") or "").strip(),
        "credential_header": str(cfg.get("credential_header") or "").strip(),
        "header_credentials": _read_header_credentials(cfg.get("header_credentials")),
        "service_name": service_name,
        "export_interval": max(
            1, int(cfg.get("export_interval", DEFAULT_METRIC_EXPORT_INTERVAL) or 60)
        ),
    }


def _read_header_credentials(raw: Any) -> list[dict[str, str]]:
    """Normalise a server's ``header_credentials`` — an array of
    ``{header, credential}`` bindings that send a keychain-backed credential's
    secret under a named HTTP header at connect time. Each entry needs both a
    non-empty ``header`` name and a ``credential`` id; malformed/partial entries
    are dropped. Only the id is kept here — the secret stays in the keychain."""
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for b in raw:
        if not isinstance(b, dict):
            continue
        header = str(b.get("header") or "").strip()
        credential = str(b.get("credential") or "").strip()
        if header and credential:
            out.append({"header": header, "credential": credential})
    return out


def read_mcp() -> dict[str, Any]:
    """The ``[mcp]`` settings — external MCP servers the chat agent connects to.

    The console is normally an MCP *server* (``runspec serve`` publishes each
    runnable as a leaf tool); this section makes it an MCP *client*, folding the
    tools of external servers into the agent's toolset (gated by the normal
    autonomy/confirm system). Requires the optional ``mcp`` extra
    (``pip install runspec-console[mcp]``); a locked-down deployment simply
    leaves ``[[mcp.servers]]`` empty (or sets ``[mcp] enabled = false``) and the
    manager stays inert.

    ``enabled`` (default true) is the section kill switch. ``call_timeout`` /
    ``connect_timeout`` (seconds) bound a single tool call and the initial
    connect. Each ``[[mcp.servers]]`` entry has:

    * ``name`` (required) — a short id; namespaces the tools (``mcp_<name>_…``).
    * ``transport`` — ``stdio`` (spawn ``command``), ``http`` (streamable-HTTP at
      ``url``), or ``sse`` (``url``). Inferred from ``command``/``url`` if unset.
    * ``command`` / ``args`` / ``env`` / ``cwd`` — for a ``stdio`` server.
    * ``url`` / ``headers`` — for an ``http`` / ``sse`` server.
    * ``enabled`` (default true) — per-server toggle.
    * ``call_timeout`` / ``connect_timeout`` — per-server overrides of the
      section defaults above (seconds). A positive value overrides just this
      server's call / connect deadline; absent (or non-positive) ⇒ inherit the
      ``[mcp]`` default — so a single slow server can be given a longer leash
      without loosening the whole fleet.
    * ``autonomy`` — ``autonomous`` / ``confirm`` / ``manual`` override for this
      server's tools. Unset ⇒ ``confirm`` (an external tool is a side effect
      until the operator trusts the server): trust is opt-in, as elsewhere.
    * ``credential`` — a named-credential id (Settings → Credentials). Its
      secret (keychain-backed) is resolved at connect time: for an ``http`` /
      ``sse`` server it becomes an ``Authorization: Bearer`` header (``Basic``
      for a ``userpass`` credential), or the raw value under ``credential_header``
      if set; for a ``stdio`` server it is injected as the credential's
      name-derived env vars. Only the id is stored here (seed/sync-safe — the
      metadata rides ``runspec_credentials.toml``, the secret never leaves the
      keychain).
    * ``credential_header`` — override the header the credential's secret is sent
      under (e.g. ``X-API-Key``); default ``Authorization`` (Bearer/Basic).
    * ``header_credentials`` — an array of ``{header, credential}`` bindings for a
      server that needs **more than one** secret header (the single ``credential``
      above only fills one). Each binding sends the named credential's raw secret
      under its ``header`` name, resolved from the keychain at connect time — so a
      server needing e.g. a Jira PAT *and* a Confluence PAT carries two bindings
      (``X-Jira-PAT`` → the Jira credential, ``X-Confluence-PAT`` → the Confluence
      one) with only the ids in config.toml, never the tokens. http/sse only; an
      explicit literal ``headers`` entry of the same name wins (never overwritten).
    * ``proxy`` — an HTTP CONNECT proxy URL (``http://proxy:8080``, scheme
      optional) this server is reached through, so servers on different corporate
      networks each get their own egress. Mirrors ``[ssh] proxy`` / Config Sync's
      ``proxy``. Applies to ``http`` / ``sse``; for ``stdio`` it is exported to
      the subprocess as ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY``."""

    def _server_timeout(v: Any) -> float | None:
        """A per-server timeout override: a positive number, else ``None`` to
        inherit the section default. TOML can't store ``None`` (an absent key is
        the inherit case), and a non-positive value is treated as "inherit" too."""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

    raw = read_config().get("mcp")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    servers: list[dict[str, Any]] = []
    raw_servers = cfg.get("servers")
    if isinstance(raw_servers, list):
        for s in raw_servers:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            if not name:
                continue
            command = str(s.get("command") or "").strip()
            url = str(s.get("url") or "").strip()
            transport = str(s.get("transport") or "").strip().lower()
            if transport not in ("stdio", "http", "sse"):
                transport = "stdio" if command else ("http" if url else "stdio")
            env_raw = s.get("env")
            env = (
                {str(k): str(v) for k, v in env_raw.items()}
                if isinstance(env_raw, dict)
                else {}
            )
            hdr_raw = s.get("headers")
            headers = (
                {str(k): str(v) for k, v in hdr_raw.items()}
                if isinstance(hdr_raw, dict)
                else {}
            )
            header_credentials = _read_header_credentials(s.get("header_credentials"))
            autonomy = str(s.get("autonomy") or "").strip().lower()
            if autonomy not in ("autonomous", "confirm", "manual"):
                autonomy = ""
            servers.append(
                {
                    "name": name,
                    "transport": transport,
                    "command": command,
                    "args": [str(a) for a in (s.get("args") or [])],
                    "env": env,
                    "cwd": str(s.get("cwd") or "").strip(),
                    "url": url,
                    "headers": headers,
                    "header_credentials": header_credentials,
                    "enabled": bool(s.get("enabled", True)),
                    "autonomy": autonomy,
                    "credential": str(s.get("credential") or "").strip(),
                    "credential_header": str(s.get("credential_header") or "").strip(),
                    "proxy": str(s.get("proxy") or "").strip(),
                    "call_timeout": _server_timeout(s.get("call_timeout")),
                    "connect_timeout": _server_timeout(s.get("connect_timeout")),
                }
            )
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "servers": servers,
        "call_timeout": max(1.0, float(cfg.get("call_timeout", 60) or 60)),
        "connect_timeout": max(1.0, float(cfg.get("connect_timeout", 20) or 20)),
    }


def read_mcp_server() -> dict[str, Any]:
    """The ``[mcp_server]`` settings — the console's **own** in-process MCP server
    that an external coding agent (Claude Code, Cursor, …) connects to.

    This is the mirror of ``[mcp]`` (which makes the console an MCP *client*): here
    the console is an MCP *server*, publishing its fleet runnables as leaf tools
    over streamable-HTTP on localhost. A ``confirm``-level call from the agent
    **parks in the console's confirm-card UI** (the human approves it there); both
    agent-initiated and human-initiated runs land in the same History. Requires the
    optional ``mcp`` extra (``pip install runspec-console[mcp]``).

    - ``enabled`` (default **false**) — opt-in: it opens a listening socket.
    - ``host`` (default ``127.0.0.1``) — bound to loopback; the primary security
      boundary. Changing it off loopback is not recommended.
    - ``port`` (default 8765) — the actual value is written to a discovery file in
      the app dir so the client's MCP config need not hard-code it.
    - ``token`` — an optional bearer token; when set it is required on every
      request (``Authorization: Bearer <token>``) and written to the discovery
      file. Localhost is the primary bound; the token guards against other local
      processes.
    - ``default_autonomy`` — optional floor applied to *every* tool
      (``autonomous`` / ``confirm`` / ``supervised`` / ``manual``): the stricter of
      it and the runnable's own declared autonomy wins (most-restrictive, as
      elsewhere). Unset ⇒ use each runnable's own autonomy.
    - ``confirm_wait`` (seconds, default 50) — how long a parked ``confirm`` call
      blocks for the operator's approval before returning a graceful, *resumable*
      "awaiting approval" result to the agent (rather than the runnable running
      late/orphaned). Kept **under** an MCP client's own per-tool-call timeout
      (Claude Code's ``MCP_TOOL_TIMEOUT`` defaults to 60s), so the agent sees our
      clean message instead of the client's own timeout error; on the agent's
      retry a fresh confirm is raised. Raise it if you also raise the client's
      timeout.
    - ``tool_style`` (``leaf`` default / ``runnable``) — how runnables with
      subcommands are presented. ``leaf`` is serve-identical (one MCP tool per
      subcommand leaf, e.g. ``jira_get-issue``…). ``runnable`` emits one tool per
      runnable with a ``command`` argument selecting the subcommand — far fewer
      tools for a multi-verb fleet, at the cost of per-subcommand arg typing.
      Autonomy is still resolved per chosen subcommand, so gating is unchanged.
    - ``expose_builtins`` (default **false**) — also publish the console's own
      **console-specific** built-in agent tools (status, run history, memory +
      runbooks, host→local file transfer, Outlook, the action tools) so the
      external agent gets the console's ops/fleet powers, not just the runnables.
      Opt-in: it widens what an external process can drive. Each built-in keeps its
      own autonomy (reads run, writes park in the confirm card). The config +
      search-meta tools and the workspace/exec/web tools are never exposed."""
    raw = read_config().get("mcp_server")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    autonomy = str(cfg.get("default_autonomy") or "").strip().lower()
    if autonomy not in _MCP_SERVER_AUTONOMY_LEVELS:
        autonomy = ""
    tool_style = str(cfg.get("tool_style") or "").strip().lower()
    if tool_style not in _MCP_SERVER_TOOL_STYLES:
        tool_style = DEFAULT_MCP_SERVER_TOOL_STYLE
    expose_builtins = bool(cfg.get("expose_builtins", False))
    try:
        port = int(cfg.get("port", DEFAULT_MCP_SERVER_PORT) or DEFAULT_MCP_SERVER_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_MCP_SERVER_PORT
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "host": str(cfg.get("host") or DEFAULT_MCP_SERVER_HOST).strip()
        or DEFAULT_MCP_SERVER_HOST,
        "port": max(1, min(65535, port)),
        "token": str(cfg.get("token") or "").strip() or None,
        "default_autonomy": autonomy or None,
        "confirm_wait": max(1.0, float(cfg.get("confirm_wait", 50) or 50)),
        "tool_style": tool_style,
        "expose_builtins": expose_builtins,
    }


def _clean_mcp_server(raw: dict[str, Any]) -> dict[str, Any]:
    """One ``[[mcp.servers]]`` entry, normalised for *writing* — only set fields
    are emitted so config.toml stays tidy (empty strings / dicts / lists are
    dropped; ``enabled`` is written only when False since the default is True)."""
    name = str(raw.get("name") or "").strip()
    command = str(raw.get("command") or "").strip()
    url = str(raw.get("url") or "").strip()
    transport = str(raw.get("transport") or "").strip().lower()
    if transport not in ("stdio", "http", "sse"):
        transport = "stdio" if command else ("http" if url else "stdio")
    out: dict[str, Any] = {"name": name, "transport": transport}
    if transport == "stdio":
        if command:
            out["command"] = command
        args = [str(a) for a in (raw.get("args") or []) if str(a) != ""]
        if args:
            out["args"] = args
        env = {
            str(k): str(v) for k, v in (raw.get("env") or {}).items() if str(k).strip()
        }
        if env:
            out["env"] = env
        cwd = str(raw.get("cwd") or "").strip()
        if cwd:
            out["cwd"] = cwd
    else:
        if url:
            out["url"] = url
        headers = {
            str(k): str(v)
            for k, v in (raw.get("headers") or {}).items()
            if str(k).strip()
        }
        if headers:
            out["headers"] = headers
        header_credentials = _read_header_credentials(raw.get("header_credentials"))
        if header_credentials:
            out["header_credentials"] = header_credentials
    autonomy = str(raw.get("autonomy") or "").strip().lower()
    if autonomy in ("autonomous", "confirm", "manual"):
        out["autonomy"] = autonomy
    for key in ("credential", "credential_header", "proxy"):
        val = str(raw.get(key) or "").strip()
        if val:
            out[key] = val
    # Per-server timeout overrides — only a positive number is written; empty /
    # 0 / non-numeric means "inherit the section default", so it stays out of
    # config.toml (matching the read side that treats absent as inherit).
    for key in ("call_timeout", "connect_timeout"):
        raw_val = raw.get(key)
        try:
            num = float(raw_val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            num = 0.0
        if num > 0:
            out[key] = num
    if raw.get("enabled") is False:
        out["enabled"] = False
    return out


def write_mcp(section: dict[str, Any] | None) -> None:
    """Replace the ``[mcp]`` table (external MCP servers) in config.toml, or drop
    it entirely when there's nothing to persist. Only non-default fields are
    written, so an all-defaults section round-trips to no ``[mcp]`` block at all.
    Every other config section is preserved."""

    def mutate(cfg: dict[str, Any]) -> None:
        servers = [
            _clean_mcp_server(s)
            for s in (section or {}).get("servers", [])
            if isinstance(s, dict) and str(s.get("name") or "").strip()
        ]
        table: dict[str, Any] = {}
        if (section or {}).get("enabled") is False:
            table["enabled"] = False
        ct = (section or {}).get("call_timeout")
        if ct is not None and float(ct) != 60.0:
            table["call_timeout"] = float(ct)
        cct = (section or {}).get("connect_timeout")
        if cct is not None and float(cct) != 20.0:
            table["connect_timeout"] = float(cct)
        if servers:
            table["servers"] = servers
        if table:
            cfg["mcp"] = table
        else:
            cfg.pop("mcp", None)  # all-defaults, no servers → no section

    update_config(mutate)


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


def read_chat_history() -> dict[str, Any]:
    """The ``[chat_history]`` settings, with defaults applied.

    ``enabled`` (default **true**): persist each chat conversation to disk so the
    operator can reopen/resume past chats. ``max_conversations`` / ``max_age_days``
    bound the on-disk store (retention; pruned on save + startup). Storage is
    machine-local (``conversations_dir()``) — never synced."""
    raw = read_config().get("chat_history")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "max_conversations": int(cfg.get("max_conversations", 200) or 0),
        "max_age_days": int(cfg.get("max_age_days", 90) or 0),
        # Resume-time compaction: keep the opening ``resume_keep_first`` turns and
        # the recent ``resume_keep_recent`` turns verbatim when resuming a long
        # chat; the overflow in between is condensed to a short note (default) —
        # or, with ``compact_on_resume``, an LLM summary — so a long thread doesn't
        # blow the context window. ``resume_keep_first`` = 0 keeps no opening turn.
        "resume_keep_first": max(0, int(cfg.get("resume_keep_first", 1) or 0)),
        "resume_keep_recent": int(cfg.get("resume_keep_recent", 20) or 0),
        "compact_on_resume": bool(cfg.get("compact_on_resume", False)),
    }


def read_history() -> dict[str, Any]:
    """The ``[history]`` settings for the History tab, with defaults applied.

    ``cache`` (default **true**): read remote hosts' history from the console's
    local cache (``history_cache.db`` — machine-local, never synced), kept current
    by pulling only what changed in each venv's runs index (core runspec >= 0.63.0).
    ``false`` restores reading every remote host over SSH on each refresh."""
    raw = read_config().get("history")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    return {"cache": bool(cfg.get("cache", True))}


def history_cache_path() -> Path:
    """The History cache file — machine-local, derived, safe to delete."""
    return app_dir() / "history_cache.db"


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
        text = _dumps_config(_strip_none(data))
        path = config_path()
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def _is_scalar_or_scalar_list(value: Any) -> bool:
    """A value that renders cleanly inside a ``[[table]]`` block: a scalar, or a
    list of scalars (e.g. ``folders = ["a", "b"]``) — but not a nested table or a
    list of tables."""
    if isinstance(value, dict):
        return False
    if isinstance(value, list):
        return all(not isinstance(e, (dict, list)) for e in value)
    return True


def _is_flat_table_array(value: Any) -> bool:
    """True for a non-empty list whose every element is a *flat* table (a dict
    whose values are scalars or scalar lists) — i.e. a top-level array-of-tables
    such as ``[[watch]]`` (whose entries may carry a ``folders`` array)."""
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(isinstance(row, dict) for row in value)
        and all(_is_scalar_or_scalar_list(v) for row in value for v in row.values())
    )


def _dumps_config(data: dict[str, Any]) -> str:
    """Serialise like ``tomli_w.dumps`` but emit top-level arrays-of-tables as
    idiomatic ``[[key]]`` blocks placed *after* the ordinary sections.

    ``tomli_w`` (1.2.x) always renders an array-of-tables **inline**
    (``watch = [ { … } ]``). A bare top-level key must, per TOML, precede every
    ``[section]``, so that inline array floats to the very top of the file —
    out of place above ``[mailbox]`` and friends, and an empty watch shows as a
    bare ``{}``. Splitting those keys out and appending real ``[[key]]`` blocks
    keeps the round-trip identical (they re-parse to the same list of dicts)
    while producing the tidy layout the config docs describe."""
    table_arrays = {k: v for k, v in data.items() if _is_flat_table_array(v)}
    if not table_arrays:
        return tomli_w.dumps(data)
    rest = {k: v for k, v in data.items() if k not in table_arrays}
    text = tomli_w.dumps(rest) if rest else ""
    for key, rows in table_arrays.items():
        for row in rows:
            body = tomli_w.dumps(row)  # "" for an empty (default-Inbox) entry
            text += f"\n[[{key}]]\n{body}"
    return text


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


# How long the footer's update check waits on `pip index versions` before giving
# up (and showing just the version, no "up to date" / "update available"). The
# version renders instantly; this only bounds the background indicator, so a
# generous default is safe — a slow PC / slow internal index needs the headroom.
DEFAULT_UPDATE_CHECK_TIMEOUT = 20.0


def update_check_timeout() -> float:
    """`[console] update_check_timeout` (seconds) — the pip index-query timeout
    for the footer update check. Default 20s; clamped to [1, 120]. Raise it when
    a slow machine or internal mirror makes the check time out and the footer
    never resolves to 'up to date'."""
    console = read_config().get("console")
    if isinstance(console, dict):
        raw = console.get("update_check_timeout")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return max(1.0, min(120.0, float(raw)))
    return DEFAULT_UPDATE_CHECK_TIMEOUT


# How long a tool-confirmation gate waits for the operator before the turn treats
# no-answer as a deny. Interactive chat keeps the historical 5-minute window;
# unattended trigger/schedule turns default much longer (the operator may be away
# when the trigger fires) and can be set to 0 to wait indefinitely.
DEFAULT_CONFIRM_TIMEOUT = 300.0
DEFAULT_TRIGGER_CONFIRM_TIMEOUT = 3600.0


def confirm_timeout(headless: bool = False) -> float:
    """Tool-confirmation timeout in seconds.

    ``headless=False`` (chat): ``[console] confirm_timeout`` (default 300).
    ``headless=True`` (trigger/schedule turns): ``[console] trigger_confirm_timeout``
    (default 3600). ``0`` (or negative) means wait indefinitely — the gate never
    auto-denies. A non-numeric/absent value falls back to the default."""
    key = "trigger_confirm_timeout" if headless else "confirm_timeout"
    default = DEFAULT_TRIGGER_CONFIRM_TIMEOUT if headless else DEFAULT_CONFIRM_TIMEOUT
    console = read_config().get("console")
    if isinstance(console, dict):
        raw = console.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            val = float(raw)
            return 0.0 if val <= 0 else val
    return default


def read_chat() -> dict[str, Any]:
    """The ``[chat]`` room-source table from config.toml ({} when unset).

    Connection config for the chat trigger source: ``host`` (the SSH host the
    room web app runs on), ``port`` (its localhost port on that host, reached
    over the tunnel), ``room``, and the console's display ``user``.
    """
    chat = read_config().get("chat")
    return dict(chat) if isinstance(chat, dict) else {}


def _normalize_folders(raw: dict[str, Any]) -> list[str]:
    """The normalized ``folders`` list for a ``[mailbox]``/``[[watch]]`` entry.

    Accepts the new ``folders`` array *and* the legacy scalar ``folder`` (read as
    a one-element list). Each path is normalised the same way — blank, missing, or
    ``"inbox"`` (case-insensitive) → ``"Inbox"`` — order-preserving deduped. An
    empty/absent list → ``["Inbox"]`` (Inbox only). Pure/testable."""
    entries = raw.get("folders")
    if not isinstance(entries, list):
        # Legacy scalar `folder` → a one-element list; missing → the Inbox.
        entries = [raw.get("folder")]
    out: list[str] = []
    for item in entries:
        folder = str(item or "").strip()
        if not folder or folder.lower() == "inbox":
            folder = "Inbox"
        if folder not in out:
            out.append(folder)
    return out or ["Inbox"]


def read_mailbox() -> dict[str, Any] | None:
    """The single ``[mailbox]`` the Outlook source monitors, or ``None`` when no
    ``[mailbox]`` table is configured (email monitoring off).

    One inbox, with an optional list of folders inside it::

        [mailbox]
        address = "helpdesk@corp.com"   # blank/absent → the default account
        folders = ["Tickets/Open", "Tickets/Alerts"]  # blank/absent → the Inbox
        label   = "Helpdesk"            # optional display label
        once_per_thread = true          # default reply-suppression for triggers

    A present-but-empty ``[mailbox]`` means "watch my default account's Inbox".
    Normalisation: ``address`` blank/missing → ``""`` (the default account);
    ``folders`` empty/missing → ``["Inbox"]``; each entry blank/``"inbox"``
    (case-insensitive) → ``"Inbox"``, deduped. The legacy scalar ``folder = "X"``
    is still read as ``folders = ["X"]``. ``folder`` is returned as ``folders[0]``
    for existing readers that expect a single folder.
    ``once_per_thread`` (default ``False``) is the mailbox-wide default for
    suppressing replies — a trigger inherits it unless it sets its own
    ``once_per_thread`` (see triggers.py). There is no queue here — queues are
    teams, managed in the room DB (see docs/design/teams-and-email-monitoring.md)."""
    raw = read_config().get("mailbox")
    if not isinstance(raw, dict):
        return None
    folders = _normalize_folders(raw)
    return {
        "address": str(raw.get("address") or "").strip(),
        "folders": folders,
        "folder": folders[0],
        "label": str(raw.get("label") or "").strip(),
        "once_per_thread": bool(raw.get("once_per_thread", False)),
    }


def _folders_for_write(mailbox: dict[str, Any]) -> list[str]:
    """The non-default ``folders`` list to emit for a mailbox/watch entry, or ``[]``
    when it is just the Inbox (so a bare "watch my Inbox" entry stays empty on
    disk). Accepts a ``folders`` list or the legacy scalar ``folder``; normalises
    and drops a lone ``"Inbox"``. Pure/testable."""
    folders = _normalize_folders(mailbox)
    if folders == ["Inbox"]:
        return []
    return folders


def write_mailbox(mailbox: dict[str, Any] | None) -> None:
    """Replace the ``[mailbox]`` table in config.toml (or drop it when ``mailbox``
    is ``None``), preserving every other section. Only set fields are emitted, so
    "watch my own Inbox" round-trips to an empty ``[mailbox]`` table. The folders
    list is written as ``folders = [...]`` (the legacy scalar ``folder`` is
    dropped); a lone Inbox emits no ``folders`` key."""

    def mutate(cfg: dict[str, Any]) -> None:
        if not mailbox:
            cfg.pop("mailbox", None)
            return
        address = str(mailbox.get("address") or "").strip()
        label = str(mailbox.get("label") or "").strip()
        folders = _folders_for_write(mailbox)
        entry: dict[str, Any] = {}
        if address:
            entry["address"] = address
        if folders:
            entry["folders"] = folders
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
    ``""`` = the default account; ``folders`` empty/missing → ``["Inbox"]``, each
    entry blank/``"inbox"`` → ``"Inbox"``; the legacy scalar ``folder`` still read
    as a one-element list) and returned as ``{address, folders, folder, label}``
    (``folder`` = ``folders[0]`` for single-folder readers).

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
        folders = _normalize_folders(item)
        out.append(
            {
                "address": str(item.get("address") or "").strip(),
                "folders": folders,
                "folder": folders[0],
                "label": str(item.get("label") or "").strip(),
            }
        )
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
            label = str(watch.get("label") or "").strip()
            folders = _folders_for_write(watch)
            entry: dict[str, Any] = {}
            if address:
                entry["address"] = address
            if folders:
                entry["folders"] = folders
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
        preserve      = ["config.toml:mailbox.folders", ...]  # keep local on pull
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


def read_seed_state() -> dict[str, Any]:
    """The machine-local package-seed state (`runspec_seed_state.toml`), or `{}`.

    Shape::

        [seed]
        applied = { "mycorp-console" = "1.4.0" }   # {dist: version} last applied
        last_applied = "2026-06-29T09:15:00Z"

    The `applied` stamp is what `Bridge.seed_apply` compares the freshly
    discovered stamp against — equal ⇒ no-op, different ⇒ re-assert."""
    path = seed_state_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            raw = dict(tomllib.load(f)).get("seed")
    except Exception:
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def write_seed_state(state: dict[str, Any]) -> None:
    """Write the package-seed state atomically (temp + os.replace).

    Standalone machine-local file — never part of config.toml and never synced,
    so a pull/seed on another machine can't clobber this machine's record."""
    with _CONFIG_LOCK:
        text = tomli_w.dumps({"seed": _strip_none(state)})
        path = seed_state_path()
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


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


def ai_enabled() -> bool:
    """`[console] ai` — whether the AI/agent features are enabled (default True).

    Set ``ai = false`` (typically from a config seed / Config Sync) to run the
    console as a non-AI fleet tool: no chat, no agent-mode triggers, no
    prompt/runbook/eval schedules, no model adapter ever built. The MCP server,
    Forms, History, saved runs, rule triggers and rooms keep working. Read once at
    bring-up (``Bridge._ai_enabled``) so the UI and the backend gates agree for
    the whole session — restart to apply a change. An absent or non-bool value
    means enabled, so a default install is unchanged. Deliberately **not** in the
    preserve lists: a seeded ``false`` re-asserts on every seed re-apply / sync
    pull. See docs/design/console-ai-off-switch.md."""
    console = read_config().get("console")
    if isinstance(console, dict):
        val = console.get("ai")
        if isinstance(val, bool):
            return val
    return True


# Default label on the disclaimer's acknowledge button when none is configured.
DEFAULT_DISCLAIMER_ACK_LABEL = "I acknowledge"


def read_disclaimer() -> dict[str, Any]:
    """The validated `[console.disclaimer]` table — the first-use AI-notice popup.

    A blocking acknowledgement notice for AI-enabled deployments that require the
    operator to accept a usage disclaimer before using the app. Read-only from
    config (a config seed / Config Sync ships it — there is **no** Settings editor,
    so an operator can't click their own compliance notice off), so it rides the
    seed and sync layers like `[console.theme]`.

    Keys (all optional):

    - ``enabled`` (default False): master switch. Off for a vanilla install; a
      corporate seed sets it true where the disclaimer is required.
    - ``title``: heading shown on the popup (default "Notice").
    - ``text``: the disclaimer body, rendered as **markdown** (so it can link to a
      full policy). An empty body means nothing to show, so it forces
      ``enabled`` false regardless — a disclaimer with no wording is meaningless.
    - ``acknowledge_label``: the accept-button label (default "I acknowledge").
    - ``version``: an optional wording version. Bumping it (or editing ``text``)
      re-prompts every operator, even those who acknowledged the prior wording —
      the acknowledgement is keyed to a hash of ``text`` **and** ``version``.
    - ``always_show`` (default False): re-show the notice on **every** app launch,
      ignoring the stored acknowledgement. The acknowledgement is still recorded
      (so it isn't lost), but the gate re-appears each launch — the once-per-launch
      behaviour some deployments want for a standing compliance reminder. (An
      in-session acknowledgement still dismisses it for that session.)

    Deliberately **not** in the preserve lists: a seeded value re-asserts on every
    seed re-apply / sync pull. See docs/design/console-disclaimer.md."""
    console = read_config().get("console")
    raw = console.get("disclaimer") if isinstance(console, dict) else None
    cfg = raw if isinstance(raw, dict) else {}
    text = str(cfg.get("text") or "").strip()
    title = str(cfg.get("title") or "").strip() or "Notice"
    label = (
        str(cfg.get("acknowledge_label") or "").strip() or DEFAULT_DISCLAIMER_ACK_LABEL
    )
    version = str(cfg.get("version") or "").strip()
    # A body is required: an enabled disclaimer with no wording is a no-op, so we
    # never let it block the app on empty text.
    enabled = bool(cfg.get("enabled", False)) and bool(text)
    return {
        "enabled": enabled,
        "title": title,
        "text": text,
        "acknowledge_label": label,
        "version": version,
        "always_show": bool(cfg.get("always_show", False)),
    }


def disclaimer_hash(text: str, version: str = "") -> str:
    """Stable content fingerprint of a disclaimer's wording (+ optional version).

    The acknowledgement is keyed to this, so any change to ``text`` or ``version``
    yields a new hash and re-prompts. A plain sha256 over the two joined with a
    separator that can't occur in the inputs' boundary; hex-encoded and truncated
    to keep the stored file small (collision risk is irrelevant — this is a
    change-detector, not a security primitive)."""
    payload = f"{version}\x00{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def read_disclaimer_ack() -> dict[str, Any]:
    """The machine-local disclaimer acknowledgement record, or ``{}`` when the
    operator has never acknowledged (see :func:`disclaimer_ack_path`). An
    unparseable file reads as empty — the operator is simply re-prompted, which is
    the safe direction."""
    path = disclaimer_ack_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return dict(tomllib.load(f))
    except Exception:
        return {}


def write_disclaimer_ack(data: dict[str, Any]) -> None:
    """Persist the disclaimer acknowledgement atomically (temp file + os.replace)."""
    path = disclaimer_ack_path()
    text = tomli_w.dumps(_strip_none(data))
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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


# Built-in default sound mapping (category -> token). A token is one of
# "synth:<name>" (bundled wav), "system:<Alias>" (Windows system sound),
# "file:<path>" (an operator's own .wav/.mp3), or "off". The Settings UI lets
# the operator override per category; an unset or garbage value falls back to
# these. Kept here so sounds.py stays config-free.
DEFAULT_SOUND_MAP = {
    "confirm": "synth:confirm",
    "trigger": "synth:trigger",
    "completion_success": "synth:success",
    "completion_error": "synth:error",
    "completion_stopped": "synth:stopped",
    "reply": "synth:reply",
    "message": "synth:message",
    "host_up": "synth:host_up",
    "host_down": "synth:host_down",
    "room_up": "synth:room_up",
    "room_down": "synth:room_down",
}


def sounds_config() -> dict[str, Any]:
    """The `[console.sounds]` table (or `{}` if unset)."""
    console = read_config().get("console")
    if isinstance(console, dict):
        sounds = console.get("sounds")
        if isinstance(sounds, dict):
            return sounds
    return {}


# White-label theme (`[console.theme]`) — read-only, set by a config seed or by
# hand. Rendered by the frontend (accent colour, watermark, sidebar brand name)
# and the app icon. See docs/design/console-theme.md.

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def theme_config() -> dict[str, Any]:
    """The `[console.theme]` table (or `{}` if unset)."""
    console = read_config().get("console")
    if isinstance(console, dict):
        theme = console.get("theme")
        if isinstance(theme, dict):
            return theme
    return {}


# config.toml sections we check for typo'd keys. Most sections have a dedicated
# read_* reader that validates itself; these are the ad-hoc ones read inline via
# `.get(...)` with no reader, where a misspelled key ([ssh] proxxy) is valid TOML
# and silently ignored — both at runtime and in any validator-based seed check.
# Each maps a section to its full recognised key set (see ssh_pool.pool_settings /
# refresh_settings + executor._make_ssh_client). WARN-ONLY + additive: a section
# not listed here is never checked (so a newer seed/synced config can't false-warn
# on an older console), and an unknown key is reported, never rejected — forward
# compatibility beats strictness for a distributed config. Extend by adding a
# section's key set here.
_SECTION_KEYS: dict[str, frozenset[str]] = {
    "ssh": frozenset(
        {
            "pool",
            "keepalive",
            "max_sessions",
            "idle_ttl",
            "max_concurrent",
            "backoff_base",
            "backoff_max",
            "connect_retries",
            "connect_retry_delay",
            "connect_timeout",
            "user",
            "identityFile",
            "binary",
            "proxy",
            "use_ssh_config",
            "rotate_retries",
            "rotate_jitter",
        }
    ),
    "refresh": frozenset({"workers", "interval", "jitter", "discovery_interval"}),
}


def validate_config(cfg: dict[str, Any]) -> list[str]:
    """Warnings for unknown keys in the schema-covered config.toml sections.

    Covers the ad-hoc sections (``[ssh]``, ``[refresh]``) that are read inline
    with ``.get`` and have no dedicated ``read_*`` reader, so a typo'd key would
    otherwise be silently ignored. Returns one human-readable warning per unknown
    key. **Warn-only and additive** — an unlisted section is skipped and an
    unknown key is reported, never rejected, so a newer seed/synced config on an
    older console is not broken. Never raises.
    """
    warnings: list[str] = []
    for section, known in _SECTION_KEYS.items():
        table = cfg.get(section)
        if not isinstance(table, dict):
            continue
        for key in table:
            if key not in known:
                warnings.append(f"[{section}] unknown key {key!r} (ignored)")
    return warnings


def valid_accent(value: Any) -> str | None:
    """Return ``value`` if it's a hex colour (``#rgb`` / ``#rrggbb``), else None.

    ``colorPrimary`` is an Ant token rather than raw CSS, but validating keeps a
    garbage value from poisoning the theme and bounds what reaches the icon's RGB
    math."""
    if isinstance(value, str) and _HEX_COLOR_RE.match(value.strip()):
        return value.strip()
    return None


def valid_image_ref(value: Any) -> str | None:
    """Return ``value`` if it's an http(s) or ``data:image/`` reference, else None.

    So ``watermark_logo`` can't smuggle a ``javascript:`` URL into an ``<img>``."""
    if isinstance(value, str):
        v = value.strip()
        if v.startswith(("https://", "http://", "data:image/")):
            return v
    return None


# How the content watermark is drawn. `tiled` = Ant's repeated/slanted mark
# (default); `banner` = a single large mark over the content area, upper third;
# `center` = a single large mark dead-centre. An unknown value falls back to
# `tiled` in the UI.
WATERMARK_STYLES: frozenset[str] = frozenset({"tiled", "banner", "center"})


def valid_watermark_style(value: Any) -> str | None:
    """Return ``value`` if it's a known `[console.theme] watermark_style`, else None."""
    if isinstance(value, str) and value.strip() in WATERMARK_STYLES:
        return value.strip()
    return None


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Expand a validated ``#rgb`` / ``#rrggbb`` hex to an (R, G, B) tuple."""
    h = value.lstrip("#")
    if len(h) == 3:  # #rgb → #rrggbb
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def title_bar_colors() -> tuple[int, int] | None:
    """The native Windows title-bar (caption, text) COLORREFs from
    `[console.theme] title_bar_color`, or ``None`` when unset/invalid (so the
    caller keeps the default runspec-blue caption).

    `title_bar_color` is an **independent** hex, deliberately separate from
    `accent_color`, so a brand can pair e.g. a blue accent UI with a red title
    bar. A Windows COLORREF is ``0x00BBGGRR`` (not the web ``#RRGGBB`` order).
    The caption text colour is picked black or white for contrast against the
    caption by perceived (Rec. 601) luminance. Pure / unit-testable — the
    ctypes DWM call that consumes these lives in ``app.py``."""
    raw = valid_accent(theme_config().get("title_bar_color"))
    if not raw:
        return None
    r, g, b = _hex_to_rgb(raw)
    caption = (b << 16) | (g << 8) | r
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    text = 0x000000 if luma > 150 else 0x00FFFFFF
    return caption, text


def sound_enabled() -> bool:
    """`[console.sounds] enabled` master toggle — play audio cues for confirm
    gates, run outcomes, triggers, and messages. Defaults to True (on)."""
    val = sounds_config().get("enabled")
    return val if isinstance(val, bool) else True


def sound_choice(category: str) -> str:
    """The configured sound token for a category (`"synth:<name>"` /
    `"system:<Alias>"` / `"file:<path>"` / `"off"`), falling back to the
    built-in default. Never raises; an unknown/garbage value yields the default
    for that category."""
    val = sound_choice_set(category)
    return val if val is not None else DEFAULT_SOUND_MAP.get(category, "off")


def sound_choice_set(category: str) -> str | None:
    """The *explicitly configured* token for a category, or ``None`` when the
    operator hasn't set one. The distinction matters for sound packs: an
    explicit token (including ``"off"``) wins over the pack; an unset category
    falls through to the pack file, then the built-in default."""
    val = sounds_config().get(category)
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def sound_volume() -> int:
    """`[console.sounds] volume` — 0–100, clamped, default 100. Applies to the
    synthesized + file/pack cues (played via MCI); Windows `system:` aliases
    have no per-sound volume."""
    val = sounds_config().get("volume")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return 100
    return max(0, min(100, int(val)))


def sound_pack() -> str:
    """`[console.sounds] pack` — a folder of category-named audio files
    (`confirm.wav`, `host_down.mp3`, …) applied for any category without an
    explicit token. Machine-local (a filesystem path — never synced). `""`
    when unset."""
    val = sounds_config().get("pack")
    return val.strip() if isinstance(val, str) else ""


def _toml_str(v: str) -> str:
    """Escape a Python string as a TOML basic string literal.

    TOML basic strings (double-quoted) recognise backslash escapes — meaning
    a Windows path like ``C:\\Program Files\\PuTTY\\plink.exe`` written
    verbatim is invalid TOML (``\\P`` is not a recognised escape) and
    ``tomllib`` will refuse to read the file back. Delegates to the shared
    audited serializer (escapes ``\\`` / ``"`` and every control char) so the
    round-trip preserves the original string.
    """
    return basic_string(v)


def _toml_scalar(v: Any) -> str:
    """Render a scalar (or list of scalars) as a TOML value."""
    return dump_scalar(v)


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
