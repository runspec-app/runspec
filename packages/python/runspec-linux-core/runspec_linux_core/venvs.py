"""Python virtualenv management: create venvs, install into them, write pip.conf.

All three helpers operate in user space (no root): a venv, a pip install into it,
and a ``pip.conf`` under a home or a venv. The driving use case is a **service
account that owns a venv shared between several consoles**, so each helper takes an
optional ``run_as`` user and escalates *internally* — ``sudo -n -u <user>`` (or
``su``) wrapping every command — rather than the console's declarative ``run_as``
(the account is chosen per-invocation, not fixed in ``runspec.toml``). ``-n`` makes
sudo fail fast instead of hanging on the TTY-less SSH channel; passwordless sudo is
assumed (see ``enable-passwordless-sudo``).

Each function returns plain data and raises on failure (``CommandError`` for a
non-zero subprocess, ``ValueError`` for bad input). The thin ``runspec-linux``
wrappers render the JSON/exit.
"""

import configparser
import contextlib
import io
import os
import pwd
import re
import shutil
import subprocess
import tempfile

from runspec_linux_core.become import build_become, check_become_error
from runspec_linux_core.errors import CommandError

# A POSIX-ish user/group name. Used here to validate a *group* before it is
# interpolated into a chgrp (a crafted value can't inject extra tokens — argv
# only, never a shell — but this keeps the error early and clear). The run-as
# *user* is validated by the shared ``build_become``. Mirrors ``become._USER_RE``.
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]*\$?$")


def _tail(output: str, limit: int = 40) -> list[str]:
    """The last ``limit`` non-empty-trimmed lines, so payloads stay bounded."""
    return output.strip().splitlines()[-limit:]


def _validate_name(value: str, what: str) -> str:
    if not _NAME_RE.match(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True)


def _check(result: subprocess.CompletedProcess[str], what: str, run_as: str | None) -> None:
    """Raise CommandError on non-zero, with a passwordless-sudo hint when relevant."""
    if result.returncode == 0:
        return
    check_become_error(what, result.stderr, run_as)


def create_venv(
    python: str,
    dest: str,
    *,
    run_as: str | None = None,
    become_method: str = "sudo",
    group: str | None = None,
    system_site_packages: bool = False,
    upgrade_deps: bool = False,
    clear: bool = False,
    prompt: str | None = None,
    inbox: bool = True,
) -> dict:
    """Create a virtualenv at ``dest`` from a specific ``python`` interpreter.

    Always uses ``--symlinks``. Then normalises the tree to ``u=rwX,go=rX`` so
    directories are ``755`` and executables in ``bin/`` keep their ``+x`` — matching
    what a plain ``python -m venv`` produces under a default ``022`` umask, regardless
    of the caller's umask (``sudo -n -u <svc>`` often lands at ``027``, which would
    otherwise leave the venv at ``750`` and lock other consoles out). Then creates
    ``<dest>/logs`` and — with ``inbox`` (the default) — the **logs inbox**
    ``<dest>/logs/inbox`` (``01777``, owned by the venv owner): every other account
    that runs the venv's runnables (operators, root) drops its run there and the
    owner indexes it (runspec >= 0.66, docs/design/venv-logs-inbox.md), so
    ``logs/`` itself stays owner-only (``0755``). With ``group`` set the venv is
    ``chgrp -R``'d to it and ``logs/`` is ``2775`` (setgid) — the group-sharing
    alternative; ``inbox=False`` keeps the pre-inbox layout (``logs/`` ``2775``)
    and writes a ``logs/.no-inbox`` opt-out marker. With ``run_as`` set every step
    runs as that user (so the venv — and the inbox — are owned by it). Raises
    ValueError on a non-executable interpreter / bad name, or CommandError on a
    failed step.
    """
    if not (os.path.isfile(python) and os.access(python, os.X_OK)):
        raise ValueError(f"not an executable file: {python!r} (pick a real interpreter, not the system default)")

    flags: list[str] = ["--symlinks"]
    if system_site_packages:
        flags.append("--system-site-packages")
    if upgrade_deps:
        flags.append("--upgrade-deps")
    if clear:
        flags.append("--clear")
    if prompt:
        flags += ["--prompt", prompt]

    _check(_run(build_become([python, "-m", "venv", *flags, dest], run_as, become_method)), "venv creation", run_as)

    _check(_run(build_become(["chmod", "-R", "u=rwX,go=rX", dest], run_as, become_method)), "chmod venv", run_as)

    logs_dir = os.path.join(dest, "logs")
    _check(_run(build_become(["mkdir", "-p", logs_dir], run_as, become_method)), "logs dir", run_as)
    if group:
        _validate_name(group, "group")
        _check(_run(build_become(["chgrp", "-R", group, dest], run_as, become_method)), "chgrp", run_as)
    logs_mode = "2775" if (group or not inbox) else "0755"
    _check(_run(build_become(["chmod", logs_mode, logs_dir], run_as, become_method)), "chmod logs", run_as)
    inbox_dir = _setup_inbox(logs_dir, inbox, run_as, become_method)

    return {
        "python": python,
        "dest": dest,
        "run_as": run_as or "",
        "group": group or "",
        "logs_dir": logs_dir,
        "logs_mode": logs_mode,
        "inbox": inbox_dir,
        "created": True,
    }


def _setup_inbox(logs_dir: str, inbox: bool, run_as: str | None, become_method: str) -> str:
    """Create ``<logs>/inbox`` (``01777``) as the venv owner — or, with
    ``inbox=False``, write the ``.no-inbox`` opt-out marker. Returns the inbox path
    ("" when opted out). ``mkdir`` then ``chmod``, both as the owner, so the dir is
    never world-writable under any other account; an existing inbox that isn't a
    plain directory owned by the owner makes ``chmod`` fail (``-h`` isn't used —
    a symlink at the path is refused by the ``test`` first)."""
    inbox_dir = os.path.join(logs_dir, "inbox")
    marker = os.path.join(logs_dir, ".no-inbox")
    if not inbox:
        _check(_run(build_become(["touch", marker], run_as, become_method)), "no-inbox marker", run_as)
        return ""
    if _run(build_become(["test", "-L", inbox_dir], run_as, become_method)).returncode == 0:
        raise CommandError(f"refusing to set up {inbox_dir}: it is a symlink")
    _check(_run(build_become(["rm", "-f", marker], run_as, become_method)), "no-inbox marker", run_as)
    _check(_run(build_become(["mkdir", "-p", "-m", "0700", inbox_dir], run_as, become_method)), "inbox dir", run_as)
    _check(_run(build_become(["chmod", "1777", inbox_dir], run_as, become_method)), "chmod inbox", run_as)
    return inbox_dir


def share_venv_logs(
    venv: str,
    *,
    group: str | None = None,
    run_as: str | None = None,
    become_method: str = "sudo",
    inbox: bool = True,
) -> dict:
    """Make an existing venv's ``logs/`` dir shared — the repair path for a venv
    **not** built by ``create-venv`` (hand-made or legacy).

    **With ``inbox`` (the default)** it sets up the logs inbox
    ``<venv>/logs/inbox`` (``01777``, owned by the venv owner — run it as the owner
    with ``run_as``): every other account drops its runs there and the owner
    indexes them (runspec >= 0.66), so no group is needed and ``logs/``'s own mode
    is left alone. Given a ``group`` it *also* applies the group sharing below.
    ``inbox=False`` is the pre-inbox behaviour (group sharing only).

    **Group sharing:**
    Sets ``<venv>/logs`` to ``2775`` (setgid + group-writable) so new files —
    including the per-venv runs index ``runspec_runs.db`` and the audit-log
    files — inherit the directory's group and can be written by every console /
    service account in a shared group. When ``group`` is given it also
    ``chgrp``s the ``logs/`` **directory** to it.

    The chgrp is on the directory only, **not** ``-R``: ``logs/`` already holds
    per-run files owned by *other* users (a ``run_as="root"`` runnable leaves
    root-owned logs there), which a non-root caller can't chgrp — and the setgid
    bit means only *new* files need the group anyway (the DB is created fresh).
    ``create-venv --group`` does exactly this at build time; this brings a
    pre-existing venv up to the same state without recreating it. With
    ``run_as`` every step runs as that user (the venv's owner).

    Raises ValueError if ``group`` is malformed or ``<venv>`` isn't an existing
    directory, CommandError on a failed step.
    """
    if group is not None:
        _validate_name(group, "group")
    if not _dir_exists_as(venv, run_as, become_method):
        raise ValueError(f"not a directory: {venv!r} (point at an existing venv)")

    logs_dir = os.path.join(venv, "logs")
    _check(_run(build_become(["mkdir", "-p", logs_dir], run_as, become_method)), "logs dir", run_as)
    mode = ""
    if group or not inbox:
        if group:
            _check(_run(build_become(["chgrp", group, logs_dir], run_as, become_method)), "chgrp logs", run_as)
        _check(_run(build_become(["chmod", "2775", logs_dir], run_as, become_method)), "chmod logs", run_as)
        mode = "2775"
    inbox_dir = _setup_inbox(logs_dir, True, run_as, become_method) if inbox else ""

    return {
        "venv": venv,
        "logs_dir": logs_dir,
        "group": group or "",
        "run_as": run_as or "",
        "mode": mode,
        "inbox": inbox_dir,
        "shared": True,
    }


def install_into_venv(
    venv: str,
    packages: str = "",
    *,
    project: str | None = None,
    editable: bool = False,
    upgrade: bool = False,
    no_cache_dir: bool = False,
    force_reinstall: bool = False,
    run_as: str | None = None,
    become_method: str = "sudo",
) -> dict:
    """Install named packages and/or a local project into an existing venv's pip.

    Targets ``<venv>/bin/python -m pip install``. ``packages`` is space-separated
    named specs (validated against the shared package-token guard); ``project`` is a
    path to a directory with a ``pyproject.toml`` (or a wheel/sdist), installed
    editable when ``editable`` is set. At least one of ``packages``/``project`` is
    required. Raises ValueError on bad input, CommandError on a failed install.

    After the install, the venv tree is normalised to ``u=rwX,go=rX`` (the same
    step ``create_venv`` runs at build time) so pip's new directories are ``755``
    regardless of the caller's umask — a ``sudo`` upgrade under a PAM-set ``027``
    login umask otherwise lands site-packages entries at ``750`` and hides the
    packs from ``runspec local`` / ``importlib.metadata`` for anyone but the
    owner. The sweep **prunes** two things: ``<venv>/logs`` (the group-shared
    audit dir, which holds per-run log files owned by other users — a
    ``run_as="root"`` runnable leaves root-owned logs there — so its mode, and
    its inbox's, are left exactly as they were), and
    **symlinks** (a ``--symlinks`` venv's ``bin/python*`` point at the root-owned
    system interpreter *outside* the venv; ``find -exec chmod`` would dereference
    them and fail — or, under sudo, silently repermission the system Python —
    where ``chmod -R`` skipped symlinks for free). A symlink's own mode is unused
    on Linux, so pruning them changes nothing.
    """
    from runspec_linux_core.packages import _validate_packages

    venv_python = os.path.join(venv, "bin", "python")
    if not (os.path.isfile(venv_python) and os.access(venv_python, os.X_OK)):
        raise ValueError(f"no python in venv: {venv_python!r}")

    names = _validate_packages(packages) if packages.strip() else []
    if project is not None and (project.startswith("-") or not os.path.exists(project)):
        raise ValueError(f"project path not found: {project!r}")
    if editable and project is None:
        raise ValueError("--editable requires a project path")
    if not names and project is None:
        raise ValueError("nothing to install: give a package and/or a project")

    opts: list[str] = []
    if upgrade:
        opts.append("-U")
    if no_cache_dir:
        opts.append("--no-cache-dir")
    if force_reinstall:
        opts.append("--force-reinstall")

    target: list[str] = list(names)
    if project is not None:
        target += ["-e", project] if editable else [project]

    cmd = build_become([venv_python, "-m", "pip", "install", *opts, *target], run_as, become_method)
    result = _run(cmd)
    _check(result, "pip install", run_as)

    # Normalise pip's freshly-created site-packages entries to u=rwX,go=rX (the
    # same step create_venv runs) so a sudo install under a restrictive login
    # umask can't land them at 750 and hide the packs from `runspec local` /
    # importlib.metadata. Two subtrees are pruned:
    #   * <venv>/logs — the group-shared (2775) audit dir, which accrues per-run
    #     log files owned by *other* users (a run_as="root" runnable such as
    #     list-upgrades / upgrade-packages leaves root-owned logs there), so a
    #     non-root caller's chmod on them dies with "Operation not permitted".
    #   * symlinks (-type l) — a `--symlinks` venv's bin/python* point at the
    #     system interpreter (root-owned, *outside* the venv). `find -exec chmod`
    #     dereferences a symlink argument, so without this it would chmod the
    #     system python: "Operation not permitted" as a normal user, or a silent
    #     repermission of /usr/bin/python* under sudo. `chmod -R` skipped symlinks
    #     for free; the find sweep must skip them explicitly to match. A symlink's
    #     own mode is irrelevant on Linux, so nothing is lost.
    # The logs dir (and its inbox) keep whatever mode they have — the prune means
    # the sweep never touches them. (Before 0.12 this forced logs/ back to 2775
    # afterwards, which undid an owner-only 0755 logs/ + inbox layout.)
    logs_dir = os.path.join(venv, "logs")
    _check(
        _run(
            build_become(
                [
                    "find",
                    venv,
                    "-path",
                    logs_dir,
                    "-prune",
                    "-o",
                    "-type",
                    "l",
                    "-prune",
                    "-o",
                    "-exec",
                    "chmod",
                    "u=rwX,go=rX",
                    "{}",
                    "+",
                ],
                run_as,
                become_method,
            )
        ),
        "chmod venv",
        run_as,
    )
    return {
        "venv": venv,
        "packages": names,
        "project": project or "",
        "editable": editable,
        "installed": True,
        "output": _tail(result.stdout),
    }


def configure_pip(
    index_url: str,
    *,
    extra_index_url: str | None = None,
    trusted_host: str | None = None,
    cert: str | None = None,
    timeout: int | None = None,
    retries: int | None = None,
    proxy: str | None = None,
    require_virtualenv: bool = False,
    scope: str = "user",
    venv: str | None = None,
    run_as: str | None = None,
    become_method: str = "sudo",
) -> dict:
    """Write a ``pip.conf`` at user or venv scope for private-PyPI access.

    Builds a ``[global]`` section from the given settings (``index-url`` is the only
    required one) and, when ``require_virtualenv`` is set, also pins
    ``require-virtualenv`` + ``disable-pip-version-check``. The file is validated by
    re-parsing before it is installed. ``scope="user"`` targets
    ``<home>/.config/pip/pip.conf`` (the ``run_as`` user's home when escalating, else
    the caller's); ``scope="venv"`` targets ``<venv>/pip.conf`` and requires ``venv``.
    With ``run_as`` the file is installed via ``install -D -m 0644`` as that user (so
    it is owned by it); otherwise it is written atomically with a ``.bak`` of any
    existing file. Raises ValueError on bad input, CommandError on a failed step.
    """
    if scope not in ("user", "venv"):
        raise ValueError(f"invalid scope: {scope!r} (expected user or venv)")
    if scope == "venv":
        if not venv:
            raise ValueError("scope=venv requires a venv path")
        path = os.path.join(venv, "pip.conf")
    else:
        if run_as:
            _validate_name(run_as, "run-as user")
            home = pwd.getpwnam(run_as).pw_dir
        else:
            home = os.path.expanduser("~")
        path = os.path.join(home, ".config", "pip", "pip.conf")

    cfg = configparser.ConfigParser()
    section: dict[str, str] = {"index-url": index_url}
    if extra_index_url:
        section["extra-index-url"] = extra_index_url
    if trusted_host:
        section["trusted-host"] = trusted_host
    if cert:
        section["cert"] = cert
    if timeout is not None:
        section["timeout"] = str(timeout)
    if retries is not None:
        section["retries"] = str(retries)
    if proxy:
        section["proxy"] = proxy
    if require_virtualenv:
        section["require-virtualenv"] = "true"
        section["disable-pip-version-check"] = "true"
    cfg["global"] = section

    buf = io.StringIO()
    cfg.write(buf)
    content = buf.getvalue()

    # Validate it re-parses before we install it anywhere.
    check = configparser.ConfigParser()
    check.read_string(content)

    # Write to a temp file the run-as user can read (0644 — pip.conf is non-secret),
    # then install it atomically into place as that user (creating parent dirs).
    fd, tmp = tempfile.mkstemp(prefix="pip.conf.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.chmod(tmp, 0o644)
        if run_as:
            if _path_exists_as(path, run_as, become_method):
                _check(
                    _run(build_become(["cp", path, f"{path}.bak"], run_as, become_method)),
                    "backup pip.conf",
                    run_as,
                )
            _check(
                _run(build_become(["install", "-D", "-m", "0644", tmp, path], run_as, become_method)),
                "install pip.conf",
                run_as,
            )
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                shutil.copy2(path, f"{path}.bak")
            shutil.copy2(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)

    return {
        "path": path,
        "scope": scope,
        "run_as": run_as or "",
        "configured": True,
    }


def _path_exists_as(path: str, run_as: str | None, become_method: str) -> bool:
    """True if ``path`` exists from the run-as user's view (``test -e``)."""
    return _run(build_become(["test", "-e", path], run_as, become_method)).returncode == 0


def _dir_exists_as(path: str, run_as: str | None, become_method: str) -> bool:
    """True if ``path`` is a directory from the run-as user's view (``test -d``)."""
    return _run(build_become(["test", "-d", path], run_as, become_method)).returncode == 0
