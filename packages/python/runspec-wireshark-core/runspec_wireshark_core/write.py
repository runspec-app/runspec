"""write.py — the verbs that create files: a live capture, a filtered copy, a
merge, and the files carried in a capture (HTTP downloads, SMB, …).

None of them changes or deletes an existing file: an output path that already
exists is refused unless ``overwrite``.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

from runspec_wireshark_core.client import WiresharkConfig, WiresharkError, capture_file, find_tool, output_path, read_argv, run
from runspec_wireshark_core.info import file_info

MAX_DURATION = 3600
EXPORT_PROTOCOLS = ("http", "smb", "tftp", "imf", "dicom", "ftp-data")


def default_capture_path(directory: str | None = None) -> str:
    """``<dir>/capture-YYYYmmdd-HHMMSS.pcapng`` — ``WIRESHARK_CAPTURE_DIR`` or
    the temp folder when no ``directory``."""
    folder = directory or os.environ.get("WIRESHARK_CAPTURE_DIR") or tempfile.gettempdir()
    return os.path.join(folder, time.strftime("capture-%Y%m%d-%H%M%S.pcapng"))


def capture(
    config: WiresharkConfig,
    interface: str,
    *,
    duration: int = 10,
    count: int | None = None,
    capture_filter: str | None = None,
    output: str | None = None,
    directory: str | None = None,
    max_mb: int = 100,
    snaplen: int | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Capture live traffic on ``interface`` into a pcapng file, stopping after
    ``duration`` seconds, ``count`` packets, or ``max_mb`` megabytes —
    whichever comes first. ``capture_filter`` is a BPF filter (``host 10.0.0.5
    and port 443``), applied in the kernel so unwanted traffic is never kept.
    Without ``output`` the file goes in ``directory`` (default
    ``WIRESHARK_CAPTURE_DIR``, else the temp folder). Returns the file's path
    and size; read it with the other verbs.

    Capturing needs privileges: root, or on Linux the ``wireshark`` group /
    ``cap_net_raw`` on dumpcap; on Windows, Npcap.
    """
    if not 1 <= duration <= MAX_DURATION:
        raise ValueError(f"duration must be 1–{MAX_DURATION} seconds")
    if max_mb < 1:
        raise ValueError("max_mb must be at least 1")
    path = output_path(output or default_capture_path(directory), overwrite=overwrite)
    argv = [find_tool("tshark", config), "-i", interface, "-q", "-n", "-w", path, "-a", f"duration:{duration}", "-a", f"filesize:{max_mb * 1000}"]
    if count:
        argv += ["-c", str(count)]
    if capture_filter:
        argv += ["-f", capture_filter]
    if snaplen:
        argv += ["-s", str(snaplen)]
    started = time.monotonic()
    try:
        run(argv, timeout=duration + 30)
    except WiresharkError as exc:
        text = str(exc)
        if "ermission" in text or "don't have permission" in text or "Operation not permitted" in text:
            raise WiresharkError(
                f"{text}\nCapturing needs privileges: run as root, or add the account to the wireshark group "
                "(`sudo usermod -aG wireshark <user>`, after `dpkg-reconfigure wireshark-common` on Debian/Ubuntu); "
                "on Windows install Npcap"
            ) from exc
        raise
    elapsed = round(time.monotonic() - started, 1)
    if not os.path.isfile(path):
        raise WiresharkError(f"tshark finished but wrote no file at {path}")
    info = file_info(config, path)
    out: dict[str, Any] = {"file": path, "interface": interface}
    if capture_filter:
        out["capture_filter"] = capture_filter
    out.update({"seconds": elapsed, "packets": info.get("packets", 0), "file_bytes": info.get("file_bytes", os.path.getsize(path))})
    if not out["packets"]:
        out["hint"] = "no packets captured — check the interface (list-interfaces) and the capture filter"
    return out


def save_filtered(
    config: WiresharkConfig,
    file: str | os.PathLike[str],
    filter: str,
    output: str,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write the packets matching a display ``filter`` to a new capture file —
    e.g. one conversation to hand to a colleague or a vendor."""
    source = capture_file(file)
    if os.path.abspath(output) == os.path.abspath(source):
        raise ValueError("output must be a different file from the input")
    path = output_path(output, overwrite=overwrite)
    run(read_argv(config, source, filter=filter) + ["-w", path], timeout=config.timeout)
    info = file_info(config, path)
    return {"file": path, "source": source, "filter": filter, "packets": info.get("packets", 0), "file_bytes": info.get("file_bytes", os.path.getsize(path))}


def merge(
    config: WiresharkConfig,
    files: list[str],
    output: str,
    *,
    append: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Merge capture files into one, interleaved by timestamp (``append`` =
    one after the other instead) — e.g. the client-side and server-side
    captures of the same problem."""
    if len(files) < 2:
        raise ValueError("name at least two capture files to merge")
    sources = [capture_file(f) for f in files]
    if any(os.path.abspath(output) == os.path.abspath(s) for s in sources):
        raise ValueError("output must be a different file from the inputs")
    path = output_path(output, overwrite=overwrite)
    argv = [find_tool("mergecap", config), "-w", path]
    if append:
        argv.append("-a")
    run(argv + sources, timeout=config.timeout)
    info = file_info(config, path)
    return {"file": path, "sources": sources, "packets": info.get("packets", 0), "file_bytes": info.get("file_bytes", os.path.getsize(path))}


def export_objects(
    config: WiresharkConfig,
    file: str | os.PathLike[str],
    directory: str,
    *,
    protocol: str = "http",
    limit: int = 50,
) -> dict[str, Any]:
    """Save the files carried in the capture — HTTP bodies, SMB and TFTP
    transfers, email (``imf``) — into ``directory`` (created if needed; must be
    empty or new, so nothing is overwritten). Returns the files written, largest
    first. They come from network traffic: treat them as untrusted."""
    if protocol not in EXPORT_PROTOCOLS:
        raise ValueError(f"protocol must be one of {', '.join(EXPORT_PROTOCOLS)}")
    folder = Path(directory)
    if folder.exists() and any(folder.iterdir()):
        raise WiresharkError(f"{folder} is not empty — choose a new or empty folder")
    folder.mkdir(parents=True, exist_ok=True)
    run(read_argv(config, file) + ["-q", "--export-objects", f"{protocol},{folder}"], timeout=config.timeout)
    sizes = sorted(((p.stat().st_size, p.name) for p in folder.iterdir() if p.is_file()), key=lambda t: (-t[0], t[1]))
    written = [{"name": name, "bytes": size} for size, name in sizes]
    shown = written[:limit] if limit else written
    return {
        "file": os.fspath(file),
        "protocol": protocol,
        "directory": str(folder),
        "count": len(shown),
        "total": len(written),
        "bytes": sum(size for size, _name in sizes),
        "truncated": len(shown) < len(written),
        "objects": shown,
    }
