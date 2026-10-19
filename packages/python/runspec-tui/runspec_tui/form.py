"""form.py — the run-form rules, with no terminal code.

Ported from the console's Forms view (``console-ui/src/views/formsShared.ts``:
``partitionArgs``, ``argNeedsValue``, ``isMultiChoice``, ``initialFormValues``,
``offerDebugSwitch``) so the TUI lays out and checks a form the way the console
does; ``tests/test_form.py`` pins the shared cases. Validation goes further than
the console's preflight: each value runs through core's own coercer (type,
range, choices, pattern, length) and the node's groups through core's
``validate_groups``, so a bad value is caught on its field before the run.

Form values are what the widgets hold: ``bool`` for a flag, ``list[str]`` for a
multi-select or a multi-value field, ``str`` for everything else (``""`` =
empty, ``None`` = an unset single choice).
"""

from __future__ import annotations

import datetime as _dt
import re
import shlex
from collections.abc import Mapping
from typing import Any

from runspec.types import coerce
from runspec.validator import validate_groups

DEBUG_ARG = "debug"
_VAR_REF = re.compile(r"^\$[A-Z_][A-Z0-9_]*$")
_UI_KINDS = ("date", "datetime", "time", "month", "color")


# ── layout ──────────────────────────────────────────────────────────────────


def is_var_ref(value: Any) -> bool:
    return isinstance(value, str) and bool(_VAR_REF.match(value))


def has_default(arg: Mapping[str, Any]) -> bool:
    return arg.get("default") is not None


def partition_args(args: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(visible, defaulted)``: only args that carry a default fold under
    "Default arguments"; required and optional-no-default args stay visible."""
    return [a for a in args if not has_default(a)], [a for a in args if has_default(a)]


def split_inherited(args: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(inherited, own)`` — globals/parent-command args render in their own
    section above the command's own."""
    return [a for a in args if a.get("inherited")], [a for a in args if not a.get("inherited")]


def is_multi_choice(arg: Mapping[str, Any]) -> bool:
    return arg.get("type") == "choice" and bool(arg.get("options")) and (arg.get("multiple") is True or arg.get("ui") == "multiselect")


def widget_kind(arg: Mapping[str, Any]) -> str:
    """Which control renders ``arg``."""
    typ, ui = arg.get("type") or "str", arg.get("ui")
    if typ == "flag":
        return "flag"
    if typ == "choice" and arg.get("options"):
        return "multichoice" if is_multi_choice(arg) else "choice"
    if typ == "password":
        return "password"
    if typ == "rest":
        return "rest"
    if arg.get("multiple"):
        return "lines"
    if typ in ("int", "float"):
        return "number"
    if typ == "path":
        return "directory" if ui == "directory" else "path"
    if ui in _UI_KINDS:
        return str(ui)
    return "text"


def type_label(arg: Mapping[str, Any]) -> str:
    """The small type tag beside an arg's name (as the console shows it)."""
    if arg.get("type") == "path" and arg.get("ui") == "directory":
        return "folder"
    if arg.get("ui") in _UI_KINDS:
        return str(arg["ui"])
    return str(arg.get("type") or "str")


def placeholder(arg: Mapping[str, Any]) -> str:
    if arg.get("hint"):
        return str(arg["hint"])
    if is_var_ref(arg.get("default")):
        return str(arg["default"])
    kind = widget_kind(arg)
    if kind == "number" and arg.get("range"):
        lo, hi = arg["range"]
        return f"{lo} – {hi}"
    return {
        "password": "secret — sent as an env var, never on the command line",
        "path": "path — Tab completes, F2 browses",
        "directory": "folder — Tab completes, F2 browses",
        "date": "YYYY-MM-DD",
        "datetime": "YYYY-MM-DD HH:MM (local time)",
        "time": "HH:MM:SS",
        "month": "YYYY-MM",
        "color": "#rrggbb",
        "rest": "passed through after --",
        "lines": "one value per line",
    }.get(kind, "")


def offer_debug(args: list[dict[str, Any]]) -> bool:
    """The Debug switch is offered unless the runnable declares its own ``debug``."""
    return DEBUG_ARG not in {a["name"] for a in args}


# ── values ──────────────────────────────────────────────────────────────────


def initial_value(arg: Mapping[str, Any], prefill: Mapping[str, Any] | None = None) -> Any:
    """The control's starting value: a previous run's value (never a secret),
    else the spec default (``$VAR`` refs left blank to show as placeholders)."""
    name, kind = arg["name"], widget_kind(arg)
    if prefill and name in prefill and kind != "password":
        return _as_form_value(kind, prefill[name])
    default = arg.get("default")
    if default is None or is_var_ref(default):
        return _as_form_value(kind, None)
    return _as_form_value(kind, default)


def _as_form_value(kind: str, value: Any) -> Any:
    if kind == "flag":
        return bool(value)
    if kind in ("multichoice", "lines"):
        if value is None:
            return []
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]
    if kind == "choice":
        return None if value is None else str(value)
    if kind == "rest":
        return shlex.join(str(v) for v in value) if isinstance(value, list) else ("" if value is None else str(value))
    return "" if value is None else str(value)


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value is False


def to_raw(arg: Mapping[str, Any], value: Any) -> Any:
    """The form value as the raw value core's parser would hold for it
    (``None`` = not given). Raises ``ValueError`` for an unparseable
    date/time or pass-through string."""
    kind = widget_kind(arg)
    if kind == "flag":
        return True if value else None
    if value is None:
        return None
    if kind == "multichoice":
        return list(value) or None
    if kind == "lines":
        if isinstance(value, str):
            value = value.splitlines()
        items = [str(v).strip() for v in value if str(v).strip()]
        return items or None
    if kind == "rest":
        if not isinstance(value, str):
            return [str(v) for v in value] or None
        try:
            return shlex.split(value) or None
        except ValueError as exc:  # an unclosed quote
            raise ValueError(f"can't split into arguments: {exc}") from None
    text = str(value).strip() if kind != "password" else str(value)
    if text == "":
        return None
    if kind in ("date", "datetime", "time", "month"):
        return normalise_ui_value(kind, text)
    if kind == "color":
        if not re.fullmatch(r"#?[0-9A-Fa-f]{6}", text):
            raise ValueError("expected a colour like #1677ff")
        return "#" + text.lstrip("#").lower()
    return text


def normalise_ui_value(kind: str, text: str) -> str:
    """The wire string for a date/time control, matching what the console's
    pickers send: ``YYYY-MM-DD``, ``YYYY-MM``, ``HH:MM:SS``, or for ``datetime``
    a UTC ISO timestamp with ``Z`` (local time is assumed when no offset is
    typed)."""
    try:
        if kind == "date":
            return _dt.date.fromisoformat(text).isoformat()
        if kind == "month":
            return _dt.date.fromisoformat(f"{text}-01").strftime("%Y-%m")
        if kind == "time":
            parts = text.split(":")
            if len(parts) == 2:
                text += ":00"
            return _dt.time.fromisoformat(text).strftime("%H:%M:%S")
        moment = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"expected {placeholder({'ui': kind})}") from None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    utc = moment.astimezone(_dt.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


# ── environment ─────────────────────────────────────────────────────────────


def auto_env_name(runnable: str, arg: str) -> str:
    """``RUNSPEC_<RUNNABLE>_ARG_<NAME>`` — the top-level runnable name even
    under a command, as core's ``_apply_env`` reads it."""
    return f"RUNSPEC_{runnable.upper().replace('-', '_')}_ARG_{arg.upper().replace('-', '_')}"


def env_source(arg: Mapping[str, Any], runnable: str, env_file: Mapping[str, str], environ: Mapping[str, str]) -> str | None:
    """Where a field left blank would get its value from, or ``None``: the
    ``.runspec_env`` file, or a variable in the environment the run inherits
    (pass ``environ={}`` when the run escalates — sudo drops it)."""
    names = [auto_env_name(runnable, arg["name"]), *(arg.get("env") or [])]
    for key in names:
        if key in environ:
            return f"${key}"
        if key in env_file:
            return ".runspec_env"
    return None


def same_as_default(arg: Mapping[str, Any], raw: Any) -> bool:
    """Whether a raw value is just the arg's declared default (compared after
    coercion, so ``"20"`` matches ``20`` and ``./x`` matches ``x``)."""
    default = arg.get("default")
    if default is None or is_var_ref(default):
        return False
    if raw == default:
        return True
    try:
        return bool(coerce(raw, dict(arg)) == coerce(default, dict(arg)))
    except (TypeError, ValueError):
        return False


# ── validation ──────────────────────────────────────────────────────────────


def _tidy(message: str) -> str:
    """Core's coercion message without the ``Cannot coerce …:`` lead-in and the
    ✗/indentation it uses for terminal output."""
    message = re.sub(r"^Cannot coerce value .*? for argument '[^']*': ", "", message, flags=re.S)
    lines = [ln.strip().lstrip("✗").strip() for ln in message.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not ln.startswith("Got:")]
    if len(lines) > 1 and lines[0].startswith("Invalid value for"):
        lines = lines[1:]
    return " · ".join(lines)


def field_error(arg: Mapping[str, Any], value: Any, env_supplied: bool = False) -> str | None:
    """The error to show under ``arg``'s field, or ``None``."""
    try:
        raw = to_raw(arg, value)
    except ValueError as exc:
        return str(exc)
    if raw is None:
        if arg.get("required") and not env_supplied:
            return "Required"
        return None
    if widget_kind(arg) in ("date", "datetime", "time", "month", "color"):
        return None  # already normalised; the arg's type is str
    try:
        coerce(raw, dict(arg))
    except TypeError:
        return None  # a custom type registered by the runnable's own code — it checks it
    except ValueError as exc:
        message = _tidy(str(exc))
        if "invalid literal" in message or "could not convert" in message:
            return "expected a whole number" if arg.get("type") == "int" else "expected a number"
        return message
    return None


def validate(
    args: list[dict[str, Any]],
    groups: Mapping[str, Any],
    values: Mapping[str, Any],
    env_supplied: set[str] | frozenset[str] = frozenset(),
) -> tuple[dict[str, str], list[str]]:
    """``(field errors by arg name, group errors)`` for the form at one node.

    Groups constrain what was explicitly provided (typed, or set in the
    environment), not what defaulting filled in — as core checks them."""
    errors: dict[str, str] = {}
    provided: dict[str, Any] = {}
    for arg in args:
        name = arg["name"]
        value = values.get(name)
        err = field_error(arg, value, name in env_supplied)
        if err:
            errors[name] = err
            continue
        raw = to_raw(arg, value)
        if raw is not None and not same_as_default(arg, raw):
            provided[name.replace("-", "_")] = raw
        elif name in env_supplied:
            provided[name.replace("-", "_")] = True
    group_errors = [] if errors else [_tidy(e) for e in validate_groups(provided, dict(groups))]
    return errors, group_errors


# ── the command ─────────────────────────────────────────────────────────────


def build_command(
    runnable: str,
    path: tuple[str, ...] | list[str],
    args: list[dict[str, Any]],
    values: Mapping[str, Any],
    debug: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """``(argv, env)`` for ``runspec exec``-style execution of the node at
    ``path``: the command path, then ``--name value`` for every value that
    differs from its default (so ``.runspec_env`` and env vars still apply to
    the rest, exactly as when typed on a command line), then ``--`` and any
    pass-through args. Secrets and the Debug switch ride the env channel
    (``RUNSPEC_<RUNNABLE>_ARG_*``), never argv."""
    argv: list[str] = list(path)
    env: dict[str, str] = {}
    rest: list[str] = []
    for arg in args:
        name = arg["name"]
        raw = to_raw(arg, values.get(name))
        if raw is None or same_as_default(arg, raw):
            continue
        kind = widget_kind(arg)
        if kind == "password":
            env[auto_env_name(runnable, name)] = str(raw)
        elif kind == "rest":
            rest = [str(v) for v in raw]
        elif kind == "flag":
            argv.append(f"--{name}")
        else:
            for item in raw if isinstance(raw, list) else [raw]:
                argv += [f"--{name}", str(item)]
    if rest:
        argv += ["--", *rest]
    if debug and offer_debug(args):
        env[auto_env_name(runnable, DEBUG_ARG)] = "1"
    return argv, env


def display_command(runnable: str, argv: list[str], env: Mapping[str, str]) -> str:
    """The command as a person would type it, env first; a secret's value shows
    as ``***``."""
    shown = [f"{k}={'1' if k.endswith('_ARG_DEBUG') else '***'}" for k in env]
    return " ".join([*shown, shlex.join([runnable, *argv])])
