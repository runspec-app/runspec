"""A throwaway venv for tests that run the real daemon and the real `runspec exec`.

It holds no packages of its own: a ``.pth`` line adds the test interpreter's own
site-packages (so ``runspec``, APScheduler and this package import as usual),
plus one fake installed runnable, ``demo``, whose behaviour each schedule picks
with ``--mode``. ``sys.prefix`` inside it is the venv, so its config, lock,
heartbeat and runs index are all its own.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import site
import sqlite3
import subprocess
import sys
import sysconfig
import textwrap
import time
from pathlib import Path
from typing import Any

DEMO_TOML = """\
[config]
run_as = ""

[demo]
description = "Test runnable for the scheduler's process tests"

[demo.args]
mode    = {options = ["ok", "fail", "sleep", "hang", "bigout", "badutf8", "bgchild"], default = "ok"}
seconds = {type = "int", default = 0}
marker  = {type = "str", default = ""}
"""

# A plain script (no rs.parse): fast, and exec records it either way.
DEMO_CLI = r"""
import os, sys, time, subprocess

def _opt(name, default=""):
    argv = sys.argv[1:]
    for i, tok in enumerate(argv):
        if tok == "--" + name and i + 1 < len(argv):
            return argv[i + 1]
    return default

def main():
    mode = _opt("mode", "ok")
    seconds = int(_opt("seconds", "0") or 0)
    marker = _opt("marker")
    if marker:
        with open(marker, "a") as f:
            f.write(f"{os.getpid()} {time.time()}\n")
    if mode == "ok":
        print("ok")
    elif mode == "fail":
        print("failing", file=sys.stderr)
        sys.exit(3)
    elif mode == "sleep":
        time.sleep(seconds)
        print("slept")
    elif mode == "hang":
        # a background child in the same process group, then hang
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
        if marker:
            with open(marker + ".child", "w") as f:
                f.write(str(child.pid))
        time.sleep(3600)
    elif mode == "bigout":
        chunk = b"x" * (1 << 20)
        for _ in range(seconds or 1024):  # MiB
            sys.stdout.buffer.write(chunk)
        sys.stdout.flush()
    elif mode == "badutf8":
        sys.stdout.buffer.write(b"bad \xff\xfe\xfd utf-8\n")
    elif mode == "bgchild":
        # leaves a child holding stdout open after the runnable exits
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(%d)" % max(seconds, 1)])
        print("started background child")
"""


def _site_packages() -> list[str]:
    paths = list(site.getsitepackages()) + [sysconfig.get_path("purelib"), sysconfig.get_path("platlib")]
    out: list[str] = []
    for p in paths:
        if p and p not in out and os.path.isdir(p):
            out.append(p)
    return out


def make_venv(root: Path) -> Path:
    venv = root / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    py = venv / "bin" / "python"
    purelib = Path(subprocess.check_output([str(py), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
    purelib.mkdir(parents=True, exist_ok=True)
    lines = [f"import site; site.addsitedir({p!r})" for p in _site_packages()]
    (purelib / "zz_outer.pth").write_text("\n".join(lines) + "\n")

    # the fake runnable
    pkg = purelib / "demo_rs"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "runspec.toml").write_text(DEMO_TOML)
    (pkg / "cli.py").write_text(textwrap.dedent(DEMO_CLI))
    dist = purelib / "demo_rs-0.1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: demo-rs\nVersion: 0.1.0\nRequires-Dist: runspec\n")
    (dist / "entry_points.txt").write_text("[console_scripts]\ndemo = demo_rs.cli:main\n")
    (dist / "RECORD").write_text("demo_rs/__init__.py,,\ndemo_rs/runspec.toml,,\ndemo_rs/cli.py,,\ndemo_rs-0.1.0.dist-info/METADATA,,\ndemo_rs-0.1.0.dist-info/RECORD,,\n")

    for name, target in (("runspec", "runspec.cli"), ("runspec-scheduler", "runspec_scheduler.cli"), ("demo", "demo_rs.cli")):
        script = venv / "bin" / name
        script.write_text(f"#!{py}\nimport sys\nfrom {target} import main\nsys.exit(main())\n")
        script.chmod(0o755)
    return venv


class Proc:
    """A daemon started in the test venv."""

    def __init__(self, venv: Path, *args: str, env: dict[str, str] | None = None) -> None:
        self.venv = venv
        full_env = {k: v for k, v in os.environ.items() if not k.startswith("RUNSPEC_")}
        full_env.update(env or {})
        self.log_path = venv / f"daemon-{time.time_ns()}.out"
        self.log = open(self.log_path, "wb")  # noqa: SIM115
        self.p = subprocess.Popen(
            [str(venv / "bin" / "runspec-scheduler"), "run", "--reload-interval", "1", *args],
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            env=full_env,
            cwd=str(venv),
            start_new_session=True,
        )

    @property
    def pid(self) -> int:
        return self.p.pid

    def output(self) -> str:
        return self.log_path.read_bytes().decode("utf-8", "replace")

    def signal(self, sig: int) -> None:
        os.kill(self.p.pid, sig)

    def stop(self, timeout: float = 10) -> int | None:
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
            try:
                self.p.wait(timeout)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        if not self.log.closed:
            self.log.close()
        return self.p.returncode


def write_config(venv: Path, text: str) -> Path:
    cfg = venv / "runspec_scheduler.toml"
    tmp = cfg.with_name(f".cfg.{time.time_ns()}.tmp")
    tmp.write_text(textwrap.dedent(text))
    os.replace(tmp, cfg)
    return cfg


def runs(venv: Path, sched_id: str | None = None) -> list[dict[str, Any]]:
    db = venv / "logs" / "runspec_runs.db"
    if not db.exists():
        return []
    con = sqlite3.connect(str(db), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        query, params = ("SELECT * FROM runs WHERE label = ? ORDER BY start_ts", (f"sched:{sched_id}",)) if sched_id else ("SELECT * FROM runs ORDER BY start_ts", ())
        rows = con.execute(query, params).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    return [dict(r) for r in rows]


def finished(venv: Path, sched_id: str) -> list[dict[str, Any]]:
    return [r for r in runs(venv, sched_id) if r.get("status") != "running"]


def state(venv: Path) -> dict[str, Any] | None:
    try:
        return json.loads((venv / "logs" / "scheduler_state.json").read_text())
    except (OSError, ValueError):
        return None


def wait_for(cond: Any, timeout: float = 20, every: float = 0.2, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = cond()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last: {last!r})")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie still answers kill(0); treat it as gone
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def kill_leftovers(venv: Path) -> None:
    """SIGKILL every process still running from ``venv`` (fires a stopped daemon
    left behind), so the next test's runs index starts empty."""
    needle = str(venv).encode()
    me = os.getpid()
    for _ in range(50):
        found = False
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == me:
                continue
            try:
                cmd = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            if needle in cmd:
                found = True
                with contextlib.suppress(OSError):
                    os.kill(int(entry.name), signal.SIGKILL)
        if not found:
            return
        time.sleep(0.1)
