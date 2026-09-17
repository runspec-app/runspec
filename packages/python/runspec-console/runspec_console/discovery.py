"""
discovery.py — discover runnables in a venv, locally or over SSH.

Fast path (both local and remote): **scan the venv's site-packages for
``runspec.toml`` files** and normalise them here with runspec's own loader —
one ``find``+``cat`` shell command per remote venv (no Python interpreter
starts on the host), an in-process glob for a local venv. This is byte-for-byte
what ``runspec local --format json`` emits (that command is ``load_raw`` per
package, printed as-is — inference is *not* applied there either), so the
runnables that come back are identical; it is just an order of magnitude
cheaper than starting Python and walking ``importlib.metadata`` on every host.

Fallback: ``<venv>/bin/runspec local --format json`` — used per venv whenever
the scan can't be trusted: the shell command fails or its output is not a scan
reply (a Windows remote, a non-POSIX shell), no ``runspec.toml`` was found, or
an **editable install** is present (its ``runspec.toml`` lives in a source tree
``direct_url.json`` points at, which the CLI already knows how to follow).

Returns a list of Runnable dicts matching the bridge/index.ts Runnable interface.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .hosts import scripts_dir, site_packages, venv_name
from .procutil import no_window_kwargs

logger = logging.getLogger(__name__)

# Dirs inside site-packages that never contain user packages
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "bin",
        "lib",
        "lib64",
        "include",
        "share",
    }
)


_SELF_EXCLUDE = frozenset({"runspec-console"})

# The scan also sees the core package's own ``runspec/runspec.toml`` (the CLI's
# dogfooded ``runspec`` control-plane runnable, whose ``bin/runspec`` binary of
# course exists). ``runspec local`` never lists it — it only walks dists that
# *require* runspec — so the scan excludes it by name to stay identical.
_SCAN_EXCLUDE = _SELF_EXCLUDE | frozenset({"runspec"})

# Markers in the remote scan reply. ``_SCAN_END`` doubles as the "this really
# was our scan command's complete output" check — anything without it (a
# Windows shell, a truncated stream, a plain ``[]``) falls back to the CLI.
_SCAN_BIN = "=====BIN"
_SCAN_EDITABLE = "=====EDITABLE"
_SCAN_FILE = "=====FILE "
_SCAN_END = "=====END"


def _scan_command(runspec_path: str) -> str:
    """The one-shot POSIX shell command that dumps everything the scan needs
    from a remote venv: the ``bin/`` listing (entry-point filter), any editable
    install marker (⇒ fall back to the CLI), and every ``runspec.toml`` under
    ``lib/python*/site-packages`` with its contents.

    Portable ``sh``: no bashisms, ``find -exec … +`` (no ``-print0``/``xargs``),
    paths with spaces survive (``$f`` is always quoted). ``lib64`` is skipped on
    purpose — on the distros that have it, it is a symlink to ``lib`` and the
    same files would be listed twice.
    """
    rp = shlex.quote(runspec_path)
    return (
        f'b=$(dirname {rp}); r=$(dirname "$b"); '
        f'echo {_SCAN_BIN}; ls -1 "$b" 2>/dev/null; '
        f"echo {_SCAN_EDITABLE}; "
        'find "$r"/lib/python*/site-packages -maxdepth 2 -name direct_url.json '
        "-exec grep -l '\"editable\": *true' {} + 2>/dev/null; "
        'find "$r"/lib/python*/site-packages -maxdepth 3 -name runspec.toml '
        f'-exec sh -c \'for f; do echo "{_SCAN_FILE}$f"; cat "$f"; done\' _ {{}} + '
        "2>/dev/null; "
        f"echo {_SCAN_END}"
    )


def _parse_scan_output(out: str) -> tuple[set[str], bool, list[tuple[str, str]]] | None:
    """Parse a scan reply into ``(bin_names, editable_present, [(path, toml_text)])``.

    ``None`` when the text is not a complete scan reply (no END marker), which
    the callers treat as "fall back to the CLI".
    """
    if _SCAN_END not in out:
        return None
    bin_names: set[str] = set()
    editable = False
    files: list[tuple[str, str]] = []
    section = ""
    cur_path = ""
    cur_lines: list[str] = []

    def _flush() -> None:
        if section == "file" and cur_path:
            files.append((cur_path, "\n".join(cur_lines)))

    for line in out.splitlines():
        if line == _SCAN_BIN:
            _flush()
            section, cur_path, cur_lines = "bin", "", []
        elif line == _SCAN_EDITABLE:
            _flush()
            section, cur_path, cur_lines = "editable", "", []
        elif line.startswith(_SCAN_FILE):
            _flush()
            section, cur_path, cur_lines = "file", line[len(_SCAN_FILE) :].strip(), []
        elif line == _SCAN_END:
            _flush()
            section, cur_path, cur_lines = "end", "", []
        elif section == "bin":
            if line.strip():
                bin_names.add(line.strip())
        elif section == "editable":
            if line.strip():
                editable = True
        elif section == "file":
            cur_lines.append(line)
    return bin_names, editable, files


def _normalise_toml_text(text: str) -> dict[str, Any] | None:
    """``runspec.loader.load_raw`` for TOML *text*: the same two normalisers the
    CLI runs per package, so the spec shape matches ``runspec local``'s JSON
    exactly. ``None`` when the file isn't a runspec.toml (or runspec's loader
    can't be imported), so the caller skips it."""
    try:
        from runspec.loader import _normalise_config, _normalise_runnables
    except ImportError:  # pragma: no cover — loader layout changed
        return None
    try:
        data = tomllib.loads(text)
        runnables_raw = {
            k: v for k, v in data.items() if k != "config" and isinstance(v, dict)
        }
        return {
            "config": _normalise_config(data.get("config", {})),
            "runnables": _normalise_runnables(runnables_raw),
        }
    except Exception:  # noqa: BLE001 — malformed / non-runspec TOML ⇒ skip
        return None


def _is_core_toml(path: str) -> bool:
    """The core package's own ``…/site-packages/runspec/runspec.toml``."""
    parts = Path(path).parts
    return len(parts) >= 3 and parts[-2] == "runspec" and parts[-3] == "site-packages"


def _runnables_from_tomls(
    files: list[tuple[str, str]],
    bin_names: set[str],
    host: str,
    group: str,
) -> list[dict[str, Any]]:
    """Turn scanned ``(path, toml_text)`` pairs into Runnable dicts, applying the
    same filters ``runspec local --format json`` does: only runnables with an
    entry point in the venv's bin dir (``name`` or ``name.exe``), deduped by
    (path, name), minus the console's own / the core control-plane runnable."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path, text in files:
        if _is_core_toml(path):
            continue
        raw = _normalise_toml_text(text)
        if not raw or not raw["runnables"]:
            continue
        config_autonomy = raw["config"].get("autonomy_default")
        for name, spec in raw["runnables"].items():
            if name in _SCAN_EXCLUDE:
                continue
            if name not in bin_names and f"{name}.exe" not in bin_names:
                continue
            key = (path, name)
            if key in seen:
                continue
            seen.add(key)
            out.extend(_spec_to_runnables(name, spec, host, group, config_autonomy))
    return out


def _scan_local(runspec_path: str, host: str) -> list[dict[str, Any]] | None:
    """In-process site-packages scan of a local venv. ``None`` ⇒ fall back to
    the CLI (no site-packages found, an editable install present, or no
    ``runspec.toml`` at all)."""
    sp = site_packages(runspec_path)
    if sp is None:
        return None
    try:
        for du in list(sp.glob("*.dist-info/direct_url.json")):
            try:
                if (
                    json.loads(du.read_text(encoding="utf-8"))
                    .get("dir_info", {})
                    .get("editable")
                ):
                    return None
            except Exception:  # noqa: BLE001 — unreadable marker ⇒ ignore it
                continue
        tomls = sorted(
            set(sp.glob("*/runspec.toml")) | set(sp.glob("*/*/runspec.toml"))
        )
    except OSError:
        return None
    if not tomls:
        return None
    files: list[tuple[str, str]] = []
    for t in tomls:
        try:
            files.append((str(t), t.read_text(encoding="utf-8")))
        except OSError:
            continue
    try:
        bin_names = {p.name for p in scripts_dir(runspec_path).iterdir()}
    except OSError:
        bin_names = set()
    return _runnables_from_tomls(files, bin_names, host, venv_name(runspec_path))


def discover_local(runspec_path: str, host: str) -> list[dict[str, Any]]:
    """Discover a local venv's runnables: site-packages scan first (see the
    module docstring), ``runspec local --format json`` as the fallback."""
    scanned = _scan_local(runspec_path, host)
    if scanned is not None:
        return scanned
    return _discover_local_cli(runspec_path, host)


def _discover_local_cli(runspec_path: str, host: str) -> list[dict[str, Any]]:
    """Shell out to the local runspec binary for discovery (the fallback)."""
    group = venv_name(runspec_path)
    try:
        result = subprocess.run(
            [runspec_path, "local", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
            **no_window_kwargs(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    runnables: list[dict[str, Any]] = []
    for item in items:
        if item["runnable"] in _SELF_EXCLUDE:
            continue
        runnables.extend(
            _spec_to_runnables(
                item["runnable"], item["spec"], host, group, item.get("config_autonomy")
            )
        )
    return runnables


def discover_remote(
    ssh_target: str,
    runspec_path: str,
    host: str,
    identity_file: str | None = None,
    global_ssh_config: dict[str, Any] | None = None,
    # Legacy kwarg kept so existing call sites don't break immediately
    ssh_binary: str = "",
    runner: Any = None,
) -> list[dict[str, Any]]:
    """Discover runnables on a remote host via paramiko SSH.

    Site-packages scan first (one ``find``+``cat`` shell command — see the
    module docstring), ``runspec local --format json`` as the fallback.

    ``runner`` is the SSH command executor — defaults to ``executor.ssh_run``
    (connect-per-call). The bridge passes its pooled runner so discovery reuses
    the per-host transport instead of opening a fresh handshake every cycle.
    """
    if runner is None:
        from .executor import ssh_run as runner

    group = venv_name(runspec_path)
    code, out, _err = runner(
        ssh_target,
        _scan_command(runspec_path),
        identity_file=identity_file,
        global_ssh_config=global_ssh_config,
    )
    parsed = _parse_scan_output(out) if code == 0 else None
    if parsed is not None:
        bin_names, editable, files = parsed
        if files and not editable:
            return _runnables_from_tomls(files, bin_names, host, group)
        logger.debug(
            "discovery %s:%s — scan %s, falling back to runspec local",
            host,
            group,
            "found an editable install" if editable else "found no runspec.toml",
        )
    else:
        logger.debug(
            "discovery %s:%s — scan reply unusable (exit %s), falling back to runspec local",
            host,
            group,
            code,
        )
    code, out, _err = runner(
        ssh_target,
        f"{runspec_path} local --format json",
        identity_file=identity_file,
        global_ssh_config=global_ssh_config,
    )
    if code != 0:
        return []
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return []
    runnables: list[dict[str, Any]] = []
    for item in items:
        runnables.extend(
            _spec_to_runnables(
                item["runnable"], item["spec"], host, group, item.get("config_autonomy")
            )
        )
    return runnables


def _parse_toml(
    toml_path: Path, host: str, group: str, bin_dir: Path
) -> list[dict[str, Any]]:
    try:
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)
    except Exception:
        return []

    config_autonomy: str | None = None
    if isinstance(raw.get("config"), dict):
        config_autonomy = raw["config"].get("autonomy")
    runnables: list[dict[str, Any]] = []
    for name, spec in raw.items():
        if name == "config" or not isinstance(spec, dict):
            continue
        # Only include runnables that have an installed entry point
        if not (bin_dir / name).exists() and not (bin_dir / f"{name}.exe").exists():
            continue
        runnables.extend(_spec_to_runnables(name, spec, host, group, config_autonomy))
    return runnables


_RAW_SPEC_EXCLUDE = frozenset({"args", "commands"})


def _spec_to_runnables(
    name: str,
    spec: dict[str, Any],
    host: str,
    group: str,
    config_autonomy: str | None = None,
    inherited_args: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    inherited_args = inherited_args or []
    own_args = _build_args(spec.get("args", {}))
    # Effective arg set = ancestor (inherited) args merged with this node's own,
    # child overriding parent on a name clash. Mirrors core
    # parser._resolve_subcommand so the form collects the same args core parses.
    own_names = {a["name"] for a in own_args}
    effective_args = [
        a for a in inherited_args if a["name"] not in own_names
    ] + own_args
    runnable: dict[str, Any] = {
        "name": name,
        "group": group,
        "host": host,
        "description": spec.get("description") or "",
        "args": effective_args,
        "autonomy": spec.get("autonomy") or config_autonomy or "confirm",
        # When true, this node must be invoked with one of its commands (core
        # enforces require-command). The CLI emits the normalised underscore key;
        # raw-TOML callers (_parse_toml/tests) carry the hyphen form — accept both.
        "requireCommand": bool(
            spec.get("require_command") or spec.get("require-command")
        ),
        # Full raw spec minus processed keys — SpecPanel uses this for the holistic view.
        "rawSpec": {k: v for k, v in spec.items() if k not in _RAW_SPEC_EXCLUDE},
    }
    if spec.get("run_as"):
        run_as = spec["run_as"]
        if isinstance(run_as, str):
            runnable["runAs"] = run_as
        elif isinstance(run_as, dict):
            runnable["runAs"] = str(run_as.get("default", ""))

    commands = spec.get("commands")
    if commands:
        # Children inherit this node's effective args, each tagged so the form can
        # group inherited fields apart from the command's own. Re-tagging an
        # already-inherited arg is idempotent.
        child_inherited = [{**a, "inherited": True} for a in effective_args]
        runnable["commands"] = {
            sub_name: _spec_to_runnables(
                sub_name, sub_spec, host, group, config_autonomy, child_inherited
            )[0]
            for sub_name, sub_spec in commands.items()
            if isinstance(sub_spec, dict)
        }

    return [runnable]


def _build_args(args_spec: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name, arg in args_spec.items():
        if not isinstance(arg, dict):
            continue
        arg_type = arg.get("type", "str")
        # load_raw normalises args with "required": None when not set in TOML,
        # so dict.get(key, fallback) won't trigger — check for None explicitly.
        required_raw = arg.get("required")
        required = (
            required_raw
            if required_raw is not None
            else (arg.get("default") is None and arg_type not in ("flag",))
        )
        entry: dict[str, Any] = {
            "name": name,
            "type": arg_type,
            "required": required,
        }
        # Optional fields — only include if present in spec
        for key in (
            "description",
            "options",
            "short",
            "deprecated",
            "ui",
            "hint",
            "env",
            "pattern",
            "delimiter",
            "meta",
        ):
            if arg.get(key) is not None:
                entry[key] = arg[key]
        if arg.get("default") is not None:
            entry["default"] = arg["default"]
        if arg.get("range") is not None:
            entry["range"] = arg["range"]
        if arg.get("multiple"):
            entry["multiple"] = True
        if arg.get("position") is not None:
            entry["position"] = arg["position"]
        if arg.get("autonomy"):
            entry["autonomy"] = arg["autonomy"]
        # String-validation lengths — the CLI JSON normalises the hyphenated TOML
        # keys to underscores, but the raw-TOML discovery path keeps the hyphen;
        # accept both and emit a stable snake_case key for the Specs inspector.
        min_length = arg.get("min_length", arg.get("min-length"))
        if min_length is not None:
            entry["min_length"] = min_length
        max_length = arg.get("max_length", arg.get("max-length"))
        if max_length is not None:
            entry["max_length"] = max_length
        result.append(entry)
    return result
