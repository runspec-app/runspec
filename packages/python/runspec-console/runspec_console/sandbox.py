"""
sandbox.py — the WASM/WASI capability sandbox behind the
``run_python_sandboxed`` / ``run_sql_sandboxed`` agent tools.

Untrusted Python runs inside a WebAssembly (WASI) CPython interpreter under
`wasmtime`. Unlike the host interpreter, the guest has **no ambient authority**:
the host grants exactly one preopened directory — the current chat session's
workspace — and nothing else. The consequences are enforced by the runtime, not
by policy that code could talk its way around:

  * **No network** — WASI preview1 exposes no sockets, so `socket`/`urllib`/… have
    nothing to open.
  * **No host filesystem** beyond the single preopened workspace (mapped to the
    guest as ``/ws``); ``open("/etc/passwd")`` fails with *no such file*.
  * **No subprocess / host process access.**
  * **No host environment or secrets.**
  * **Bounded wall-clock** (a killable worker subprocess, so even a guest blocked
    in a host syscall is stopped at the deadline) and **memory** (`Store` limits),
    and a fresh, disposable instance per call.

This is a *real* security boundary for agent code execution — which is why the
tools can honestly carry the word **sandboxed**. The confirm gate stays on top as
operator governance; it is no longer the *only* control.

Heavy deps are optional and lazy: `wasmtime` (the runtime) ships in the
``[sandbox]`` extra, and the ~20 MB WASI CPython ``.wasm`` is resolved at runtime
(bundled path / env / config / app-data cache, downloaded on first use with a
SHA-256 check). :func:`available` reports readiness so a console without the extra
simply doesn't offer the tools — **fail-closed, never a host-execution fallback**
(that would defeat the whole point). Mirrors ``observability.py``.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import urllib.request
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from . import config as config_mod
from .bridge_const import ToolCancelled
from .workspaces import jail

# The pinned WASI CPython build the sandbox runs (VMware Wasm Labs). Stdlib is
# embedded in the module (incl. `_sqlite3`), so no separate stdlib zip is needed.
_WASM_VERSION = "3.11.4"
_WASM_URL = (
    "https://github.com/vmware-labs/webassembly-language-runtimes/releases/"
    "download/python%2F3.11.4%2B20230714-11be424/python-3.11.4.wasm"
)
_WASM_SHA256 = "422a1088378ef31788384afcdf705594d833e10c7d6336c6988e2875a269f68b"
_WASM_FILENAME = f"python-{_WASM_VERSION}.wasm"

# Read cap on a single stream (bytes). Anything larger is truncated here; the
# caller's spill layer then caps again for the model context.
_MAX_STREAM_BYTES = 4 * 1024 * 1024
# How often the parent's poll loop wakes to check the deadline / cancel flag.
_POLL_INTERVAL_S = 0.05
# A generous default wasmtime fuel (instruction) budget for a run — the real
# runaway guard is the killable-subprocess wall-clock timeout, so this is only a
# belt-and-suspenders cap on a tight CPU loop. Overridable via [sandbox] fuel.
_DEFAULT_FUEL = 1_000_000_000_000

# Compiling the ~20 MB WASI CPython module takes ~3 s but *serializing* the
# compiled artefact and *deserializing* it are ~0.04 s each. So we compile once in
# the parent and cache the serialized module to disk (`.cwasm`); each run's
# killable worker subprocess deserializes it — fast startup AND a hard wall-clock
# guarantee. Guarded by a lock (turns can overlap). Serialized modules are tied to
# the wasmtime version, so the cache filename carries it → an upgrade recompiles.
_CWASM_LOCK = threading.Lock()


class SandboxUnavailable(RuntimeError):
    """Raised when a sandboxed call is attempted but the runtime or the WASI
    CPython binary can't be made ready — surfaced to the agent as a tool error
    with an actionable hint (install the extra / place the binary)."""


# ── availability + binary resolution ─────────────────────────────────────────


def _wasmtime() -> Any | None:
    """The `wasmtime` module, or ``None`` when the ``[sandbox]`` extra is absent."""
    try:
        import wasmtime  # noqa: PLC0415 — lazy: only when the sandbox is used
    except Exception:  # noqa: BLE001 — any import failure = feature absent
        return None
    return wasmtime


def _cache_dir() -> Path:
    return config_mod.app_dir() / "sandbox"


def wasm_path() -> Path | None:
    """The resolved WASI CPython binary, or ``None`` if not present yet.

    Resolution order: ``[sandbox] wasm_path`` → ``$RUNSPEC_SANDBOX_WASM`` →
    app-data cache (``{app_dir}/sandbox/python-<ver>.wasm``). No download here —
    this only reports what already exists (see :func:`ensure_wasm`)."""
    import os

    cfg = config_mod.read_sandbox()
    candidates = [
        str(cfg.get("wasm_path") or "").strip(),
        os.environ.get("RUNSPEC_SANDBOX_WASM", "").strip(),
        str(_cache_dir() / _WASM_FILENAME),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def available() -> bool:
    """True when the sandbox can run: the `wasmtime` runtime imports **and** a WASI
    CPython binary is present, or download-on-first-use is enabled so :func:`run`
    can fetch one. Fail-closed everywhere else."""
    if _wasmtime() is None:
        return False
    if wasm_path() is not None:
        return True
    return bool(config_mod.read_sandbox().get("auto_download", True))


def unavailable_reason() -> str:
    """A short, actionable reason `available()` is false — for the tool error and
    the console-status report."""
    if _wasmtime() is None:
        return (
            "the WASM sandbox runtime is not installed — "
            "`pip install runspec-console[sandbox]`"
        )
    if wasm_path() is None and not config_mod.read_sandbox().get("auto_download", True):
        return (
            "the WASI Python binary is not present and auto-download is off — "
            "set [sandbox] wasm_path or RUNSPEC_SANDBOX_WASM to a python.wasm"
        )
    return ""


def ensure_wasm() -> Path:
    """Return the WASI CPython binary, downloading it to the app-data cache on
    first use (SHA-256 verified) when absent and ``auto_download`` is on. Raises
    :class:`SandboxUnavailable` if it can't be produced."""
    existing = wasm_path()
    if existing is not None:
        return existing
    cfg = config_mod.read_sandbox()
    if not cfg.get("auto_download", True):
        raise SandboxUnavailable(unavailable_reason())
    url = str(cfg.get("url") or _WASM_URL)
    want = str(cfg.get("sha256") or _WASM_SHA256).lower()
    dest = _cache_dir()
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / _WASM_FILENAME
    tmp = dest / (_WASM_FILENAME + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 — pinned URL
            data = resp.read()
    except Exception as e:  # noqa: BLE001
        raise SandboxUnavailable(
            f"could not download the WASI Python binary from {url}: {e}"
        ) from e
    got = sha256(data).hexdigest()
    if want and got != want:
        raise SandboxUnavailable(
            f"WASI Python binary checksum mismatch (got {got}, want {want})"
        )
    tmp.write_bytes(data)
    tmp.replace(target)
    return target


# ── execution ────────────────────────────────────────────────────────────────

# A guest bootstrap: chdir into the preopened workspace so the user's code runs
# with the workspace as cwd (relative `open("tool-results/x.json")` works, matching
# the host tool's old contract), then run the real target as ``__main__``.
_BOOT_TEMPLATE = (
    "import os, runpy\n"
    "os.chdir('/ws')\n"
    "runpy.run_path({target!r}, run_name='__main__')\n"
)


def _wasmtime_version() -> str:
    try:
        from importlib.metadata import version

        return version("wasmtime")
    except Exception:  # noqa: BLE001
        return "unknown"


def ensure_cwasm() -> Path:
    """Return the path to the serialized (pre-compiled) WASI CPython module,
    compiling + serializing the ``.wasm`` once and caching it in app-data. Keyed by
    the wasmtime version (a serialized module is version-specific), so an upgrade
    recompiles. The worker subprocess deserializes this in ~0.04 s."""
    binary = ensure_wasm()
    w = _wasmtime()
    if w is None:  # pragma: no cover — guarded by available() upstream
        raise SandboxUnavailable(unavailable_reason())
    cwasm = _cache_dir() / f"python-{_WASM_VERSION}-wt{_wasmtime_version()}.cwasm"
    with _CWASM_LOCK:
        try:
            fresh = cwasm.is_file() and cwasm.stat().st_mtime >= binary.stat().st_mtime
        except OSError:
            fresh = False
        if fresh:
            return cwasm
        cfg = w.Config()
        cfg.consume_fuel = True  # MUST match _sandbox_worker's engine config
        engine = w.Engine(cfg)
        module = w.Module.from_file(engine, str(binary))
        cwasm.parent.mkdir(parents=True, exist_ok=True)
        tmp = cwasm.with_suffix(".cwasm.part")
        tmp.write_bytes(module.serialize())
        tmp.replace(cwasm)
        return cwasm


def _worker_argv() -> list[str]:
    import sys

    worker = str(Path(__file__).with_name("_sandbox_worker.py"))
    return [sys.executable, worker]


def _run_module(
    *,
    root: Path,
    exec_dir: Path,
    target_guest: str,
    timeout_s: int,
    mem_bytes: int,
    fuel: int | None,
    cancel: Callable[[], bool] | None,
) -> dict[str, Any]:
    """Run ``target_guest`` in a **killable worker subprocess** with ``root``
    preopened at ``/ws`` and the private ``exec_dir`` at ``/exec`` (holding the
    bootstrap + inline source), and return ``{stdout, stderr, exit_code,
    timed_out}``.

    The subprocess — not an in-process wasmtime call — is what makes the wall-clock
    timeout and the operator's Cancel *hard*: a guest blocked in a host syscall
    (`time.sleep`, a blocking read) is unkillable in-process, but the child is
    always killable. A fired cancel raises :class:`ToolCancelled` (the tracked-tool
    wrapper turns it into "⏹ stopped")."""
    import subprocess

    cwasm = ensure_cwasm()
    out_file = exec_dir / "stdout"
    err_file = exec_dir / "stderr"
    (exec_dir / "boot.py").write_text(
        _BOOT_TEMPLATE.format(target=target_guest), encoding="utf-8"
    )
    job = json.dumps(
        {
            "cwasm": str(cwasm),
            "root": str(root),
            "exec_dir": str(exec_dir),
            "argv": ["python", "/exec/boot.py"],
            "stdout": str(out_file),
            "stderr": str(err_file),
            "mem_bytes": int(mem_bytes),
            "fuel": int(fuel) if fuel else _DEFAULT_FUEL,
        }
    )

    proc = subprocess.Popen(
        _worker_argv(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Drain the worker's own stdout (result JSON) / stderr on background threads so
    # a filled pipe can't deadlock the poll loop (mirrors the old subprocess path).
    wout: list[str] = []
    werr: list[str] = []

    def _drain(stream: Any, box: list[str]) -> None:
        try:
            box.append(stream.read())
        except (ValueError, OSError):
            box.append("")

    t_out = threading.Thread(target=_drain, args=(proc.stdout, wout), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, werr), daemon=True)
    t_out.start()
    t_err.start()
    if proc.stdin is not None:
        try:
            proc.stdin.write(job)
            proc.stdin.close()
        except OSError:
            pass

    deadline = time.monotonic() + max(1, int(timeout_s))
    timed_out = False
    cancelled = False
    while proc.poll() is None:
        if cancel is not None and cancel():
            cancelled = True
            proc.kill()
            break
        if time.monotonic() >= deadline:
            timed_out = True
            proc.kill()
            break
        time.sleep(_POLL_INTERVAL_S)
    proc.wait()
    t_out.join(timeout=1.0)
    t_err.join(timeout=1.0)

    if cancelled:
        raise ToolCancelled()

    stdout = _read_capped(out_file)
    stderr = _read_capped(err_file)
    exit_code = 1
    if timed_out:
        stderr += f"\n[killed: exceeded {int(timeout_s)}s timeout]"
    else:
        try:
            res = json.loads((wout[0] if wout else "").strip() or "{}")
            exit_code = int(res.get("exit_code", 1))
            trap = str(res.get("trap") or "")
            if trap:
                stderr += f"\n[sandbox trap: {trap}]"
        except (ValueError, TypeError):
            # Worker crashed before emitting a result — surface its stderr.
            worker_err = (werr[0] if werr else "").strip()
            stderr += f"\n[sandbox worker failed: {worker_err[:300]}]"
    return {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "timed_out": timed_out,
    }


def _read_capped(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > _MAX_STREAM_BYTES:
        data = data[:_MAX_STREAM_BYTES]
        suffix = b"\n[output truncated by sandbox]"
    else:
        suffix = b""
    return (data + suffix).decode("utf-8", errors="replace")


def run_python(
    root: Path,
    *,
    code: str | list | None = None,
    file_rel: str | None = None,
    timeout_s: int,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run a Python snippet (``code`` — a string or a list of lines) or a
    workspace-relative ``.py`` file (``file_rel``) inside the WASM sandbox with the
    session workspace (``root``) as the only visible directory. Returns
    ``{stdout, stderr, exit_code, timed_out}``. Exactly one of ``code`` /
    ``file_rel`` is required."""
    from . import codeexec

    code = codeexec.normalize_code(code)  # str | list-of-lines → str | None
    if bool(code) == bool(file_rel):
        raise ValueError(
            "run_python: provide exactly one of 'code' (the Python source as a "
            "string) or 'file' (a workspace-relative .py path) — got neither or both"
        )
    cfg = config_mod.read_sandbox()
    with tempfile.TemporaryDirectory(prefix="rs-sandbox-") as td:
        exec_dir = Path(td)
        if file_rel:
            target = jail(root, file_rel)  # raises ValueError on escape
            if not target.is_file():
                raise FileNotFoundError(f"no such workspace file: {file_rel}")
            target_guest = "/ws/" + str(file_rel).replace("\\", "/").lstrip("/")
        else:
            (exec_dir / "main.py").write_text(code or "", encoding="utf-8")
            target_guest = "/exec/main.py"
        return _run_module(
            root=root,
            exec_dir=exec_dir,
            target_guest=target_guest,
            timeout_s=timeout_s,
            mem_bytes=int(cfg["mem_mb"]) * 1024 * 1024,
            fuel=cfg.get("fuel"),
            cancel=cancel,
        )


# A guest program that runs one SQL statement against a workspace SQLite file and
# prints the bounded result as JSON on stdout — so `run_sql_sandboxed` reuses the
# same WASM engine (sqlite3 is embedded in the WASI CPython build). Read-only is
# enforced in-guest by the `mode=ro` URI, not just by the host classifier.
_SQL_TEMPLATE = """\
import json, sqlite3
db, query, read_only, max_rows = {db!r}, {query!r}, {read_only!r}, {max_rows!r}
conn = sqlite3.connect("file:" + db + ("?mode=ro" if read_only else ""), uri=True)
try:
    cur = conn.execute(query)
    columns = [d[0] for d in cur.description] if cur.description else []
    rows, truncated = [], False
    if columns:
        for row in cur:
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append(list(row))
    if not read_only:
        conn.commit()
    print(json.dumps({{
        "columns": columns, "rows": rows, "rowcount": cur.rowcount,
        "returned": len(rows), "truncated": truncated,
    }}))
finally:
    conn.close()
"""


def run_sql(
    root: Path,
    db_rel: str,
    query: str,
    *,
    read_only: bool,
    max_rows: int,
    timeout_s: int,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run one SQL statement against the workspace SQLite file ``db_rel`` inside
    the sandbox and return the parsed ``{columns, rows, rowcount, returned,
    truncated}`` result. ``read_only`` opens the db ``mode=ro`` in-guest so a read
    cannot mutate even if the classifier was fooled."""
    db_path = jail(root, db_rel)  # raises ValueError on escape
    if not db_path.is_file():
        raise FileNotFoundError(f"no such workspace database: {db_rel}")
    db_guest = "/ws/" + str(db_rel).replace("\\", "/").lstrip("/")
    program = _SQL_TEMPLATE.format(
        db=db_guest, query=query, read_only=bool(read_only), max_rows=int(max_rows)
    )
    res = run_python(root, code=program, timeout_s=timeout_s, cancel=cancel)
    if res.get("exit_code") not in (0, None):
        raise RuntimeError((res.get("stderr") or "sql failed").strip())
    out = (res.get("stdout") or "").strip()
    try:
        return json.loads(out.splitlines()[-1]) if out else {}
    except (ValueError, IndexError) as e:
        raise RuntimeError(f"sandbox SQL produced no parseable result: {e}") from e
