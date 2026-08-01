"""Performance tuning & analysis helpers — for perf-sensitive corporate
workloads (quants, devops): read/tune kernel params, inspect CPU
governor/frequency, and read the kernel's PSI pressure counters.

Reads are pure and tool-light (``/proc`` / ``/sys`` / ``sysctl -a``); tuning
(``set_sysctl``) is a privileged write, so it takes ``run_as`` / ``become_method``
and escalates via :func:`runspec_linux_core.become.build_become`. Parsers are
separated from the file/subprocess I/O so they unit-test without root or the
real files.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess

from runspec_linux_core.become import build_become, check_become_error
from runspec_linux_core.errors import CommandError, ToolNotFoundError

# A dotted sysctl key (e.g. net.core.somaxconn). Validated before it is written,
# so a crafted value can't inject extra tokens into the escalated command.
_SYSCTL_KEY_RE = re.compile(r"^[a-zA-Z0-9_.\-/]+$")

_PERSIST_FILE = "/etc/sysctl.d/99-runspec.conf"


def _parse_sysctl(text: str) -> list[dict]:
    """Parse ``sysctl -a`` output (``key = value`` lines) into ``{key, value}``."""
    entries: list[dict] = []
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        entries.append({"key": key.strip(), "value": value.strip()})
    return entries


def get_sysctl(pattern: str | None = None) -> dict:
    """Return kernel parameters (optionally filtered by a substring of the key).

    Uses ``sysctl -a`` when available, else reads ``/proc/sys`` directly. Raises
    ToolNotFoundError only if neither source is available.
    """
    if shutil.which("sysctl"):
        result = subprocess.run(["sysctl", "-a"], capture_output=True, text=True)
        entries = _parse_sysctl(result.stdout)
    elif os.path.isdir("/proc/sys"):
        entries = _read_proc_sys()
    else:
        raise ToolNotFoundError("sysctl not available and /proc/sys not present")

    if pattern:
        entries = [e for e in entries if pattern in e["key"]]
    return {"count": len(entries), "entries": entries}


def _read_proc_sys() -> list[dict]:
    """Fallback reader: walk ``/proc/sys`` reading readable leaf files as
    dotted keys. Unreadable / write-only entries are skipped."""
    entries: list[dict] = []
    for root, _dirs, files in os.walk("/proc/sys"):
        for name in files:
            path = os.path.join(root, name)
            key = os.path.relpath(path, "/proc/sys").replace("/", ".")
            try:
                with open(path, errors="replace") as f:
                    value = f.read().strip().replace("\n", " ")
            except OSError:
                continue
            entries.append({"key": key, "value": value})
    return entries


def _validate_sysctl_key(key: str) -> str:
    if not _SYSCTL_KEY_RE.match(key):
        raise ValueError(f"invalid sysctl key: {key!r}")
    return key


def set_sysctl(
    key: str,
    value: str,
    *,
    persist: bool = False,
    run_as: str | None = None,
    become_method: str = "sudo",
) -> dict:
    """Set a kernel parameter with ``sysctl -w key=value`` (a privileged write).

    With ``persist`` also appends ``key = value`` to ``/etc/sysctl.d/99-runspec.conf``
    so it survives a reboot. ``run_as`` / ``become_method`` escalate the write
    (``--sudo`` on the runnable maps to ``run_as="root"``). Raises ValueError on a
    malformed key, or CommandError if the write fails.
    """
    _validate_sysctl_key(key)

    argv = build_become(["sysctl", "-w", f"{key}={value}"], run_as, become_method)
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        check_become_error("set sysctl", result.stderr, run_as)

    persisted: str | None = None
    if persist:
        line = f"{key} = {value}\n"
        tee = subprocess.run(
            build_become(["tee", "-a", _PERSIST_FILE], run_as, become_method),
            input=line,
            capture_output=True,
            text=True,
        )
        if tee.returncode != 0:
            raise CommandError(f"applied but failed to persist to {_PERSIST_FILE}: {tee.stderr.strip()}")
        persisted = _PERSIST_FILE

    out = {"key": key, "value": value, "applied": True, "persisted": persisted}
    if run_as:
        out["run_as"] = run_as
    return out


def _read_first_line(path: str) -> str | None:
    try:
        with open(path, errors="replace") as f:
            return f.readline().strip()
    except OSError:
        return None


def cpu_info() -> dict:
    """Report CPU model, core count, and per-CPU governor / frequency (kHz).

    Reads ``/proc/cpuinfo`` and ``/sys/devices/system/cpu/cpu*/cpufreq``. A host
    without cpufreq (many VMs / containers) simply reports empty governor/freq.
    """
    model = ""
    cpu_count = 0
    try:
        with open("/proc/cpuinfo", errors="replace") as f:
            for line in f:
                if line.startswith("processor"):
                    cpu_count += 1
                elif not model and line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
    except OSError:
        pass

    cpus: list[dict] = []
    for cpu_dir in sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*"), key=_cpu_index):
        freq = os.path.join(cpu_dir, "cpufreq")
        cpus.append(
            {
                "cpu": os.path.basename(cpu_dir),
                "governor": _read_first_line(os.path.join(freq, "scaling_governor")) or "",
                "cur_khz": _read_int(os.path.join(freq, "scaling_cur_freq")),
                "min_khz": _read_int(os.path.join(freq, "scaling_min_freq")),
                "max_khz": _read_int(os.path.join(freq, "scaling_max_freq")),
            }
        )

    governors = sorted({c["governor"] for c in cpus if c["governor"]})
    return {
        "model": model,
        "cpu_count": cpu_count or len(cpus),
        "governors": governors,
        "cpus": cpus,
    }


def _cpu_index(cpu_dir: str) -> int:
    m = re.search(r"cpu(\d+)$", cpu_dir)
    return int(m.group(1)) if m else 0


def _read_int(path: str) -> int | None:
    line = _read_first_line(path)
    if line is None:
        return None
    try:
        return int(line)
    except ValueError:
        return None


def _parse_pressure(text: str) -> dict:
    """Parse a ``/proc/pressure/<res>`` file into ``{some: {...}, full: {...}}``.

    Each line is e.g. ``some avg10=0.00 avg60=0.00 avg300=0.00 total=12345``.
    """
    out: dict[str, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]  # "some" or "full"
        fields: dict[str, float] = {}
        for token in parts[1:]:
            if "=" not in token:
                continue
            k, v = token.split("=", 1)
            try:
                fields[k] = float(v)
            except ValueError:
                continue
        out[kind] = fields
    return out


def pressure() -> dict:
    """Return kernel PSI pressure-stall stats for cpu / io / memory.

    Reads ``/proc/pressure/{cpu,io,memory}``. Raises ToolNotFoundError if PSI is
    not enabled on the kernel (the directory is absent).
    """
    if not os.path.isdir("/proc/pressure"):
        raise ToolNotFoundError("/proc/pressure not present — PSI is not enabled on this kernel")

    resources: dict[str, dict] = {}
    for res in ("cpu", "io", "memory"):
        path = f"/proc/pressure/{res}"
        try:
            with open(path, errors="replace") as f:
                resources[res] = _parse_pressure(f.read())
        except OSError:
            resources[res] = {}
    return {"pressure": resources}
