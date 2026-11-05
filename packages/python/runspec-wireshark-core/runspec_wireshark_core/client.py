"""client.py — find the Wireshark tools and run them.

Everything here shells out to Wireshark's own command-line tools (``tshark``,
``capinfos``, ``mergecap``) with an argument list, never a shell string, so a
display filter or a file name can't be read as a command. Name resolution is off
by default (``-n``): it is slow, and it would send DNS lookups for every address
in the capture.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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


# ── Confined tshark (Ubuntu's AppArmor profile) ──────────────────────────────
# Ubuntu 25.10+ ships an AppArmor profile for /usr/bin/tshark that lets it open
# files only under /tmp, /var/tmp and ~/tmp (plus its own data). Reading a
# capture anywhere else fails with "You don't have permission to read the file",
# and -w / --export-objects / a TLS key log outside /tmp fail the same way. So
# when tshark is confined the capture is piped in on stdin (`-r -`), files
# tshark writes go to a private folder under /tmp first and are moved into
# place, and the key log is read from a private copy under /tmp.
_APPARMOR_PROFILE = "/etc/apparmor.d/tshark"
_APPARMOR_ENABLED = "/sys/module/apparmor/parameters/enabled"
_CONFINED_TSHARK = "/usr/bin/tshark"
_STAGE_ROOT = "/tmp"
_DENIED = "You don't have permission to read the file"
_denied_seen = False
_keylog_copies: dict[tuple[str, int, int], str] = {}


def confined(tshark: str) -> bool:
    """Whether ``tshark`` runs under a sandbox that stops it opening files
    outside /tmp: Ubuntu's AppArmor profile is installed and AppArmor is on
    (the profile attaches to ``/usr/bin/tshark``), or tshark has already been
    refused a readable file in this process."""
    if _denied_seen:
        return True
    if not sys.platform.startswith("linux") or os.path.realpath(tshark) != _CONFINED_TSHARK:
        return False
    try:
        enabled = Path(_APPARMOR_ENABLED).read_text(encoding="ascii").strip()
    except OSError:
        return False
    return enabled.upper().startswith("Y") and os.path.isfile(_APPARMOR_PROFILE)


def _note_denied() -> None:
    global _denied_seen
    _denied_seen = True


def _stage_root() -> str | None:
    return _STAGE_ROOT if os.path.isdir(_STAGE_ROOT) else None


@contextmanager
def staging(config: WiresharkConfig) -> Iterator[str | None]:
    """A private folder under /tmp for tshark to write into when it is
    :func:`confined` (removed afterwards; move what you need out of it), or
    ``None`` when tshark can write the real destination itself."""
    if not confined(find_tool("tshark", config)):
        yield None
        return
    folder = tempfile.mkdtemp(prefix="runspec-pcap-", dir=_stage_root())
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _remove_keylog_copies() -> None:
    for path in _keylog_copies.values():
        with contextlib.suppress(OSError):
            os.unlink(path)
    _keylog_copies.clear()


def _keylog_for(tshark: str, path: str) -> str:
    """The TLS key log path to hand tshark: a private copy under /tmp (owner
    only, deleted at exit) when tshark is confined and couldn't read it."""
    root = _stage_root()
    if not confined(tshark) or root is None or os.path.realpath(path).startswith(root + os.sep):
        return path
    try:
        st = os.stat(path)
        key = (os.path.realpath(path), st.st_mtime_ns, st.st_size)
        if key not in _keylog_copies:
            if not _keylog_copies:
                atexit.register(_remove_keylog_copies)
            fd, copy = tempfile.mkstemp(prefix="runspec-pcap-", suffix=".keys", dir=root)
            with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
                shutil.copyfileobj(src, out)
            _keylog_copies[key] = copy
        return _keylog_copies[key]
    except OSError:
        return path  # let tshark report it


def capture_file(path: str | os.PathLike[str]) -> str:
    """The capture path as a string, refusing a missing file up front (tshark's
    own message for it is a long paragraph)."""
    text = os.fspath(path)
    if not os.path.isfile(text):
        raise WiresharkError(f"capture file not found: {text}")
    return text


def read_argv(config: WiresharkConfig, file: str | os.PathLike[str], *, filter: str | None = None) -> list[str]:
    """``tshark -r FILE`` with the config's options (and ``-Y FILTER``)."""
    tshark = find_tool("tshark", config)
    argv = [tshark, "-r", capture_file(file)]
    if not config.resolve_names:
        argv.append("-n")
    if config.keylog_file:
        argv += ["-o", f"tls.keylog_file:{_keylog_for(tshark, config.keylog_file)}"]
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


class _Failed(Exception):
    """A tool exited non-zero (internal; turned into :class:`WiresharkError`)."""

    def __init__(self, code: int, stderr: str) -> None:
        super().__init__(code, stderr)
        self.code = code
        self.stderr = stderr


class _Feed:
    """Pipe a capture file into tshark's stdin (``tshark -r -``) from a thread,
    for a confined tshark that may not open the file itself. The pipe is ours,
    not the file's descriptor: AppArmor rechecks an inherited file descriptor
    against the profile, but not a pipe."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.read_end, self._write_end = os.pipe()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        os.close(self.read_end)  # the child holds its own copy now
        self._thread = threading.Thread(target=self._pump, name="tshark-feed", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        with open(self._write_end, "wb") as out:
            try:
                with open(self.path, "rb") as src:
                    shutil.copyfileobj(src, out, 1 << 20)
            except OSError:
                pass  # tshark stopped reading (exit, -c, timeout) or the file went away

    def close(self) -> None:
        if self._thread is None:
            os.close(self.read_end)
            os.close(self._write_end)
        else:
            self._thread.join(timeout=5)


def _timeout_error(argv: list[str], timeout: float) -> WiresharkError:
    return WiresharkError(f"{tool_name(argv[0])} took longer than {timeout:g}s — narrow it with --filter, or raise --timeout")


def _capture_input(argv: list[str]) -> str | None:
    """The file a ``tshark -r FILE`` command reads (``None`` for any other
    command, or one already reading stdin)."""
    if tool_name(argv[0]) != "tshark" or "-r" not in argv:
        return None
    i = argv.index("-r")
    path = argv[i + 1] if i + 1 < len(argv) else "-"
    return None if path == "-" else path


def _from_stdin(argv: list[str]) -> list[str]:
    i = argv.index("-r")
    return argv[: i + 1] + ["-"] + argv[i + 2 :]


def _was_denied(path: str, stderr: str) -> bool:
    """tshark couldn't open a file this process can read: a sandbox
    (AppArmor) stopped it, not the file's permissions."""
    return _DENIED in stderr and os.access(path, os.R_OK)


def _attempts(argv: list[str]) -> Iterator[tuple[list[str], str | None]]:
    """The command to try first, then (only if the first one is refused by a
    sandbox, see :func:`run`) the same command reading the file from stdin."""
    source = _capture_input(argv)
    if source is None:
        yield argv, None
        return
    if confined(argv[0]):
        yield _from_stdin(argv), source
        return
    yield argv, None
    yield _from_stdin(argv), source


def _exec(argv: list[str], timeout: float, feed: str | None) -> bytes:
    feeder = _Feed(feed) if feed else None
    try:
        proc = subprocess.Popen(
            argv,
            stdin=feeder.read_end if feeder else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        if feeder:
            feeder.close()
        raise ToolNotFoundError(f"{argv[0]} not found") from exc
    if feeder:
        feeder.start()
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise _timeout_error(argv, timeout) from exc
    finally:
        if feeder:
            feeder.close()
    if proc.returncode != 0:
        raise _Failed(proc.returncode, err.decode("utf-8", "replace"))
    return out


def _stderr_for(stderr: str, source: str | None) -> str:
    # Reading stdin, tshark calls the file "-"; name the real one.
    return stderr.replace('"-"', f'"{source}"') if source else stderr


def run(argv: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run a Wireshark tool and return its stdout, decoded as UTF-8 with line
    endings left as they are (a followed stream's ``\r\n`` is payload).
    Raises :class:`WiresharkError` on a non-zero exit or when it runs past
    ``timeout`` seconds.

    A ``tshark -r FILE`` command reads the file through a pipe when tshark is
    confined (see :func:`confined`), or when tshark is refused a file this
    process can read — then the rest of the process pipes too."""
    for attempt, feed in _attempts(argv):
        try:
            return _exec(attempt, timeout, feed).decode("utf-8", "replace")
        except _Failed as exc:
            source = _capture_input(argv)
            if feed is None and source and _was_denied(source, exc.stderr):
                _note_denied()
                continue
            raise _failure(argv, exc.code, _stderr_for(exc.stderr, feed)) from None
    raise AssertionError("unreachable")  # pragma: no cover


def _iter(argv: list[str], timeout: float, feed: str | None) -> Iterator[str]:
    feeder = _Feed(feed) if feed else None
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=err,
                stdin=feeder.read_end if feeder else subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            if feeder:
                feeder.close()
            raise ToolNotFoundError(f"{argv[0]} not found") from exc
        if feeder:
            feeder.start()
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
            if feeder:
                feeder.close()
        if timed_out.is_set():
            raise _timeout_error(argv, timeout)
        if code != 0:
            err.seek(0)
            raise _Failed(code, err.read().decode("utf-8", "replace"))


def iter_lines(argv: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> Iterator[str]:
    """Run a tool and yield its stdout one line at a time (no trailing newline),
    so a large capture is never held in memory. Raises :class:`WiresharkError`
    after the last line if the tool failed or timed out. Reads a capture
    through a pipe in the same cases as :func:`run`."""
    for attempt, feed in _attempts(argv):
        yielded = False
        try:
            for line in _iter(attempt, timeout, feed):
                yielded = True
                yield line
            return
        except _Failed as exc:
            source = _capture_input(argv)
            if feed is None and not yielded and source and _was_denied(source, exc.stderr):
                _note_denied()
                continue
            raise _failure(argv, exc.code, _stderr_for(exc.stderr, feed)) from None


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
