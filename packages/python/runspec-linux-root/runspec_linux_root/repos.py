"""``package-repo``: list, add and remove package repositories (apt, dnf/yum, zypper)."""

from __future__ import annotations

import json
import sys
from typing import Any

import runspec as rs
from runspec_linux_core.errors import LinuxCoreError
from runspec_linux_core.repos import add_repo, list_repos, remove_repo


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=str))


def _run(spec: rs.RunSpec) -> dict[str, Any]:
    command = spec.runspec_command
    if command == "list":
        return list_repos(spec.filter.value or None)
    if command == "add":
        return add_repo(
            spec.name.value,
            apt_url=spec.apt_url.value or None,
            apt_key=spec.apt_key.value or None,
            apt_suite=spec.apt_suite.value,
            apt_components=spec.apt_components.value,
            rpm_url=spec.rpm_url.value or None,
            rpm_key=spec.rpm_key.value or None,
            key_fingerprints=list(spec.key_fingerprint.value or []),
            signed_metadata=not spec.unsigned_metadata.value,
            replace=spec.replace.value,
            refresh=not spec.no_refresh.value,
            proxy=spec.proxy.value or None,
            ca_bundle=spec.ca_bundle.value or None,
        )
    if command == "remove":
        return remove_repo(spec.name.value)
    raise SystemExit("no command — see `package-repo --help`")  # pragma: no cover (require-command)


def main() -> None:
    spec = rs.parse("package-repo")
    try:
        result = _run(spec)
    except (LinuxCoreError, ValueError) as exc:
        # LinuxCoreError covers ToolNotFoundError (no package manager) and CommandError.
        _emit({"error": str(exc), "kind": type(exc).__name__})
        sys.exit(1)
    _emit(result)


if __name__ == "__main__":
    main()
