"""``config-file``: write, edit and read config files as root, at allowlisted
paths only (``{venv}/runspec_root_files.conf``).

The narrow root counterpart of ``filesystem write-file``/``edit-file``: no move,
delete, list or search, and only where the venv's root-owned allowlist says.
Text may hold ``{{secret:<credential>}}`` placeholders, filled from
``RUNSPEC_SECRET_*`` (runspec-console sends the named credential on stdin) just
before the write and masked back in everything printed. See
``runspec_linux_core.rootfiles``.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import runspec as rs
from runspec_fs_core.secrets import (
    SecretNotAvailableError,
    fill_secrets,
    find_placeholders,
    has_placeholders,
    mask_secrets,
)
from runspec_linux_core import rootfiles as rf


def _venv() -> Path:
    return Path(sys.prefix)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str))


def _fill(text: str, masks: dict[str, str], ids: list[str]) -> str:
    if not has_placeholders(text):
        return text
    filled, m = fill_secrets(text)
    masks.update(m)
    for cid, _field in find_placeholders(text):
        if cid not in ids:
            ids.append(cid)
    return filled


def _audit(event: str, outcome: str, target: dict[str, Any], detail: dict[str, Any]) -> None:
    # The audit must never break the command's own result.
    with contextlib.suppress(Exception):
        rs.audit(event, outcome, target=target, detail=detail)


def _write_options(spec: rs.RunSpec) -> dict[str, Any]:
    return {
        "owner": spec.owner.value or None,
        "group": spec.group.value or None,
        "mode": spec.mode.value or None,
        "dry_run": bool(spec.dry_run.value),
    }


def _run(spec: rs.RunSpec, masks: dict[str, str], ids: list[str]) -> dict[str, Any]:
    command = spec.runspec_command
    venv = _venv()
    allowlist = venv / rf.ALLOWLIST_NAME
    backups = venv / rf.BACKUP_DIR_NAME
    if command == "allowed":
        return rf.allowed(allowlist)
    if command in ("allow", "disallow"):
        fn = rf.allow if command == "allow" else rf.disallow
        result = fn(allowlist, spec.pattern.value)
        if result["changed"]:
            _audit(
                f"root_file.{command}",
                "succeeded",
                {"allowlist": str(allowlist)},
                {"pattern": spec.pattern.value},
            )
        return result
    if command == "read":
        return rf.read_file(spec.path.value, allowlist)
    if command == "write":
        content = _fill(spec.content.value, masks, ids)
        return rf.write_file(
            spec.path.value,
            content,
            allowlist,
            backup_dir=backups,
            private=bool(masks),
            make_dirs=bool(spec.make_dirs.value),
            **_write_options(spec),
        )
    if command == "edit":
        olds = list(spec.old_text.value or [])
        news = list(spec.new_text.value or [])
        if len(olds) != len(news):
            raise rf.RootFileError(f"give one --new-text for each --old-text ({len(olds)} old, {len(news)} new). Nothing was changed.")
        edits = [(old, _fill(new, masks, ids)) for old, new in zip(olds, news, strict=True)]
        return rf.edit_file(
            spec.path.value,
            edits,
            allowlist,
            backup_dir=backups,
            private=bool(masks),
            **_write_options(spec),
        )
    raise SystemExit("no command — see `config-file --help`")  # pragma: no cover (require-command)


def main() -> None:
    spec = rs.parse("config-file")
    command = spec.runspec_command
    masks: dict[str, str] = {}
    ids: list[str] = []
    writes = command in ("write", "edit")
    target = {"path": spec.path.value} if writes else {}
    try:
        result = _run(spec, masks, ids)
    except (rf.RootFileError, SecretNotAvailableError, OSError, ValueError) as exc:
        if writes:
            _audit("root_file.written", "failed", target, {"verb": command, "credentials": ids, "error": str(exc)})
        _emit(mask_secrets({"error": str(exc), "kind": type(exc).__name__}, masks))
        sys.exit(1)
    if writes and result.get("changed") and not result.get("dry_run"):
        _audit(
            "root_file.written",
            "succeeded",
            {"path": result["path"]},
            {
                "verb": command,
                "created": result.get("created", False),
                "credentials": ids,
                "backup": result.get("backup", ""),
                "owner": result.get("owner"),
                "group": result.get("group"),
                "mode": result.get("mode"),
            },
        )
    _emit(mask_secrets(result, masks))


if __name__ == "__main__":
    main()
