"""
runspec.testing — test helpers for runspec runnables.

Stdlib-only and free of any test-framework dependency, so it is safe to import
anywhere (it does not pull in pytest). It is *not* imported by ``runspec`` at
package load — import it explicitly from your tests::

    from runspec.testing import ParseHarness

The core idea: ``runspec.parse()`` already takes ``argv=`` and ``config_path=``,
so testing a runnable never requires monkeypatching ``sys.argv`` or mocking the
parser. This module removes the remaining boilerplate — pointing ``parse()`` at
a spec, asserting on clean error exits without pytest's ``capsys``, and walking
the subcommand tree to auto-cover every command.

Two layers worth testing, and what this helps with:

1. **Wiring / parsing** (no mocks): does each subcommand accept its args, coerce
   types, enforce required args and ``require-command``? ``ParseHarness`` plus
   ``smoke()`` cover this for a whole command tree in a few lines.

2. **The actual work** (mock only the I/O boundary): keep ``main()`` thin —
   ``parse() -> call a plain function -> print`` — then test that plain function
   directly and patch its single external call. ``ParseHarness`` builds the
   parsed ``RunSpec`` you can hand to it; the mocking is ordinary
   ``unittest.mock``.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import runspec
from runspec.inference import infer_script
from runspec.loader import load_raw

__all__ = ["ParseHarness", "harness", "no_run_as_check"]


class ParseHarness:
    """Drive ``runspec.parse()`` for a single runnable in tests.

    Point it at a spec one of two ways:

    * ``config_path=`` — an existing ``runspec.toml`` (e.g. a fixture, or your
      package's real spec).
    * ``toml=`` — an inline TOML string written to a throwaway temp file.

    Then call :meth:`parse` / :meth:`expect_ok` / :meth:`expect_exit` with a
    CLI-style argv list, or :meth:`smoke` to auto-cover the whole command tree.

    Usable as a context manager so the temp file (when ``toml=`` is used) is
    cleaned up::

        with ParseHarness(script_name="greet", toml=SPEC) as h:
            spec = h.expect_ok(["--name", "Ada"])
            assert str(spec.name) == "Ada"
    """

    def __init__(
        self,
        *,
        script_name: str,
        config_path: str | Path | None = None,
        toml: str | None = None,
        suppress_summary: bool = True,
    ) -> None:
        if (config_path is None) == (toml is None):
            raise ValueError("pass exactly one of config_path= or toml=")

        self.script_name = script_name
        self._suppress_summary = suppress_summary
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None

        if toml is not None:
            self._tmpdir = tempfile.TemporaryDirectory(prefix="runspec-test-")
            config_path = Path(self._tmpdir.name) / "runspec.toml"
            config_path.write_text(toml, encoding="utf-8")

        self.config_path = Path(config_path)  # type: ignore[arg-type]
        try:
            self._raw = load_raw(self.config_path)
            if script_name not in self._raw["runnables"]:
                have = ", ".join(sorted(self._raw["runnables"]))
                raise KeyError(f"runnable {script_name!r} not in {self.config_path} (have: {have})")
            # Apply the same inference parse() does (fills type/required, recurses
            # into subcommands) so the harness's view of the spec — required_args,
            # the smoke valid-argv builder, choice options — matches a real parse.
            autonomy_default = (self._raw.get("config") or {}).get("autonomy_default") or "confirm"
            self._raw["runnables"][script_name] = infer_script(self._raw["runnables"][script_name], autonomy_default)
        except BaseException:
            # Tear the temp dir down synchronously so a failed construction does
            # not leave a TemporaryDirectory finalizer to fire later during GC.
            self.close()
            raise

    # ── lifecycle ───────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    def __enter__(self) -> ParseHarness:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── core parse entry points ──────────────────────────────────────────────

    def parse(self, argv: Iterable[str]) -> runspec.RunSpec:
        """Parse ``argv`` and return the ``RunSpec``.

        Behaves like a real invocation for everything wiring-related: on a bad
        spec/argv it prints the human error and raises ``SystemExit``. Use
        :meth:`expect_ok` / :meth:`expect_exit` for assertions.

        The ``run_as`` identity gate (``enforce_run_as``) is **not** applied —
        like secret prompting, it is an execution-time concern, not a wiring
        one. A test asserting that a runnable's args parse must not depend on
        *who* runs the suite, so a runnable that declares ``run_as = "svc"``
        stays testable as your own user. (Exercise the gate itself by calling
        ``runspec.parse()`` directly, as ``test_run_as_enforce.py`` does.)
        """
        with self._env():
            return runspec.parse(
                self.script_name,
                argv=list(argv),
                config_path=self.config_path,
                _prompt_secrets=False,
                _check_run_as=False,
                _implicit_logging=False,
            )

    def expect_ok(self, argv: Iterable[str]) -> runspec.RunSpec:
        """Assert ``argv`` parses cleanly; return the ``RunSpec``."""
        argv = list(argv)
        out = io.StringIO()
        with self._env(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                return runspec.parse(
                    self.script_name,
                    argv=argv,
                    config_path=self.config_path,
                    _prompt_secrets=False,
                    _check_run_as=False,
                    _implicit_logging=False,
                )
            except SystemExit as e:
                raise AssertionError(f"expected `{self.script_name} {' '.join(argv)}` to parse, but it exited {e.code}:\n{out.getvalue()}") from None

    def expect_exit(
        self,
        argv: Iterable[str],
        *,
        code: int | None = 1,
        contains: str | Iterable[str] | None = None,
    ) -> str:
        """Assert ``argv`` exits (default ``code=1``); return captured output.

        ``contains`` asserts the captured stdout+stderr includes the given
        substring(s) — handy for checking the offending flag is named in the
        error. Pass ``code=None`` to accept any non-zero exit, or ``code=0`` for
        ``--help``.
        """
        argv = list(argv)
        out = io.StringIO()
        with self._env(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                runspec.parse(
                    self.script_name,
                    argv=argv,
                    config_path=self.config_path,
                    _prompt_secrets=False,
                    _check_run_as=False,
                    _implicit_logging=False,
                )
            except SystemExit as e:
                text = out.getvalue()
                if code is not None and e.code != code:
                    raise AssertionError(f"`{self.script_name} {' '.join(argv)}`: expected exit {code}, got {e.code}:\n{text}") from None
                if contains is not None:
                    needles = [contains] if isinstance(contains, str) else list(contains)
                    for needle in needles:
                        if needle not in text:
                            raise AssertionError(f"`{self.script_name} {' '.join(argv)}`: {needle!r} not found in output:\n{text}") from None
                return text
        raise AssertionError(f"expected `{self.script_name} {' '.join(argv)}` to exit, but it parsed cleanly")

    # ── spec introspection ────────────────────────────────────────────────────

    def command_paths(self) -> list[list[str]]:
        """Every subcommand path in the tree, deepest last within each branch.

        ``[["show"], ["create"], ["multi"], ["multi", "create"]]`` for a tree
        with a nested ``multi`` group. Empty list for a runnable with no
        subcommands.
        """
        paths: list[list[str]] = []

        def walk(node: dict[str, Any], prefix: list[str]) -> None:
            for name, sub in (node.get("commands") or {}).items():
                here = [*prefix, name]
                paths.append(here)
                walk(sub, here)

        walk(self._runnable(), [])
        return paths

    def leaf_command_paths(self) -> list[list[str]]:
        """Command paths that have no further subcommands."""
        return [p for p in self.command_paths() if not (self._node(p).get("commands"))]

    def args_for(self, path: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
        """The arg spec dict declared at a command path (`()` = the runnable)."""
        return self._node(list(path)).get("args") or {}

    def required_args(self, path: Iterable[str] = ()) -> list[str]:
        """Names of required args declared at a command path."""
        return [name for name, a in self.args_for(path).items() if a.get("required")]

    def requires_command(self, path: Iterable[str] = ()) -> bool:
        """Whether the node at ``path`` sets ``require-command``."""
        return bool(self._node(list(path)).get("require_command"))

    # ── auto coverage ─────────────────────────────────────────────────────────

    def smoke(
        self,
        *,
        values: dict[str, Any] | None = None,
        check_help: bool = True,
    ) -> list[str]:
        """Auto-cover the whole command tree without per-command boilerplate.

        For the runnable and every subcommand it asserts:

        * ``--help`` exits 0 (proves the command is wired and discoverable);
        * ``require-command`` is enforced where declared;
        * omitting required args exits 1 (proves validation is active);
        * if ``values`` is given, a fully-populated invocation parses cleanly.

        ``values`` maps arg names to string values used to build valid
        invocations — one dict covers every command, since args reused across
        subcommands (e.g. ``symbols``) share a name. Flag args take a truthy
        value to be included. Args missing from ``values`` are simply omitted,
        so a leaf with an unsupplied *required* arg skips its valid-invocation
        check (its missing-required check still runs).

        Returns a list of human-readable labels for the checks performed — assert
        on its length if you want to guard against accidentally checking nothing.
        """
        values = values or {}
        done: list[str] = []

        if check_help:
            self.expect_exit(["--help"], code=0)
            done.append("--help")
        if self.requires_command():
            self.expect_exit([])
            done.append("(no command) -> require-command")

        for path in self.command_paths():
            label = " ".join(path)
            node = self._node(path)

            if check_help:
                self.expect_exit([*path, "--help"], code=0)
                done.append(f"{label} --help")

            if node.get("require_command"):
                self.expect_exit(list(path))
                done.append(f"{label} -> require-command")
                continue
            if node.get("commands"):
                continue  # intermediate group without require-command

            if self.required_args(path):
                self.expect_exit(list(path))
                done.append(f"{label} -> missing required")

            built = self._build_valid_argv(path, values)
            if built is not None:
                self.expect_ok([*path, *built])
                done.append(f"{label} (valid)")

        return done

    # ── internals ─────────────────────────────────────────────────────────────

    def _runnable(self) -> dict[str, Any]:
        return dict(self._raw["runnables"][self.script_name])

    def _node(self, path: list[str]) -> dict[str, Any]:
        node = self._runnable()
        for part in path:
            node = (node.get("commands") or {})[part]
        return dict(node)

    def _merged_args(self, path: list[str]) -> dict[str, dict[str, Any]]:
        """Args visible at a leaf: runnable-level plus each node along the path."""
        merged: dict[str, dict[str, Any]] = dict(self.args_for(()))
        acc: list[str] = []
        for part in path:
            acc.append(part)
            merged.update(self.args_for(acc))
        return merged

    def _merged_groups(self, path: list[str]) -> dict[str, dict[str, Any]]:
        """Groups visible at a leaf: runnable-level plus each node along the path."""
        node = self._runnable()
        merged: dict[str, dict[str, Any]] = dict(node.get("groups") or {})
        for part in path:
            node = (node.get("commands") or {})[part]
            merged.update(node.get("groups") or {})
        return merged

    def _build_valid_argv(self, path: list[str], values: dict[str, Any]) -> list[str] | None:
        """Build argv covering every required arg from ``values``; None if a leaf
        can't be satisfied (a required non-choice arg has no value, or a group
        constraint needs a member that can't be fabricated).

        Group-aware across every constraint the group validator enforces:

        * ``exclusive`` / ``exactly-one`` (*capping*): at most one member is
          included even when ``values`` supplies several — so a populated
          ``values`` map doesn't build a mutually-exclusive invocation the group
          would (correctly) reject. A required member is preferred when choosing
          which one to keep. An ``exactly-one`` group with no supplied member
          can't be built, so the leaf is skipped (None).
        * ``at-least-one`` / ``inclusive`` / ``if``-``requires`` (*presence*): a
          member the constraint needs is added even when ``values`` supplies none
          — a fabricatable one (a flag, or a choice filled from its options). An
          ``at-least-one`` group with only non-fabricatable members (non-choice,
          non-flag args with no value) can't be built cleanly, so the leaf is
          skipped (None) rather than producing an invocation the group rejects.

        Choice-aware: ``values`` is one flat map keyed by arg name, but the same
        arg name can declare *different* ``options`` on different subcommands. For
        a ``choice`` arg the value used is validated against *this* leaf's
        options — a shared value that isn't valid here falls back to this leaf's
        first option, and a required choice with no supplied value is filled from
        its options. So one value (or none) for a repeated choice arg works across
        the whole tree.
        """
        merged = self._merged_args(path)
        groups = list(self._merged_groups(path).values())

        # Phase 1 — capping groups: pick members to drop so a fully-populated
        # `values` map can't build an invocation an exclusive/exactly-one group
        # rejects. An exactly-one group with no supplied member can't be built.
        drop: set[str] = set()
        for group in groups:
            if not (group.get("exclusive") or group.get("exactly_one")):
                continue
            supplied = [m for m in (group.get("args") or []) if values.get(m) is not None]
            if group.get("exactly_one") and not supplied:
                return None  # can't construct a valid call for this leaf
            if len(supplied) > 1:
                # Keep a required member if there is one; drop the rest.
                supplied.sort(key=lambda m: not (merged.get(m) or {}).get("required"))
                drop.update(supplied[1:])

        # Phase 2 — build argv from supplied values plus required-arg fills.
        argv: list[str] = []
        included: set[str] = set()
        for name, a in merged.items():
            if name in drop:
                continue
            frag = self._arg_fragment(name, a, values.get(name))
            if frag is None:
                return None  # required non-choice with no value -> skip this leaf
            if frag:
                argv += frag
                included.add(name)

        # Phase 3 — presence groups: add fabricatable members so the built argv
        # satisfies each constraint (or skip the leaf if it can't be).
        for group in groups:
            fill = self._presence_fill(group, merged, included, drop)
            if fill is None:
                return None
            argv += fill
        return argv

    def _arg_fragment(self, name: str, spec: dict[str, Any], value: Any, *, force: bool = False) -> list[str] | None:
        """The argv fragment for one arg, or a signal for omit / can't-build.

        Returns the ``["--flag", value]`` (or ``["--flag"]``) fragment; ``[]``
        when the arg should be omitted (optional, unsupplied, not forced); or
        ``None`` when it must appear but can't be fabricated (a required/forced
        non-choice, non-flag arg with no value — the caller skips the leaf).

        Choice-aware: a supplied value invalid for *this* leaf's options is
        replaced with the leaf's first option, and a needed choice with no value
        is filled from its options.
        """
        options = spec.get("options")
        is_flag = spec.get("type") == "flag"
        if value is None:
            if not (force or spec.get("required")):
                return []  # optional + unsupplied -> omit
            if is_flag:
                value = True
            elif options:
                value = options[0]  # fill a needed choice from its own options
            else:
                return None  # non-choice with no value -> can't fabricate
        elif options and value not in options:
            value = options[0]  # shared value invalid for THIS leaf -> a valid option

        flag = "--" + name  # runspec keys the flag by the declared arg name
        if is_flag:
            return [flag] if value else []
        return [flag, str(value)]

    def _presence_fill(
        self,
        group: dict[str, Any],
        merged: dict[str, dict[str, Any]],
        included: set[str],
        drop: set[str],
    ) -> list[str] | None:
        """Extra argv fragments so a presence group is satisfied by the built
        invocation, or ``None`` when a needed member can't be fabricated.

        * ``at-least-one`` — if no member is included yet, add one fabricatable
          member.
        * ``inclusive`` (all-or-none) — if any member is included, add every
          other member; none included is already valid.
        * ``if`` + ``requires`` — if the condition arg is included, add every
          required arg not already present.

        Returns ``[]`` when nothing needs adding. ``included`` is updated in
        place so members added for one group count toward a later one.
        """
        members = group.get("args") or []

        def fabricate(names: list[str]) -> list[str] | None:
            frags: list[str] = []
            for m in names:
                if m in drop:
                    return None  # a capping group already excluded this member
                frag = self._arg_fragment(m, merged.get(m) or {}, None, force=True)
                if frag is None:
                    return None
                frags += frag
                included.add(m)
            return frags

        if group.get("at_least_one"):
            if any(m in included for m in members):
                return []
            for m in members:
                frag = fabricate([m])
                if frag is not None:
                    return frag
            return None
        if group.get("inclusive"):
            if not any(m in included for m in members):
                return []  # all-or-none: none supplied is valid
            return fabricate([m for m in members if m not in included])
        if group.get("condition"):
            if group["condition"] not in included:
                return []
            return fabricate([m for m in (group.get("requires") or []) if m not in included])
        return []

    @contextlib.contextmanager
    def _env(self) -> Iterator[None]:
        """Context wrapper around a parse.

        The ``suppress_summary`` knob is retained for back-compat but is now a
        no-op: since the run-summary terminal echo is opt-in (``summary_console``,
        default off), a parse never leaks the closing line into test output, and
        the audit record is written to the log file only — never the console.
        """
        yield


@contextlib.contextmanager
def no_run_as_check() -> Iterator[None]:
    """Disable the ``enforce_run_as`` identity gate for the duration of the block.

    :class:`ParseHarness` already skips the gate, but only for parses it makes
    itself. A test that drives a runnable's ``main()`` directly is different:
    ``main()`` calls ``runspec.parse()`` *itself*, which is the real parse, so
    the gate fires there and a ``run_as`` runnable exits non-zero on a
    developer's machine. Wrap the ``main()`` call to skip it::

        from runspec.testing import no_run_as_check

        with no_run_as_check():
            exit_code = my_pkg.main()

    Or apply it to a whole module via an autouse fixture in ``conftest.py``::

        @pytest.fixture(autouse=True)
        def _no_run_as():
            with no_run_as_check():
                yield

    This lives in ``runspec.testing`` (a test-only import) and patches the
    parser's view of the identity check, so no production code path can bypass
    the gate — only code run inside the ``with`` block. Everything else about
    the parse (arg validation, coercion, ``require-command``) is unchanged. To
    *run* (not test) a ``run_as`` runnable by hand, set ``enforce_run_as =
    "warn"`` / ``"off"`` in the spec instead.
    """
    from runspec import parser

    # parser binds `verify_run_as_identity` as its own module global (imported
    # from runspec.become) and calls it by that name, so patching parser's
    # namespace is what the gate actually reads. Going through __dict__ keeps the
    # rebinding off the typed attribute surface.
    ns = parser.__dict__
    original = ns["verify_run_as_identity"]
    ns["verify_run_as_identity"] = lambda _run_as_user: None
    try:
        yield
    finally:
        ns["verify_run_as_identity"] = original


def harness(
    script_name: str,
    *,
    config_path: str | Path | None = None,
    toml: str | None = None,
    suppress_summary: bool = True,
) -> ParseHarness:
    """Convenience factory mirroring :class:`ParseHarness`."""
    return ParseHarness(
        script_name=script_name,
        config_path=config_path,
        toml=toml,
        suppress_summary=suppress_summary,
    )
