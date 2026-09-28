"""invocation.py — turn a schedule's ``{name: value}`` args into a command line.

A schedule stores args as a plain map (``args = { path = "/tmp", verbose = true }``);
the runnable's own ``runspec.toml`` says what each one *is*. This module plans the
argv + environment that expresses that map faithfully, using the runnable's spec
(with core's own inference, so ``default = false`` is a flag here exactly as it is
to the runnable):

* the runnable's own (global) args, then the subcommand path, then the command's
  args — runspec's documented order (``name [globals] <command> [command args]``);
* a **flag**: ``true`` → ``--name``; ``false`` → nothing when that's already the
  default, else ``RUNSPEC_<RUNNABLE>_ARG_<NAME>=0`` in the environment (argv has
  no way to switch a default-true flag off);
* a **multiple** arg: a list → the flag repeated per item;
* a **positional**: passed by name (``--target prod``; runspec ≥ 0.62);
* a **rest** arg: a list → appended after a ``--``;
* anything argv can't express faithfully is refused with a reason
  (:class:`InvocationError`) — a list for a single-value arg, a nested table, a
  non-boolean flag, and ``password`` args (secrets belong in the credential env
  file, never in the config/crontab).

An arg the spec doesn't declare falls back to untyped handling (``true`` →
``--name``, ``false`` → omitted, list → repeated, scalar → ``--name value``);
:func:`runspec_scheduler.discovery.check_invocation` rejects undeclared names at
``add`` time, so this only matters for callers without a spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from runspec.inference import infer_arg


class InvocationError(ValueError):
    """A schedule arg the command line can't express (the message says why)."""


@dataclass(frozen=True)
class Invocation:
    """The argv after the runnable's binary, plus extra environment for the run."""

    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def _norm(name: str) -> str:
    return str(name).replace("_", "-")


def _commands_of(node: dict[str, Any]) -> dict[str, Any]:
    cmds = node.get("commands")
    return cmds if isinstance(cmds, dict) else {}


def effective_args(spec: dict[str, Any], command: tuple[str, ...] | list[str]) -> tuple[dict[str, tuple[str, dict[str, Any]]], frozenset[str]]:
    """``{normalised name: (declared name, inferred arg spec)}`` for the runnable's
    own args plus every command's args along ``command`` (deeper wins), and the
    set of the runnable's own (global) names."""
    eff: dict[str, tuple[str, dict[str, Any]]] = {}

    def add(node: dict[str, Any]) -> None:
        for name, raw in (node.get("args") or {}).items():
            try:
                inferred = infer_arg(dict(raw or {}))
            except ValueError:  # a malformed spec is the runnable's problem, not ours
                inferred = dict(raw or {})
            eff[_norm(name)] = (str(name), inferred)

    add(spec)
    globals_ = frozenset(eff)
    node = spec
    for verb in command:
        node = _commands_of(node).get(verb) or {}
        add(node)
    return eff, globals_


def env_key(runnable: str, arg_name: str) -> str:
    """``RUNSPEC_<RUNNABLE>_ARG_<NAME>`` — the automatic env channel core reads."""
    return f"RUNSPEC_{runnable.upper().replace('-', '_')}_ARG_{arg_name.upper().replace('-', '_')}"


def _scalar(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise InvocationError(f"argument '{key}': a {type(value).__name__} can't be passed on the command line")


def plan_invocation(
    runnable: str,
    spec: dict[str, Any],
    command: tuple[str, ...] | list[str],
    args: dict[str, Any],
) -> Invocation:
    """The :class:`Invocation` for running ``runnable`` with ``command`` + ``args``.

    Raises :class:`InvocationError` for an arg that can't be expressed."""
    eff, globals_ = effective_args(spec, command)
    head: list[str] = []
    tail: list[str] = []
    rest: list[str] = []
    env: dict[str, str] = {}

    for key, value in args.items():
        norm = _norm(key)
        declared, aspec = eff.get(norm, (key, {}))
        typ = aspec.get("type", "")
        flag = f"--{declared}"
        out = head if norm in globals_ else tail

        if typ == "password":
            raise InvocationError(f"argument '{key}' is a password — secrets can't go in a schedule's args (they'd sit in the config and crontab); use --cred-vars")
        if isinstance(value, dict):
            raise InvocationError(f"argument '{key}': a table/object can't be passed on the command line")

        if typ == "rest":
            items = value if isinstance(value, list) else [value]
            rest.extend(_scalar(key, v) for v in items)
            continue

        if typ == "flag" or (not typ and isinstance(value, bool)):
            if not isinstance(value, bool):
                raise InvocationError(f"argument '{key}' is a flag — use true or false, not {value!r}")
            if value:
                out.append(flag)
            elif aspec.get("default") is True:
                # argv can't switch a default-true flag off; the env channel can.
                env[env_key(runnable, declared)] = "0"
            continue

        if isinstance(value, list):
            if typ and not aspec.get("multiple"):
                raise InvocationError(f"argument '{key}' takes one value, not a list")
            for item in value:
                if isinstance(item, (list, dict)):
                    raise InvocationError(f"argument '{key}': list items must be plain values")
                out.extend([flag, _scalar(key, item)])
            continue

        out.extend([flag, _scalar(key, value)])

    argv = [*head, *command, *tail]
    if rest:
        argv += ["--", *rest]
    return Invocation(argv=argv, env=env)
