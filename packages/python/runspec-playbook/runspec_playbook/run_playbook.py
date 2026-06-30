"""run-playbook — deploy a package by running a bundled Ansible playbook locally.

The runspec-console reaches the target host over its own SSH connection (the
host from ``runspec_hosts.toml``) and runs this runnable there. The playbook is
then executed against ``localhost`` with ``ansible_connection=local``, so
Ansible itself opens no SSH connection. Per-host concerns the console already
owns — connection, credentials (injected as env vars), denylist, audit — keep
applying; Ansible supplies the playbook richness (idempotent modules, handlers,
templates, ``become:``, ``--check``).

The pure helpers (``compose_package_spec``/``parse_extra_vars``/
``summarize_stats``) are factored out so they unit-test without Ansible, and the
single Ansible call goes through ``run_ansible`` so tests can substitute it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import runspec as rs


def compose_package_spec(package: str, version: str) -> str:
    """Combine a package (optionally with extras) and a pinned version.

    ``"myapp"`` + ``"1.4.2"`` → ``"myapp==1.4.2"``; extras are preserved:
    ``"myapp[postgres]"`` + ``"1.4.2"`` → ``"myapp[postgres]==1.4.2"``.
    """
    package = package.strip()
    version = version.strip()
    if not package:
        raise ValueError("package is required")
    if not version:
        return package
    return f"{package}=={version}"


def parse_extra_vars(items: list[str]) -> dict[str, str]:
    """Parse ``--extra-var key=value`` items into a dict (split on the first =)."""
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--extra-var must be key=value, got: {item!r}")
        key, value = item.split("=", 1)
        out[key.strip()] = value
    return out


def summarize_stats(stats: dict[str, Any]) -> dict[str, int]:
    """Reduce ansible-runner's per-host stats to friendly fleet-wide totals."""

    def total(key: str) -> int:
        section = stats.get(key) or {}
        return sum(section.values()) if isinstance(section, dict) else 0

    return {
        "ok": total("ok"),
        "changed": total("changed"),
        "failed": total("failures"),
        "unreachable": total("dark"),
        "skipped": total("skipped"),
    }


def run_ansible(
    playbook_name: str,
    extravars: dict[str, Any],
    *,
    check: bool,
    verbosity: int,
    tags: str | None,
    skip_tags: str | None,
    envvars: dict[str, str],
) -> dict[str, Any]:
    """Run a bundled playbook against localhost via ansible-runner.

    The bundled ``playbooks/`` tree is copied into a throwaway private data dir
    so artefacts never litter the host. ``envvars`` is forwarded verbatim so the
    console-injected secrets reach the playbook (read there with
    ``lookup('env', 'NAME')``); this function never logs them.
    """
    import shutil
    from importlib.resources import as_file, files

    import ansible_runner

    source = files("runspec_playbook") / "playbooks"
    with as_file(source) as playbooks_dir, tempfile.TemporaryDirectory(prefix="runspec-playbook-") as data_dir:
        project = Path(data_dir) / "project"
        shutil.copytree(playbooks_dir, project)
        if not (project / f"{playbook_name}.yml").is_file():
            raise FileNotFoundError(f"bundled playbook not found: {playbook_name}.yml")

        kwargs: dict[str, Any] = {
            "private_data_dir": data_dir,
            "playbook": f"{playbook_name}.yml",
            # One inline host pinned to the local connection — no SSH, no remote inventory.
            "inventory": {"all": {"hosts": {"localhost": {"ansible_connection": "local"}}}},
            "extravars": extravars,
            "envvars": envvars,
            "verbosity": verbosity,
            "quiet": True,
        }
        if tags:
            kwargs["tags"] = tags
        if skip_tags:
            kwargs["skip_tags"] = skip_tags
        if check:
            kwargs["cmdline"] = "--check"

        runner = ansible_runner.run(**kwargs)
        return {"status": runner.status, "rc": runner.rc, "stats": runner.stats or {}}


def main_run_playbook() -> None:
    spec = rs.parse("run-playbook")
    package = str(spec.package.value)
    version = str(spec.version.value)
    playbook_name = str(spec.playbook.value)
    check = bool(spec.check.value)
    verbosity = int(spec.verbosity.value)
    tags = spec.tags.value
    skip_tags = spec.skip_tags.value
    extra_items = spec.extra_var.value or []

    try:
        package_spec = compose_package_spec(package, version)
        extravars: dict[str, Any] = {
            "deploy_package": package,
            "deploy_version": version,
            "deploy_package_spec": package_spec,
            **parse_extra_vars(extra_items),
        }
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(2)

    try:
        result = run_ansible(
            playbook_name,
            extravars,
            check=check,
            verbosity=verbosity,
            tags=tags,
            skip_tags=skip_tags,
            # The console already curated this environment (it added the selected
            # credentials' env vars); forward it so the playbook can read them.
            envvars=dict(os.environ),
        )
    except ModuleNotFoundError as exc:
        print(json.dumps({"error": f"ansible-runner is not installed ({exc}). Install ansible-core and ansible-runner on this host."}))
        sys.exit(1)
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)

    summary = summarize_stats(result.get("stats") or {})
    status = result.get("status")
    rc = int(result.get("rc") or 0)
    print(
        json.dumps(
            {
                "package": package_spec,
                "playbook": playbook_name,
                "check": check,
                "status": status,
                "rc": rc,
                "changed": summary["changed"] > 0,
                "stats": summary,
            }
        )
    )
    sys.exit(0 if status == "successful" else (rc or 1))
