"""
mock.py — mock playback: answer a runnable's call from a recorded response.

With ``RUNSPEC_MOCK=1`` (or ``strict``) in the environment, ``parse()`` runs the
whole argument pipeline as usual and then, instead of returning to the
runnable's code, writes a recorded response and exits with its exit code. The
runnable's own code never runs. See SPEC → "Mock Playback".

Mock files are ``<runnable>.json``, read from (highest priority first)
``$RUNSPEC_MOCK_DIR``, the venv overlay ``{sys.prefix}/runspec_mocks/`` and the
package's ``mocks/`` folder next to ``runspec.toml``.

Recording (``runspec mock record``) runs the real runnable with
``RUNSPEC_MOCK_RECORD=<file>``: its own ``parse()`` writes the call's key
(subcommand path + command-line args) to that file, so the key a recording is
saved under is computed by exactly the code that matches it on playback.

Stdlib only.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MOCK_ENV = "RUNSPEC_MOCK"
MOCK_DIR_ENV = "RUNSPEC_MOCK_DIR"
MOCK_RECORD_ENV = "RUNSPEC_MOCK_RECORD"
FORMAT_VERSION = 1
EXIT_NO_MOCK = 69
REDACTED = "«redacted»"
OVERLAY_DIRNAME = "runspec_mocks"
PACKAGE_DIRNAME = "mocks"

# Provenance sources that count as "the caller chose this value".
_CALL_SOURCES = ("cli", "env", "runspec_env")
# Auto-injected args that are never part of a key.
_AUTO_ARGS = {"debug"}
# Entry size above which `check` warns (the response is meant for an agent).
SIZE_WARN_BYTES = 256 * 1024

# Values that look like credentials — `check` refuses them in a response.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9+/=_\-.]{20,}"),
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b\"?\s*[:=]\s*\"?[^\s\"',}]{8,}"),
]


class MockError(Exception):
    """A mock file is unreadable / malformed, or a record/play step failed."""


# ── mode ──────────────────────────────────────────────────────────────────────


def mock_mode(environ: Mapping[str, str] | None = None) -> str:
    """``""`` (off), ``"best"`` or ``"strict"`` from ``RUNSPEC_MOCK``."""
    raw = (environ if environ is not None else os.environ).get(MOCK_ENV, "").strip().lower()
    if raw == "strict":
        return "strict"
    if raw in ("1", "true", "yes", "on"):
        return "best"
    return ""


# ── files ─────────────────────────────────────────────────────────────────────


def overlay_dir() -> Path:
    """The venv overlay: mocks collected on this machine, never in a package."""
    return Path(sys.prefix) / OVERLAY_DIRNAME


def package_dir(config_path: str | Path) -> Path:
    """The package's ``mocks/`` folder, next to its ``runspec.toml``."""
    return Path(config_path).parent / PACKAGE_DIRNAME


def mock_paths(runnable: str, config_path: str | Path) -> list[tuple[str, Path]]:
    """``[(layer, path)]`` for every place a mock file may live, highest
    priority first. Layers: ``dir`` ($RUNSPEC_MOCK_DIR), ``venv``, ``package``."""
    name = f"{runnable}.json"
    paths: list[tuple[str, Path]] = []
    env_dir = os.environ.get(MOCK_DIR_ENV, "").strip()
    if env_dir:
        paths.append(("dir", Path(env_dir) / name))
    paths.append(("venv", overlay_dir() / name))
    paths.append(("package", package_dir(config_path) / name))
    return paths


def load_file(path: str | Path) -> dict[str, Any]:
    """Read and shape-check one mock file. Entries get their defaults filled."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MockError(f"cannot read mock file {path}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise MockError(f"{path}: not a mock file (expected an object with an 'entries' list)")
    version = data.get("runspec_mock")
    if version != FORMAT_VERSION:
        raise MockError(f"{path}: unsupported runspec_mock version {version!r} (expected {FORMAT_VERSION})")
    entries: list[dict[str, Any]] = []
    for i, entry in enumerate(data["entries"], start=1):
        if not isinstance(entry, dict):
            raise MockError(f"{path}: entry {i} is not an object")
        command = entry.get("command", [])
        args = entry.get("args", {})
        if not isinstance(command, list) or not all(isinstance(c, str) for c in command):
            raise MockError(f"{path}: entry {i}: 'command' must be a list of strings")
        if not isinstance(args, dict):
            raise MockError(f"{path}: entry {i}: 'args' must be an object")
        exit_code = entry.get("exit_code", 0)
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise MockError(f"{path}: entry {i}: 'exit_code' must be an integer")
        entries.append({**entry, "command": command, "args": args, "exit_code": exit_code})
    return {**data, "entries": entries}


@dataclass
class Candidate:
    """One entry, with where it came from (for ranking and reporting)."""

    entry: dict[str, Any]
    layer: str
    path: Path
    index: int  # 0-based position in its file


def load_candidates(runnable: str, config_path: str | Path) -> list[Candidate]:
    """Every entry for ``runnable`` across all layers, highest priority first.
    A malformed file raises :class:`MockError` — mock mode must not quietly
    answer from a partial set."""
    out: list[Candidate] = []
    for layer, path in mock_paths(runnable, config_path):
        if not path.is_file():
            continue
        for index, entry in enumerate(load_file(path)["entries"]):
            out.append(Candidate(entry, layer, path, index))
    return out


# ── keys and matching ─────────────────────────────────────────────────────────


def norm_name(name: str) -> str:
    return name.replace("-", "_")


def norm_value(value: Any) -> Any:
    """Comparison form: booleans ``true``/``false``, scalars their string form,
    lists element by element."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return [norm_value(v) for v in value]
    if value is None:
        return None
    return str(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)  # Path and custom types


def key_args(runspec_obj: Any, sources: Iterable[str] = _CALL_SOURCES) -> dict[str, Any]:
    """A parsed ``RunSpec``'s args whose value came from one of ``sources``,
    minus ``password`` args and auto-injected flags, as JSON values keyed by the
    arg's declared name."""
    wanted = set(sources)
    out: dict[str, Any] = {}
    for name, arg in object.__getattribute__(runspec_obj, "_args").items():
        if norm_name(name) in _AUTO_ARGS or arg.type == "password":
            continue
        if arg.value is None or arg.source not in wanted:
            continue
        out[name] = _jsonable(arg.value)
    return out


def _normalised(args: Mapping[str, Any]) -> dict[str, Any]:
    return {norm_name(k): norm_value(v) for k, v in args.items()}


def entry_matches(entry_args: Mapping[str, Any], call_args: Mapping[str, Any], *, strict: bool = False) -> bool:
    want = _normalised(entry_args)
    have = _normalised(call_args)
    if strict:
        return want == have
    return all(k in have and have[k] == v for k, v in want.items())


def find_match(candidates: list[Candidate], command: list[str], call_args: Mapping[str, Any], *, strict: bool = False) -> Candidate | None:
    """The entry that answers this call: same command path; every entry arg
    present with an equal value (``strict``: exactly the same args); the most
    specific wins, ties to the earlier candidate (higher layer, then file order)."""
    best: Candidate | None = None
    for cand in candidates:
        if cand.entry["command"] != list(command):
            continue
        if not entry_matches(cand.entry["args"], call_args, strict=strict):
            continue
        if best is None or len(cand.entry["args"]) > len(best.entry["args"]):
            best = cand
    return best


def recorded_args(candidates: list[Candidate], command: list[str]) -> list[dict[str, Any]]:
    return [c.entry["args"] for c in candidates if c.entry["command"] == list(command)]


# ── playback ──────────────────────────────────────────────────────────────────


def render_stdout(stdout: Any) -> str:
    if stdout is None:
        return ""
    if isinstance(stdout, str):
        return stdout
    return json.dumps(stdout, indent=2, ensure_ascii=False) + "\n"


def no_match_payload(runnable: str, command: list[str], call_args: Mapping[str, Any], candidates: list[Candidate]) -> dict[str, Any]:
    return {
        "error": "no mock response recorded for these arguments",
        "kind": "MockNotFound",
        "runnable": runnable,
        "command": list(command),
        "args": dict(call_args),
        "recorded": recorded_args(candidates, command),
    }


def playback(runnable: str, command: list[str], call_args: Mapping[str, Any], config_path: str | Path, *, strict: bool = False) -> int:
    """Write the matching response (or the no-match payload) and return the
    exit code to exit with. A malformed mock file is reported on stdout as a
    ``MockError`` and exits ``EXIT_NO_MOCK`` too — never a real run."""
    try:
        candidates = load_candidates(runnable, config_path)
    except MockError as e:
        sys.stdout.write(json.dumps({"error": str(e), "kind": "MockError", "runnable": runnable}, indent=2) + "\n")
        sys.stdout.flush()
        return EXIT_NO_MOCK
    match = find_match(candidates, command, call_args, strict=strict)
    if match is None:
        payload = no_match_payload(runnable, command, call_args, candidates)
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        return EXIT_NO_MOCK
    entry = match.entry
    # A person at a terminal is told the answer is canned; pipes, agents and
    # log processors get exactly the recorded output.
    with contextlib.suppress(Exception):
        if sys.stderr.isatty():
            sys.stderr.write(f"runspec: mock response ({MOCK_ENV} is set) — {runnable} did not run\n")
    text = render_stdout(entry.get("stdout"))
    if text:
        sys.stdout.write(text)
        sys.stdout.flush()
    err = entry.get("stderr")
    if isinstance(err, str) and err:
        sys.stderr.write(err)
        sys.stderr.flush()
    return int(entry["exit_code"])


def write_record_key(path: str, command: list[str], args: Mapping[str, Any]) -> None:
    """The child half of ``runspec mock record``: save this call's key."""
    with contextlib.suppress(OSError):
        Path(path).write_text(json.dumps({"command": list(command), "args": dict(args)}), encoding="utf-8")


# ── recording ─────────────────────────────────────────────────────────────────


def _in_site_packages(path: Path) -> bool:
    return any(part in ("site-packages", "dist-packages") for part in path.resolve().parts)


def resolve_destination(runnable: str, config_path: str | Path, to: str | None = None) -> Path:
    """Where ``record`` saves: ``package`` (the spec's ``mocks/``), ``venv``
    (the overlay) or a directory. Default: the package when it's a source
    checkout (not under site-packages and writable), else the venv overlay."""
    name = f"{runnable}.json"
    pkg = package_dir(config_path)
    if to == "package":
        return pkg / name
    if to == "venv":
        return overlay_dir() / name
    if to:
        return Path(to).expanduser() / name
    spec_dir = Path(config_path).parent
    if not _in_site_packages(spec_dir) and os.access(spec_dir, os.W_OK):
        return pkg / name
    return overlay_dir() / name


def redact_text(text: str, patterns: Iterable[str]) -> str:
    for pattern in patterns:
        text = re.sub(pattern, REDACTED, text)
    return text


def stdout_value(text: str) -> Any:
    """Store JSON output as JSON (readable diffs); anything else as a string."""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    return text


def save_entry(path: Path, runnable: str, entry: dict[str, Any]) -> str:
    """Add ``entry`` to the file at ``path`` (created if missing), replacing an
    entry with the same command and exactly the same args. Returns ``"added"``
    or ``"replaced"``."""
    data: dict[str, Any] = load_file(path) if path.is_file() else {"runspec_mock": FORMAT_VERSION, "runnable": runnable, "entries": []}
    entries: list[dict[str, Any]] = data["entries"]
    outcome = "added"
    for i, existing in enumerate(entries):
        if existing["command"] == entry["command"] and _normalised(existing["args"]) == _normalised(entry["args"]):
            entries[i] = entry
            outcome = "replaced"
            break
    else:
        entries.append(entry)
    write_file(path, data)
    return outcome


def write_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def run_for_record(command: list[str], argv: list[str], *, timeout: float | None = None) -> tuple[int, str, str, dict[str, Any] | None]:
    """Run the real runnable as an agent would, capturing its output and the
    key its own ``parse()`` computed. ``key`` is ``None`` when the run never
    got past argument parsing."""
    fd, key_file = tempfile.mkstemp(prefix="runspec-mock-key-", suffix=".json")
    os.close(fd)
    os.unlink(key_file)  # the child creates it only once parse() succeeds
    env = {k: v for k, v in os.environ.items() if k not in (MOCK_ENV,)}
    env.update({"RUNSPEC_AGENT": "1", MOCK_RECORD_ENV: key_file, "RUNSPEC_TRIGGER": env.get("RUNSPEC_TRIGGER") or "mock_record"})
    try:
        proc = subprocess.run([*command, *argv], env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as e:
        raise MockError(f"the runnable did not finish within {timeout}s") from e
    except OSError as e:
        raise MockError(f"cannot run {command[0]}: {e}") from e
    key: dict[str, Any] | None = None
    try:
        key = json.loads(Path(key_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        key = None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(key_file)
    return proc.returncode, proc.stdout, proc.stderr, key


def build_entry(key: Mapping[str, Any], exit_code: int, stdout: str, stderr: str, *, redact: Iterable[str] = (), note: str = "") -> dict[str, Any]:
    patterns = list(redact)
    out = redact_text(stdout, patterns)
    err = redact_text(stderr, patterns)
    entry: dict[str, Any] = {
        "command": list(key.get("command", [])),
        "args": dict(key.get("args", {})),
        "exit_code": exit_code,
        "stdout": stdout_value(out),
    }
    if err.strip():
        entry["stderr"] = err
    entry["recorded"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry["source"] = "record"
    if note:
        entry["note"] = note
    return entry


# ── check ─────────────────────────────────────────────────────────────────────


def secret_hits(text: str) -> list[str]:
    return [m.group(0)[:24] + "…" for p in _SECRET_PATTERNS for m in p.finditer(text)]


def check_entries(runnable: str, config_path: str | Path, candidates: list[Candidate]) -> list[str]:
    """Problems with recorded entries against the current spec: an unknown
    subcommand or arg, a value the arg rejects, a ``password`` arg in a key,
    a credential-looking response, or an oversized response."""
    from runspec.inference import infer_script
    from runspec.loader import load_raw
    from runspec.parser import _resolve_subcommand
    from runspec.types import coerce

    raw = load_raw(Path(config_path))
    if runnable not in raw["runnables"]:
        return [f"{runnable}: not defined in {config_path}"]
    script = infer_script(raw["runnables"][runnable], raw["config"]["autonomy_default"])
    problems: list[str] = []
    for cand in candidates:
        entry = cand.entry
        where = f"{cand.path} entry {cand.index + 1} ({' '.join([runnable, *entry['command']])})"
        leaf, command_path, rest, _globals, leftover = _resolve_subcommand(script, list(entry["command"]))
        if command_path != entry["command"] or rest or leftover:
            problems.append(f"{where}: no such subcommand")
            continue
        if leaf.get("commands") and leaf.get("require_command"):
            problems.append(f"{where}: needs a subcommand")
        specs = {norm_name(k): v for k, v in leaf["args"].items()}
        for name, value in entry["args"].items():
            spec = specs.get(norm_name(name))
            if spec is None:
                problems.append(f"{where}: unknown arg '{name}'")
                continue
            if spec.get("type") == "password":
                problems.append(f"{where}: keys on password arg '{name}' — secrets must never be recorded")
                continue
            try:
                coerce(value, spec)
            except (ValueError, TypeError) as e:
                problems.append(f"{where}: {e}")
        text = render_stdout(entry.get("stdout")) + str(entry.get("stderr") or "")
        hits = secret_hits(text)
        if hits:
            problems.append(f"{where}: response looks like it contains a credential ({', '.join(hits[:3])})")
        if len(text.encode("utf-8")) > SIZE_WARN_BYTES:
            problems.append(f"{where}: response is {len(text.encode('utf-8')) // 1024} KB (over {SIZE_WARN_BYTES // 1024} KB) — record a smaller call")
    return problems
