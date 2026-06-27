"""
Core data models for runspec.

Arg     — a single argument with value + full metadata
Group   — a relationship constraint between arguments
RunSpec — the full parsed result for a script
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generic, TypeVar

# Value type carried by an Arg. Generic so a generated stub can pin it precisely
# (`Arg[int]`, `Arg[Literal["json", "yaml"]]`); plain runtime use is `Arg[Any]`.
# Generic is erased at runtime, so this changes nothing about behaviour.
T = TypeVar("T")

# Item-type name → its Python type repr, for `Arg.python_type`. Mirrors the type
# map used by schema emit (cli._arg_to_json_schema) and the planned stub
# generator. `rest` is inherently list-valued; everything else is the scalar
# item type, wrapped in `list[...]` when the arg is `multiple`.
_PYTHON_TYPE_NAMES = {
    "str": "str",
    "password": "str",
    "int": "int",
    "float": "float",
    "bool": "bool",
    "flag": "bool",
    "path": "Path",
    "choice": "str",
    "rest": "list[str]",
}


@dataclass(eq=False)
class Arg(Generic[T]):
    """
    A single parsed argument. Carries its resolved value alongside
    its full spec metadata so the parsed result is self-describing.

    Explicit value access — read ``.value`` to get the coerced native value::

        workers: int = args.workers.value
        if args.dry_run.value: ...
        path = args.out_dir.value / "f.txt"

    Arg is **not** a transparent proxy. It deliberately does not pretend to be
    its value: arithmetic, indexing, iteration, ``int()``/``str()``/``len()``,
    ``os.fspath``, and attribute delegation all raise rather than silently
    operate on the wrapped value, so a forgotten ``.value`` fails loudly at the
    use site instead of producing a subtly wrong result. ``bool()`` and ``==``
    raise for the same reason — those are the two contexts where a missing
    ``.value`` would otherwise be *silently* wrong (every object is truthy;
    identity-equality is always False against a native value).

    (Pre-0.38.0 Arg was a transparent proxy that forwarded ~30 dunders; the
    leak — ``is None`` never matching, ``isinstance`` failing, values that
    won't JSON-serialise — made ``.value`` necessary "sometimes but not
    always". It is now always required.)
    """

    # Resolved value — coerced to native type by the language pack. Typed T so a
    # generated stub can pin it (see TypeVar above); at runtime it is Arg[Any].
    value: T

    # Spec metadata
    name: str
    type: str
    required: bool = False
    default: Any = None
    description: str | None = None
    options: list[Any] | None = None
    range: tuple[Any, Any] | None = None
    pattern: str | None = None  # regex the value must fully match (str only)
    min_length: int | None = None  # minimum string length (str only)
    max_length: int | None = None  # maximum string length (str only)
    multiple: bool = False
    delimiter: str | None = None
    short: str | None = None
    env: str | None = None
    deprecated: str | None = None
    autonomy: str | None = None
    ui: str | None = None
    hint: str | None = None  # UI placeholder/hint text (str/path/password types)
    meta: dict[str, Any] | None = None
    position: int | None = None  # positional index (1-based); None = --flag arg

    # Resolution metadata — where this arg's value originated
    source: str = "not_set"  # "cli" | "env" | "runspec_env" | "spec_default" | "not_set"

    @property
    def python_type(self) -> str:
        """Effective Python type of ``value`` as a string — e.g. ``"list[str]"``.

        ``type`` is the *item* type (it drives per-item coercion); on its own it
        doesn't convey that a ``multiple`` arg's parsed value is a list. This
        composes the two: ``{type="str", multiple=true}`` → ``"list[str]"``,
        ``{type="int"}`` → ``"int"``, ``type="rest"`` → ``"list[str]"``.
        """
        if self.type == "rest":
            return "list[str]"
        base = _PYTHON_TYPE_NAMES.get(self.type, self.type)
        return f"list[{base}]" if self.multiple else base

    # ── Explicit value access ─────────────────────────────────────────────────
    # Read `.value` for the coerced native value. Arg is intentionally *not* a
    # transparent proxy — most value operations are simply absent (so they raise
    # naturally), and the two that would otherwise be *silently* wrong without a
    # `.value` — truthiness and equality — raise a guiding TypeError.

    def __repr__(self) -> str:
        # Show that this is an Arg, not the bare value — a stray `print(args.x)`
        # should look wrong, not masquerade as the value.
        return f"Arg(name={self.name!r}, value={self.value!r})"

    def __bool__(self) -> bool:
        raise TypeError(f"Arg {self.name!r} used in a boolean context — write `args.{self.name.replace('-', '_')}.value` (e.g. `if args.{self.name.replace('-', '_')}.value:`).")

    def __eq__(self, other: object) -> bool:
        raise TypeError(f"Arg {self.name!r} compared with `==` — write `args.{self.name.replace('-', '_')}.value == ...`.")

    def __ne__(self, other: object) -> bool:
        raise TypeError(f"Arg {self.name!r} compared with `!=` — write `args.{self.name.replace('-', '_')}.value != ...`.")


@dataclass
class Group:
    """
    A relationship constraint between arguments.
    Validated in a second pass after individual arg validation.
    """

    name: str
    args: list[str]

    # Group type — exactly one of these will be set
    exclusive: bool = False  # at most one arg from the group
    inclusive: bool = False  # if any, then all
    at_least_one: bool = False  # one or more must be provided
    exactly_one: bool = False  # strictly one must be provided

    # Conditional — if `condition` arg is provided, `args` become required
    condition: str | None = None  # the triggering arg name


@dataclass
class RunSpec:
    """
    The full parsed result for a script invocation.

    Acts as a namespace — args are attributes, accessed as args.quality,
    args.input_dir etc. Hyphens in arg names become underscores.

    Also carries full spec metadata so it can be used as a source of
    truth for emit, describe, scaffold, and other future operations.
    """

    # Script identity
    __runspec_runnable__: str
    __runspec_source__: Path
    __runspec_command_path__: list[str] = field(default_factory=list)  # active subcommand path, deepest last
    __runspec_autonomy__: str = "confirm"  # effective autonomy for this invocation
    __runspec_agent__: bool = False  # True when called via runspec serve (RUNSPEC_AGENT=1)
    __runspec_spec__: dict[str, Any] = field(default_factory=dict)
    __runspec_groups__: list[Group] = field(default_factory=list)

    # Args are stored internally and exposed as attributes
    _args: dict[str, Arg[Any]] = field(default_factory=dict)

    # Values loaded from the .runspec_env file at parse time
    _runspec_env: dict[str, str] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        """Access args as attributes: args.quality, args.input_dir.

        Annotated ``-> Any`` (not ``-> Arg``) on purpose. Args are dynamic —
        their names come from runspec.toml at runtime, so a type checker can't
        know which attributes exist or what each value's type is. Returning
        ``Any`` lets ``args.<name>`` resolve without a checker error; the
        runtime object is an Arg (read ``.value`` for the coerced native value,
        and ``.type``/``.source``/``.meta``/… for metadata) —

            workers = args.workers.value     # int — read .value
            if args.dry_run.value: ...
            out = args.out_dir.value / "f.txt"   # .value is a Path

        Arg is **not** a transparent proxy (since 0.38.0): truthiness and ``==``
        on a bare ``args.<name>`` raise, and value operations are absent. Same
        idiom as argparse.Namespace / SimpleNamespace, whose __getattr__ is also
        typed Any. Precise per-arg static types (so ``args.workers.value`` is
        ``int`` and a typo is flagged) are the job of the planned
        ``emit --stubs`` generator — a PEP 561 ``.pyi`` per package — not this
        annotation; until then a parsed arg is ``Any`` to the checker.
        """
        try:
            args: dict[str, Arg[Any]] = object.__getattribute__(self, "_args")
            return args[name]
        except KeyError as err:
            raise AttributeError(f"No argument '{name}' in spec for '{self.__runspec_runnable__}'. Available: {', '.join(self._args.keys())}") from err

    def __repr__(self) -> str:
        args_repr = ", ".join(f"{k}={v.value!r}" for k, v in self._args.items())
        return f"RunSpec(runnable={self.__runspec_runnable__!r}, {args_repr})"

    def __contains__(self, name: str) -> bool:
        """`"symbol" in args` — True when the arg is in the active command path.

        The effective arg set is scoped to the resolved subcommand path (the
        runnable's globals plus the chosen command's own args), so an arg
        declared only on a *different* subcommand is not present. Hyphens are
        normalised to underscores, matching attribute access.
        """
        return name.replace("-", "_") in self._args

    def get(self, name: str, default: Any = None) -> Arg[Any]:
        """Safe arg lookup — always returns an Arg, never raises.

        Use this for args whose presence depends on the active subcommand, so
        you don't have to guard every access:

            since = args.get("since").value        # None if not in this path
            symbol = args.get("symbol", "ABC123").value

        When the arg is in the active command path its real Arg is returned;
        otherwise a synthetic ``not_set`` Arg wrapping ``default`` is returned,
        so ``args.get(name).value`` is always safe and yields ``default`` for an
        arg that doesn't apply to the current invocation.
        """
        norm = name.replace("-", "_")
        existing = self._args.get(norm)
        if existing is not None:
            return existing
        return Arg(value=default, name=norm, type="str", source="not_set")

    def __dir__(self) -> list[str]:
        """Include the active args so they show up in dir()/REPL completion."""
        return [*super().__dir__(), *self._args.keys()]

    # ── Public metadata accessors ─────────────────────────────────────────────
    # The dunder fields (__runspec_*__) are the storage; these properties are
    # the developer API. Use `args.runspec_agent` not `args.__runspec_agent__`.

    @property
    def runspec_runnable(self) -> str:
        """Name of the runnable being invoked (e.g. the [clean] section in runspec.toml)."""
        return self.__runspec_runnable__

    @property
    def runspec_source(self) -> Path:
        """Path to the runspec.toml that defined this runnable."""
        return self.__runspec_source__

    @property
    def runspec_prefix(self) -> Path:
        """Package root: the directory containing runspec.toml."""
        return self.__runspec_source__.parent

    @property
    def runspec_autonomy(self) -> str:
        """Effective autonomy for this invocation (after escalation)."""
        return self.__runspec_autonomy__

    @property
    def runspec_agent(self) -> bool:
        """True when called by an agent (RUNSPEC_AGENT=1 in the environment)."""
        return self.__runspec_agent__

    @property
    def runspec_spec(self) -> dict[str, Any]:
        """The raw, fully-inferred spec dict for the runnable."""
        return self.__runspec_spec__

    @property
    def runspec_groups(self) -> list[Group]:
        """Group constraints declared on this runnable."""
        return self.__runspec_groups__

    @property
    def runspec_discoverable(self) -> bool:
        """Whether this runnable is discoverable (listed by `runspec serve`/`local`).

        Normalised from the `discoverable` keyword: absent/`true` → ``True``,
        `false` → ``False`` (an internal helper, hidden from discovery surfaces).
        """
        from runspec.discovery import normalise_discoverable

        return normalise_discoverable(self.__runspec_spec__.get("discoverable"))

    @property
    def runspec_command(self) -> str | None:
        """Active subcommand name (leaf), or None if no subcommand was invoked."""
        path = self.__runspec_command_path__
        return path[-1] if path else None

    @property
    def runspec_command_path(self) -> list[str]:
        """Full subcommand path as a list, deepest last. Empty if no subcommand."""
        return self.__runspec_command_path__

    def get_runspec_env(self) -> SimpleNamespace:
        """Return the contents of the .runspec_env file as a SimpleNamespace.

        Keys are lowercased: MY_API_KEY becomes ns.my_api_key.
        Returns an empty namespace if no file was found at parse time.
        """
        from runspec.env import make_env_namespace

        return make_env_namespace(self._runspec_env)

    def _set_arg(self, name: str, arg: Arg[Any]) -> None:
        """Internal: store an arg. Normalises hyphens to underscores."""
        normalised = name.replace("-", "_")
        self._args[normalised] = arg
