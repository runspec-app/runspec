"""run-playbook — run an Ansible playbook locally on this host.

The runspec-console reaches the target host over its own SSH connection (the
host from ``runspec_hosts.toml``) and runs this runnable there. The playbook is
then executed against ``localhost`` with ``ansible_connection=local``, so
Ansible itself opens no SSH connection. Per-host concerns the console already
owns — connection, credentials (injected as env vars), denylist, audit — keep
applying; Ansible supplies the playbook richness (idempotent modules, handlers,
templates, ``become:``, ``--check``).

The playbook is either a file in a git repo the console sends with the run
(``--repo`` names a repo in the console's Repos tab; the checkout arrives on
stdin, see :mod:`runspec_playbook.source`) or one bundled in this package
(``deploy``).

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

from runspec_playbook import source as source_mod


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


def local_inventory(sent: source_mod.SentRepo | None) -> dict[str, Any]:
    """The one-host inventory: this host, on the local connection.

    When the console sent a repo it also names this host and its console groups
    and profiles, so the inventory uses that name (``host_vars/<name>.yml``
    applies) and puts the host in those groups (``hosts: web`` and
    ``group_vars/web.yml`` apply). Without them it is plain ``localhost``.
    """
    name = (sent.host if sent is not None else "") or "localhost"
    inv: dict[str, Any] = {"all": {"hosts": {name: {"ansible_connection": "local"}}}}
    groups = [g for g in (sent.groups if sent is not None else []) if g and g != "all"]
    if groups:
        inv["all"]["children"] = {g: {"hosts": {name: {}}} for g in groups}
    return inv


def run_ansible(
    playbook_name: str,
    extravars: dict[str, Any],
    *,
    check: bool,
    verbosity: int,
    tags: str | None,
    skip_tags: str | None,
    envvars: dict[str, str],
    sent: source_mod.SentRepo | None = None,
) -> dict[str, Any]:
    """Run a playbook against localhost via ansible-runner.

    ``sent`` (a repo the console sent with the run) is written into a
    throwaway private data dir; otherwise the bundled ``playbooks/`` tree is
    copied there. Either way artefacts never litter the host. ``envvars`` is
    forwarded verbatim so the console-injected secrets reach the playbook (read
    there with ``lookup('env', 'NAME')``); this function never logs them.
    """
    import shutil
    from importlib.resources import as_file, files

    import ansible_runner

    bundled = files("runspec_playbook") / "playbooks"
    with as_file(bundled) as playbooks_dir, tempfile.TemporaryDirectory(prefix="runspec-playbook-") as data_dir:
        project = Path(data_dir) / "project"
        if sent is not None:
            project.mkdir()
            source_mod.write_files(sent, project)
            entry = source_mod.safe_relpath(playbook_name)
            if not (project / entry).is_file():
                raise FileNotFoundError(f"playbook {playbook_name!r} is not in repo {sent.repo!r}")
        else:
            shutil.copytree(playbooks_dir, project)
            entry = f"{playbook_name}.yml"
            if not (project / entry).is_file():
                raise FileNotFoundError(f"playbook not found: {playbook_name!r} is not bundled in this package (to run one from a git repo, pass --repo with a repo from the console's Repos tab)")

        kwargs: dict[str, Any] = {
            "private_data_dir": data_dir,
            "playbook": entry,
            # One inline host pinned to the local connection — no SSH, no remote inventory.
            "inventory": local_inventory(sent),
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


def sent_repo(repo: str, environ: dict[str, str], stdin: Any) -> source_mod.SentRepo | None:
    """The repo the console sent for this run, or None when ``repo`` is empty.

    Pops ``RUNSPEC_PLAYBOOK_SOURCE`` from ``environ`` so it is never forwarded
    to Ansible. ``--repo`` without a sent repo (run by hand, or by a console
    that doesn't know the repo) is an error, as is a sent repo with another name.
    """
    header = environ.pop(source_mod.ENV_VAR, "")
    if not repo:
        return None
    if not header:
        raise source_mod.SourceError(f"--repo {repo!r} needs the runspec-console to send the repo with the run (run it from the console, where the repo is registered in the Repos tab)")
    sent = source_mod.receive(header, stdin)
    if sent.repo != repo:
        raise source_mod.SourceError(f"the console sent repo {sent.repo!r} but --repo is {repo!r}")
    return sent


def main_run_playbook() -> None:
    spec = rs.parse("run-playbook")
    package = str(spec.package.value or "")
    version = str(spec.version.value or "")
    playbook_name = str(spec.playbook.value)
    repo = str(spec.repo.value or "")
    check = bool(spec.check.value)
    verbosity = int(spec.verbosity.value)
    tags = spec.tags.value
    skip_tags = spec.skip_tags.value
    extra_items = spec.extra_var.value or []

    envvars = dict(os.environ)
    try:
        sent = sent_repo(repo, envvars, sys.stdin.buffer)
    except source_mod.SourceError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(2)

    try:
        extravars: dict[str, Any] = {}
        package_spec = ""
        if package:
            package_spec = compose_package_spec(package, version)
            extravars.update(
                deploy_package=package,
                deploy_version=version,
                deploy_package_spec=package_spec,
            )
        elif sent is None and playbook_name == "deploy":
            raise ValueError("the bundled 'deploy' playbook needs a package (and version) to install")
        extravars.update(parse_extra_vars(extra_items))
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
            envvars=envvars,
            sent=sent,
        )
    except ModuleNotFoundError as exc:
        print(json.dumps({"error": f"ansible-runner is not installed ({exc}). Install ansible-core and ansible-runner on this host."}))
        sys.exit(1)
    except (FileNotFoundError, source_mod.SourceError) as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(2)

    summary = summarize_stats(result.get("stats") or {})
    status = result.get("status")
    rc = int(result.get("rc") or 0)
    out: dict[str, Any] = {"playbook": playbook_name}
    if sent is not None:
        out.update(repo=sent.repo, commit=sent.commit[:12], dirty=sent.dirty)
    else:
        out["source"] = "bundled"
    if package_spec:
        out["package"] = package_spec
    out.update(
        check=check,
        status=status,
        rc=rc,
        changed=summary["changed"] > 0,
        stats=summary,
    )
    print(json.dumps(out))
    sys.exit(0 if status == "successful" else (rc or 1))
