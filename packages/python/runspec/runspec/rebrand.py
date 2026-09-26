"""
rebrand.py — republish a runspec package wheel under your own name.

Backs ``runspec rebrand``. Takes a published wheel (e.g. ``runspec-linux``) and
writes a byte-identical copy whose **distribution name** is yours (e.g.
``mycorp-linux``), ready to upload to a private index such as Nexus. Only the
packaging metadata changes:

- the ``*.dist-info`` directory (and any ``*.data`` directory) is renamed;
- ``METADATA`` gets the new ``Name`` (and, optionally, a new ``Version``);
- ``Requires-Dist`` entries named in ``rename_deps`` are re-pointed (the
  version specifier, extras and markers are kept exactly);
- ``RECORD`` is regenerated so every hash matches.

Code, ``runspec.toml``, entry points and the import package (``runspec_linux``)
are untouched, so the copy behaves exactly like the upstream release, and a new
upstream version is picked up just by re-running it. Shipping it in the core
library means it reaches a locked-down machine through the same index as
runspec itself — no separate script to transfer. Stdlib only (``pip`` is used
only to download a requirement).
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from email.parser import HeaderParser
from pathlib import Path

# PEP 427 wheel filename: {dist}-{version}(-{build})?-{python}-{abi}-{platform}.whl
_WHEEL_RE = re.compile(r"^(?P<dist>[^-]+)-(?P<version>[^-]+)(?:-(?P<build>\d[^-]*))?-(?P<tags>[^-]+-[^-]+-[^-]+)\.whl$")
# PEP 508 name at the start of a requirement string.
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


class RebrandError(Exception):
    """A wheel or an argument can't be rebranded as asked."""


def canonical(name: str) -> str:
    """PEP 503 normalised project name (``Runspec_Linux`` → ``runspec-linux``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def filename_component(name: str) -> str:
    """Name as it appears in a wheel filename / dist-info dir (PEP 427 escaping)."""
    return re.sub(r"[-_.]+", "_", name).lower()


@dataclass
class RebrandResult:
    """What a rebrand produced — printed by the CLI, asserted on by tests."""

    source: Path
    output: Path
    old_name: str
    new_name: str
    version: str
    renamed_deps: list[tuple[str, str]] = field(default_factory=list)
    runspec_tomls: list[str] = field(default_factory=list)
    requires_runspec: bool = False
    unused_renames: list[str] = field(default_factory=list)


def _record_hash(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _rewrite_metadata(text: str, new_name: str, new_version: str | None, dep_map: dict[str, str]) -> tuple[str, list[tuple[str, str]], bool]:
    """Rewrite the header block of a METADATA file, leaving the body verbatim.

    Returns ``(text, renamed_deps, requires_runspec)``. Works line-by-line on the
    header block rather than round-tripping through ``email`` so folding, header
    order and the long-description body are preserved byte for byte.
    """
    header, sep, body = text.partition("\n\n")
    out: list[str] = []
    renamed: list[tuple[str, str]] = []
    requires_runspec = False
    for line in header.split("\n"):
        key, colon, value = line.partition(":")
        if colon and key == "Name":
            line = f"Name: {new_name}"
        elif colon and key == "Version" and new_version:
            line = f"Version: {new_version}"
        elif colon and key == "Requires-Dist":
            m = _REQ_NAME_RE.match(value)
            if m:
                dep = canonical(m.group(1))
                if dep == "runspec" and "extra ==" not in value:
                    requires_runspec = True
                if dep in dep_map:
                    value = value[: m.start(1)] + dep_map[dep] + value[m.end(1) :]
                    renamed.append((dep, dep_map[dep]))
                    line = f"Requires-Dist:{value}"
        out.append(line)
    return "\n".join(out) + sep + body, renamed, requires_runspec


def rebrand_wheel(
    source: Path,
    new_name: str,
    out_dir: Path,
    *,
    rename_deps: dict[str, str] | None = None,
    new_version: str | None = None,
) -> RebrandResult:
    """Write a copy of ``source`` distributed as ``new_name`` into ``out_dir``."""
    if not _VALID_NAME_RE.match(new_name):
        raise RebrandError(f"invalid distribution name: {new_name!r}")
    m = _WHEEL_RE.match(source.name)
    if not m:
        raise RebrandError(f"not a wheel filename: {source.name}")
    old_dist, old_version, build, tags = m.group("dist"), m.group("version"), m.group("build"), m.group("tags")
    version = new_version or old_version
    new_dist = filename_component(new_name)

    old_info, old_data = f"{old_dist}-{old_version}.dist-info/", f"{old_dist}-{old_version}.data/"
    new_info, new_data = f"{new_dist}-{version}.dist-info/", f"{new_dist}-{version}.data/"
    dep_map = {canonical(k): v for k, v in (rename_deps or {}).items()}

    out_name = "-".join(p for p in (new_dist, version, build, tags) if p) + ".whl"
    out_path = out_dir / out_name
    if out_path.resolve() == source.resolve():
        raise RebrandError("output would overwrite the source wheel — pass a different --out")

    with zipfile.ZipFile(source) as zin:
        names = zin.namelist()
        if old_info + "METADATA" not in names:
            raise RebrandError(f"{source.name}: no {old_info}METADATA (is this a wheel?)")
        old_name = HeaderParser().parsestr(zin.read(old_info + "METADATA").decode("utf-8"))["Name"] or old_dist

        entries: list[tuple[zipfile.ZipInfo, bytes]] = []
        renamed: list[tuple[str, str]] = []
        requires_runspec = False
        tomls: list[str] = []
        for info in zin.infolist():
            name = info.filename
            if name == old_info + "RECORD":
                continue  # regenerated below
            data = zin.read(info)
            if name.startswith(old_info):
                name = new_info + name[len(old_info) :]
            elif name.startswith(old_data):
                name = new_data + name[len(old_data) :]
            if name == new_info + "METADATA":
                text, renamed, requires_runspec = _rewrite_metadata(data.decode("utf-8"), new_name, new_version, dep_map)
                data = text.encode("utf-8")
            if name.rsplit("/", 1)[-1] == "runspec.toml":
                tomls.append(name)
            new_info_obj = zipfile.ZipInfo(name, date_time=info.date_time)
            new_info_obj.compress_type = info.compress_type
            new_info_obj.external_attr = info.external_attr
            new_info_obj.create_system = info.create_system
            entries.append((new_info_obj, data))

    unused = sorted(set(dep_map) - {old for old, _ in renamed})

    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for zi, data in entries:
        writer.writerow([zi.filename, _record_hash(data), len(data)])
    writer.writerow([new_info + "RECORD", "", ""])
    record_info = zipfile.ZipInfo(new_info + "RECORD", date_time=entries[-1][0].date_time if entries else (1980, 1, 1, 0, 0, 0))
    record_info.compress_type = zipfile.ZIP_DEFLATED
    record_info.external_attr = 0o644 << 16
    entries.append((record_info, record.getvalue().encode("utf-8")))

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".whl.tmp")
    with zipfile.ZipFile(tmp, "w") as zout:
        for zi, data in entries:
            zout.writestr(zi, data)
    tmp.replace(out_path)

    return RebrandResult(
        source=source,
        output=out_path,
        old_name=old_name,
        new_name=new_name,
        version=version,
        renamed_deps=renamed,
        runspec_tomls=tomls,
        requires_runspec=requires_runspec,
        unused_renames=unused,
    )


def download_wheel(requirement: str, dest: Path) -> Path:
    """``pip download`` one wheel (no deps) using pip's own index configuration."""
    cmd = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:", "--dest", str(dest), requirement]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RebrandError(f"pip download {requirement} failed:\n{proc.stderr.strip() or proc.stdout.strip()}")
    wheels = sorted(dest.glob("*.whl"))
    if len(wheels) != 1:
        raise RebrandError(f"expected one wheel from pip download {requirement}, got {len(wheels)}")
    return wheels[0]


def parse_renames(values: list[str]) -> dict[str, str]:
    """Parse ``OLD=NEW`` pairs from ``--rename-dep``."""
    mapping: dict[str, str] = {}
    for item in values:
        old, eq, new = item.partition("=")
        old, new = old.strip(), new.strip()
        if not eq or not old or not new or not _VALID_NAME_RE.match(new):
            raise RebrandError(f"--rename-dep expects OLD=NEW, got {item!r}")
        mapping[old] = new
    return mapping


def rebrand(source: str, new_name: str, out_dir: Path, *, rename_deps: dict[str, str] | None = None, new_version: str | None = None) -> RebrandResult:
    """Rebrand ``source`` — a ``.whl`` path, or a requirement to ``pip download``."""
    src = Path(source)
    with tempfile.TemporaryDirectory() as tmp:
        if not (src.suffix == ".whl" and src.is_file()):
            src = download_wheel(source, Path(tmp))
        return rebrand_wheel(src, new_name, out_dir, rename_deps=rename_deps, new_version=new_version)
