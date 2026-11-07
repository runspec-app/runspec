"""Installed-code integrity — is a runnable's installed code what was installed?

``parse()`` calls :func:`check_runnable` before a run: the distribution that owns
the runnable's ``runspec.toml``, plus each distribution it requires directly, has
every hashed ``RECORD`` file hashed again and compared. An edited or deleted file
makes the run ``modified``: a stderr warning, an audit event
(``runnable.modified``, once per distinct set of changes) and an ``integrity``
field on the run record + runs index. A distribution installed from a folder, a
file or a VCS (PEP 610 ``direct_url.json``) is recorded as ``local``; an
editable install (``pip install -e``) is never checked.

Honest limits: this is tamper-*evident*. Whoever can edit an installed file can
also edit ``RECORD`` or this module, and module code runs at import, before
``parse()``. It catches the "quick fix on the box, never committed" habit; code
that must not be changed by the account running it belongs to another account.

Stdlib only; never raises into the caller. See SPEC.md → "Installed-Code
Integrity" and docs/integrity.md.
"""

from __future__ import annotations

import base64
import contextlib
import csv
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

__all__ = [
    "INTEGRITY_ENV",
    "DistCheck",
    "Report",
    "check_runnable",
    "check_dist",
    "dist_source",
    "owning_dist",
    "direct_requirements",
    "report_run",
    "enabled",
]

INTEGRITY_ENV = "RUNSPEC_INTEGRITY"
STATE_NAME = ".integrity-state.json"
# How many changed files a run record / warning carries (the full list is
# `runspec verify`'s job).
MAX_FILES = 20
_WARN_FILES = 3

_LOCAL_SOURCES = frozenset({"folder", "archive", "vcs"})
_REQ_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXTRA_MARKER = re.compile(r"""\bextra\s*==""")


def enabled() -> bool:
    """False when ``RUNSPEC_INTEGRITY=off`` (test suites, accepted local changes)."""
    return os.environ.get(INTEGRITY_ENV, "").strip().lower() not in ("off", "0", "false", "no")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class DistCheck:
    """One distribution's result."""

    name: str
    version: str
    source: str  # index | editable | folder | archive | vcs
    status: str  # ok | modified | skipped | unverifiable
    checked: int = 0
    modified: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # current digests of the changed files — the fingerprint of *this* change
    digests: dict[str, str] = field(default_factory=dict)

    @property
    def changed(self) -> list[str]:
        return [*self.modified, *self.missing]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "status": self.status,
            "checked": self.checked,
            "modified": list(self.modified),
            "missing": list(self.missing),
        }


@dataclass
class Report:
    """The result for one runnable (its own distribution first)."""

    dists: list[DistCheck]

    @property
    def modified(self) -> list[DistCheck]:
        return [d for d in self.dists if d.status == "modified"]

    @property
    def token(self) -> str | None:
        """``modified`` / ``local`` / ``None`` — the run record's ``integrity``."""
        if self.modified:
            return "modified"
        if any(d.source in _LOCAL_SOURCES and d.status != "skipped" for d in self.dists):
            return "local"
        return None

    def files(self, limit: int = MAX_FILES) -> list[str]:
        out = [f"{d.name}: {p}" for d in self.modified for p in d.changed]
        return out[:limit]


# ── Classifying a distribution ───────────────────────────────────────────────


def dist_source(dist: metadata.Distribution) -> str:
    """Where a distribution came from, from PEP 610 ``direct_url.json``."""
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return "index"
    try:
        data = json.loads(raw)
    except ValueError:
        return "index"
    if not isinstance(data, dict):
        return "index"
    if isinstance(data.get("dir_info"), dict):
        return "editable" if data["dir_info"].get("editable") else "folder"
    if "vcs_info" in data:
        return "vcs"
    if "archive_info" in data:
        return "archive"
    return "index"


def _digest(path: Path, mode: str) -> str:
    h = hashlib.new(mode)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode("ascii")


def _record_rows(dist: metadata.Distribution) -> list[tuple[str, str, str]]:
    """``(path, mode, value)`` for every hashed ``RECORD`` entry. Read directly
    rather than via ``Distribution.files``, which (Python 3.12+) leaves out files
    that no longer exist — a deleted file must count as changed."""
    try:
        text = dist.read_text("RECORD")
    except Exception:  # noqa: BLE001
        text = None
    out: list[tuple[str, str, str]] = []
    for row in csv.reader((text or "").splitlines()):
        if len(row) < 2 or not row[0] or "=" not in row[1]:
            continue
        mode, _, value = row[1].partition("=")
        out.append((row[0], mode, value))
    return out


def check_dist(dist: metadata.Distribution) -> DistCheck:
    """Hash every hashed ``RECORD`` file of ``dist`` and compare."""
    name = str(dist.name or "")
    version = str(dist.version or "")
    source = dist_source(dist)
    if source == "editable":
        return DistCheck(name, version, source, "skipped")
    result = DistCheck(name, version, source, "ok")
    for rel, mode, expected in _record_rows(dist):
        try:
            path = Path(str(dist.locate_file(rel)))
            actual = _digest(path, mode)
        except FileNotFoundError:
            result.missing.append(rel)
            result.digests[rel] = "missing"
            continue
        except (OSError, ValueError, TypeError):  # unreadable / unknown hash mode — can't judge
            continue
        result.checked += 1
        if actual != expected:
            result.modified.append(rel)
            result.digests[rel] = actual
    if result.changed:
        result.status = "modified"
    elif result.checked == 0:
        result.status = "unverifiable"
    return result


# ── Finding a runnable's distributions ───────────────────────────────────────


def _owns(dist: metadata.Distribution, target: Path) -> bool:
    for f in dist.files or []:
        if f.name != target.name:
            continue
        with contextlib.suppress(OSError, ValueError):
            if Path(f.locate()).resolve() == target:
                return True
    return False


def owning_dist(config_path: Path) -> metadata.Distribution | None:
    """The installed distribution whose files include ``config_path``, or ``None``
    (a checkout, an editable install's source tree, a loose script)."""
    try:
        target = Path(config_path).resolve()
    except OSError:
        return None
    # Fast path: the import package directory is usually named after the
    # distribution (runspec_linux ↔ runspec-linux). Try each ancestor's name.
    for parent in list(target.parents)[:4]:
        with contextlib.suppress(Exception):
            dist = metadata.distribution(parent.name)
            if _owns(dist, target):
                return dist
    # Rebranded / differently named distributions (runspec rebrand): look through
    # the distributions installed in the sys.path entry that holds the spec.
    # (Not packages_distributions(): on Python 3.10 it reads only top_level.txt,
    # which hatchling-built wheels don't have.)
    with contextlib.suppress(Exception):
        for sp in map(Path, sys.path):
            with contextlib.suppress(OSError, ValueError):
                if len(target.relative_to(sp.resolve()).parts) < 2:
                    continue
                for dist in metadata.distributions(path=[str(sp)]):
                    if _owns(dist, target):
                        return dist
    return None


def direct_requirements(dist: metadata.Distribution) -> list[metadata.Distribution]:
    """The installed distributions ``dist`` requires directly — requirements that
    only apply to an extra are skipped, and ones not installed are left out."""
    out: list[metadata.Distribution] = []
    seen = {_norm(str(dist.name or ""))}
    for req in dist.requires or []:
        spec, _, marker = req.partition(";")
        if marker and _EXTRA_MARKER.search(marker):
            continue
        m = _REQ_NAME.match(spec)
        if not m:
            continue
        key = _norm(m.group(1))
        if key in seen:
            continue
        seen.add(key)
        with contextlib.suppress(Exception):
            out.append(metadata.distribution(m.group(1)))
    return out


def check_runnable(config_path: Path) -> Report | None:
    """Check the distribution that owns ``config_path`` and its direct
    requirements. ``None`` when nothing is checked (switched off, or the spec is
    not part of an installed distribution). Never raises."""
    if not enabled():
        return None
    try:
        dist = owning_dist(config_path)
        if dist is None:
            return None
        return Report([check_dist(d) for d in (dist, *direct_requirements(dist))])
    except Exception:  # noqa: BLE001 — the check never breaks a run
        return None


# ── Acting on a result ───────────────────────────────────────────────────────


def _fingerprint(d: DistCheck) -> str:
    lines = sorted(f"{p}={h}" for p, h in d.digests.items())
    return hashlib.sha256("\n".join([d.name, d.version, *lines]).encode("utf-8")).hexdigest()


def _state_path() -> Path:
    return Path(sys.prefix) / "logs" / STATE_NAME


def _read_state(path: Path) -> dict[str, str]:
    with contextlib.suppress(Exception):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    return {}


def _write_state(path: Path, state: dict[str, str]) -> bool:
    try:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False


def _warning(name: str, report: Report) -> str:
    files = [p for d in report.modified for p in d.changed]
    shown = ", ".join(files[:_WARN_FILES]) + (f" and {len(files) - _WARN_FILES} more" if len(files) > _WARN_FILES else "")
    dists = ", ".join(f"{d.name} {d.version}" for d in report.modified)
    noun = "file differs" if len(files) == 1 else "files differ"
    return f'⚠  {name}: {len(files)} installed {noun} from the package ({dists}): {shown}. This run is recorded as modified. Reinstall the package to restore it; "runspec verify" lists the changes.\n'


def report_run(name: str, report: Report | None, *, state_path: Path | None = None) -> None:
    """Warn on stderr and record the ``runnable.modified`` audit event for a
    ``modified`` report — the event once per distinct set of changes. Never raises."""
    if report is None:
        return
    if report.modified:
        with contextlib.suppress(Exception):
            sys.stderr.write(_warning(name, report))
    with contextlib.suppress(Exception):
        path = state_path or _state_path()
        state = _read_state(path) if (report.modified or path.exists()) else {}
        changed = False
        # A distribution that is clean again forgets its last change, so the same
        # edit made again later is recorded again.
        for d in report.dists:
            if d.status != "modified" and d.name in state:
                del state[d.name]
                changed = True
        for d in report.modified:
            fp = _fingerprint(d)
            if state.get(d.name) == fp:
                continue
            from .auditlog import audit

            audit(
                "runnable.modified",
                "detected",
                target={"distribution": d.name, "version": d.version},
                detail={"files": d.changed[:MAX_FILES], "count": len(d.changed), "source": d.source},
            )
            state[d.name] = fp
            changed = True
        if changed:
            _write_state(path, state)
