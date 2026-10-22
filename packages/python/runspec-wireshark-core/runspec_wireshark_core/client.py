"""client.py — find the Wireshark tools and run them.

Everything here shells out to Wireshark's own command-line tools (``tshark``,
``capinfos``, ``mergecap``) with an argument list, never a shell string, so a
display filter or a file name can't be read as a command. Name resolution is off
by default (``-n``): it is slow, and it would send DNS lookups for every address
in the capture.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 120.0

# Where Wireshark installs itself when its folder isn't on PATH.
_KNOWN_DIRS = {
    "win32": [r"C:\Program Files\Wireshark", r"C:\Program Files (x86)\Wireshark"],
    "darwin": ["/Applications/Wireshark.app/Contents/MacOS", "/opt/homebrew/bin", "/usr/local/bin"],
}

# Field separator / multi-value aggregator for `-T fields`. Control characters
# never appear in a field value, unlike tabs, commas or `|`.
SEP = "\x1e"
AGG = "\x1f"


class WiresharkError(Exception):
    """A Wireshark tool failed, or its output couldn't be read."""


class ToolNotFoundError(WiresharkError):
    """``tshark`` (or one of its companion tools) isn't installed."""


@dataclass
class WiresharkConfig:
    """How to run tshark.

    ``tshark`` is an explicit path to the binary (``None`` = look it up);
    ``keylog_file`` is a TLS key log (``SSLKEYLOGFILE`` format) used to decrypt
    TLS; ``decode_as`` entries are tshark ``-d`` rules such as
    ``tcp.port==8888,http``; ``resolve_names`` turns name resolution back on;
    ``timeout`` bounds every run, in seconds.
    """

    tshark: str | None = None
    keylog_file: str | None = None
    decode_as: list[str] = field(default_factory=list)
    resolve_names: bool = False
    timeout: float = DEFAULT_TIMEOUT


def config_from_env(
    *,
    tshark: str | None = None,
    keylog_file: str | None = None,
    decode_as: list[str] | None = None,
    resolve_names: bool = False,
    timeout: float | None = None,
) -> WiresharkConfig:
    """Build a :class:`WiresharkConfig`; arguments win over the environment.

    Reads ``TSHARK_PATH`` (or ``WIRESHARK_TSHARK``) for the binary,
    ``WIRESHARK_KEYLOG_FILE`` (or ``SSLKEYLOGFILE``) for a TLS key log, and
    ``WIRESHARK_TIMEOUT`` for the timeout.
    """
    env = os.environ
    raw_timeout = env.get("WIRESHARK_TIMEOUT")
    return WiresharkConfig(
        tshark=tshark or env.get("TSHARK_PATH") or env.get("WIRESHARK_TSHARK") or None,
        keylog_file=keylog_file or env.get("WIRESHARK_KEYLOG_FILE") or env.get("SSLKEYLOGFILE") or None,
        decode_as=list(decode_as or []),
        resolve_names=resolve_names,
        timeout=timeout if timeout is not None else (float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT),
    )


def _exe(name: str) -> str:
    return name + ".exe" if sys.platform == "win32" else name


def find_tool(name: str, config: WiresharkConfig | None = None) -> str:
    """Full path of a Wireshark tool (``tshark``, ``capinfos``, ``mergecap``).

    An explicit ``config.tshark`` is used as given; the companion tools are
    looked for next to it first. Then PATH, then the usual install folders.
    Raises :class:`ToolNotFoundError` when it isn't installed.
    """
    explicit = config.tshark if config else None
    if name == "tshark" and explicit:
        if os.path.isfile(explicit) or shutil.which(explicit):
            return shutil.which(explicit) or explicit
        raise ToolNotFoundError(f"tshark not found at {explicit} (from --tshark / TSHARK_PATH)")
    dirs: list[str] = []
    if explicit:
        dirs.append(os.path.dirname(os.path.abspath(explicit)))
    for folder in dirs:
        candidate = os.path.join(folder, _exe(name))
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which(name)
    if found:
        return found
    for folder in _KNOWN_DIRS.get(sys.platform, []):
        candidate = os.path.join(folder, _exe(name))
        if os.path.isfile(candidate):
            return candidate
    hint = {
        "win32": "install Wireshark (it includes tshark), or pass --tshark",
        "darwin": "install Wireshark or `brew install wireshark`, or pass --tshark",
    }.get(sys.platform, "install the tshark package (e.g. `apt install tshark` / `dnf install wireshark-cli`), or pass --tshark")
    raise ToolNotFoundError(f"{name} not found — {hint}")


def capture_file(path: str | os.PathLike[str]) -> str:
    """The capture path as a string, refusing a missing file up front (tshark's
    own message for it is a long paragraph)."""
    text = os.fspath(path)
    if not os.path.isfile(text):
        raise WiresharkError(f"capture file not found: {text}")
    return text


def read_argv(config: WiresharkConfig, file: str | os.PathLike[str], *, filter: str | None = None) -> list[str]:
    """``tshark -r FILE`` with the config's options (and ``-Y FILTER``)."""
    argv = [find_tool("tshark", config), "-r", capture_file(file)]
    if not config.resolve_names:
        argv.append("-n")
    if config.keylog_file:
        argv += ["-o", f"tls.keylog_file:{config.keylog_file}"]
    for rule in config.decode_as:
        argv += ["-d", rule]
    if filter:
        argv += ["-Y", filter]
    return argv


def fields_argv(config: WiresharkConfig, file: str | os.PathLike[str], fields: list[str], *, filter: str | None = None, occurrence: str = "a") -> list[str]:
    """``tshark -T fields`` for ``fields``, values separated by :data:`SEP` and
    repeats of one field joined by :data:`AGG`."""
    argv = read_argv(config, file, filter=filter) + ["-T", "fields", "-E", f"separator={SEP}", "-E", f"occurrence={occurrence}", "-E", f"aggregator={AGG}"]
    for name in fields:
        argv += ["-e", name]
    return argv


def tool_name(path: str) -> str:
    """``C:\\Program Files\\Wireshark\\tshark.exe`` → ``tshark`` on any OS."""
    name = re.split(r"[\\/]", path)[-1]
    return name[:-4] if name.lower().endswith(".exe") else name


def _error_text(stderr: str) -> str:
    # tshark warns about running as root on every run; that's not the error.
    lines = [line for line in stderr.strip().splitlines() if line.strip() and not line.startswith("Running as user")]
    return "\n".join(lines[-6:])


def _failure(argv: list[str], code: int, stderr: str) -> WiresharkError:
    tool = tool_name(argv[0])
    if "Invalid -z argument" in stderr:
        # tshark follows this with every statistic it knows (~200 lines).
        line = next(x for x in stderr.splitlines() if "Invalid -z argument" in x)
        return WiresharkError(line.removeprefix("tshark: ").split(";")[0] + " — see `tshark -z help` for the list")
    text = _error_text(stderr).removeprefix("tshark: ")
    # Exit 4 is tshark's "couldn't compile the display filter"; its message
    # already points a caret at the offending token.
    if tool == "tshark" and code == 4 and "-Y" in argv:
        return WiresharkError(f"bad display filter: {text}")
    return WiresharkError(f"{tool} exited {code}: {text or '(no message)'}")


def run(argv: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run a Wireshark tool and return its stdout, decoded as UTF-8 with line
    endings left as they are (a followed stream's ``\r\n`` is payload).
    Raises :class:`WiresharkError` on a non-zero exit or when it runs past
    ``timeout`` seconds."""
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise WiresharkError(f"{tool_name(argv[0])} took longer than {timeout:g}s — narrow it with --filter, or raise --timeout") from exc
    except FileNotFoundError as exc:
        raise ToolNotFoundError(f"{argv[0]} not found") from exc
    if proc.returncode != 0:
        raise _failure(argv, proc.returncode, proc.stderr.decode("utf-8", "replace"))
    return proc.stdout.decode("utf-8", "replace")


def iter_lines(argv: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> Iterator[str]:
    """Run a tool and yield its stdout one line at a time (no trailing newline),
    so a large capture is never held in memory. Raises :class:`WiresharkError`
    after the last line if the tool failed or timed out."""
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
        except FileNotFoundError as exc:
            raise ToolNotFoundError(f"{argv[0]} not found") from exc
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            proc.kill()

        timer = threading.Timer(timeout, _kill)
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                yield line.rstrip("\r\n")
            code = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if timed_out.is_set():
            raise WiresharkError(f"{tool_name(argv[0])} took longer than {timeout:g}s — narrow it with --filter, or raise --timeout")
        if code != 0:
            err.seek(0)
            raise _failure(argv, code, err.read().decode("utf-8", "replace"))


def iter_fields(
    config: WiresharkConfig,
    file: str | os.PathLike[str],
    fields: list[str],
    *,
    filter: str | None = None,
    occurrence: str = "a",
) -> Iterator[list[str]]:
    """Yield one list of field values per packet (``""`` for an absent field;
    repeats joined by :data:`AGG`)."""
    width = len(fields)
    for line in iter_lines(fields_argv(config, file, fields, filter=filter, occurrence=occurrence), timeout=config.timeout):
        values = line.split(SEP)
        if len(values) < width:
            values += [""] * (width - len(values))
        yield values[:width]


def first(value: str) -> str:
    """The first of a field's repeated values."""
    return value.split(AGG, 1)[0]


def every(value: str) -> list[str]:
    """All of a field's repeated values (``[]`` when absent)."""
    return [v for v in value.split(AGG) if v] if value else []


def to_int(value: str) -> int | None:
    value = first(value)
    try:
        return int(value, 0)
    except ValueError:
        return None


def to_float(value: str) -> float | None:
    try:
        return float(first(value))
    except ValueError:
        return None


def shorten(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters with an ellipsis (0 = no limit)."""
    return text if limit <= 0 or len(text) <= limit else text[: limit - 1] + "…"


def output_path(path: str | os.PathLike[str], *, overwrite: bool) -> str:
    """A file a write verb may create: refuses to replace an existing file
    unless ``overwrite``, and creates the parent folder."""
    text = os.fspath(path)
    if os.path.exists(text) and not overwrite:
        raise WiresharkError(f"{text} already exists — pass --overwrite to replace it")
    Path(text).resolve().parent.mkdir(parents=True, exist_ok=True)
    return text
