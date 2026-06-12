# Testing runnables

runspec is designed to make runnables easy to test. `runspec.parse()` already
takes `argv=` and `config_path=`, so you never have to monkeypatch `sys.argv`
or mock the parser — you just hand it a list of arguments. The `runspec.testing`
module builds on that to remove the remaining boilerplate.

> `runspec.testing` is stdlib-only and pulls in **no** test framework, so it is
> safe to import anywhere. Import it explicitly from your tests:
> `from runspec.testing import ParseHarness`.

## Two layers worth testing

A runnable has two distinct things to test, and conflating them is what makes
mocking feel hard:

1. **Wiring / parsing** — does each subcommand accept its args, coerce types,
   and enforce required args and `require-command`? This needs **no mocks at
   all**.
2. **The actual work** — the network call, the subprocess, the file write. This
   is where mocking belongs, and the trick is to **mock only the I/O boundary**.

The single most useful thing you can do for testability is keep `main()` thin:

```python
import json
import runspec as rs

def create_symbols(region, symbols, *, price=None):   # plain function, plain values
    return _store_call(region, "create", symbols, price=price)   # the one I/O call

def main():
    spec = rs.parse("static-data")
    print(json.dumps(create_symbols(spec.region.value, spec.symbols.value, price=spec.price.value)))
```

With the work in a plain function, you test it directly with ordinary Python
values and patch its single external call — no spec, no argv, no parser in the
way.

## Layer 1 — parsing, with `ParseHarness`

Point the harness at your package's real `runspec.toml` (or a fixture), then
assert on parses and on clean error exits:

```python
from pathlib import Path
from runspec.testing import ParseHarness

SPEC = Path(__file__).parent / "fixtures" / "static_data.toml"

def test_create_parses():
    h = ParseHarness(script_name="static-data", config_path=SPEC)
    spec = h.expect_ok(["create", "-s", "AAPL,MSFT", "--region", "americas", "--env", "prod"])
    assert spec.runspec_command == "create"
    assert spec.symbols.value == ["AAPL", "MSFT"]   # multiple + delimiter -> list

def test_bad_choice_is_rejected():
    h = ParseHarness(script_name="static-data", config_path=SPEC)
    h.expect_exit(["create", "-s", "AAPL", "--region", "mars", "--env", "prod"],
                  contains="--region")
```

- `expect_ok(argv)` asserts the parse succeeds and returns the `RunSpec`.
- `expect_exit(argv, code=1, contains=...)` asserts a clean error exit and
  returns the captured stdout+stderr — no pytest `capsys` needed. Use `code=0`
  for `--help`.

For an inline spec (handy for small cases), pass `toml=` and use it as a context
manager so the temp file is cleaned up:

```python
from runspec.testing import ParseHarness

SPEC = """
[greet]
[greet.args]
name = {type = "str", required = true}
times = {default = 1}
"""

def test_inline():
    with ParseHarness(script_name="greet", toml=SPEC) as h:
        spec = h.expect_ok(["--name", "Ada", "--times", "3"])
        assert spec.times.value == 3
        h.expect_exit([], contains="name")   # missing required
```

## One call to cover a whole command tree: `smoke()`

For a big migrated runnable with many subcommands, `smoke()` walks the entire
tree and asserts that `--help` works for every command, `require-command` is
enforced, and every command with required args rejects an empty invocation.
Pass `values=` — a map of arg names to values, shared across subcommands — and
each leaf is also exercised with a full, valid invocation:

```python
def test_smoke():
    h = ParseHarness(script_name="static-data", config_path=SPEC)
    performed = h.smoke(values={
        "region": "americas", "env": "prod", "symbols": "AAPL",
        "price": "12.5", "volume": "100", "ipo_date": "20240101",
    })
    assert len(performed) > 20            # guard against checking nothing
```

`smoke()` returns a list of labels for the checks it performed, so you can
assert it actually exercised something. Args missing from `values` are simply
omitted — a leaf with an unsupplied *required* arg skips only its valid-invocation
check; its missing-required check still runs.

Spec introspection is available directly too: `command_paths()`,
`required_args(path)`, `requires_command(path)`, and `args_for(path)`.

## Layer 2 — the work, mocking only the boundary

Test the plain logic function directly and patch its single external call. The
harness gives you a parsed `RunSpec` to feed it when you want a realistic input:

```python
from unittest.mock import patch
from runspec.testing import ParseHarness
import mypkg.static_data as ei   # your module: main() + plain functions

def test_create_calls_store_once():
    h = ParseHarness(script_name="static-data", config_path=SPEC)
    spec = h.expect_ok(["create", "-s", "AAPL,MSFT", "--region", "americas", "--env", "prod"])

    with patch.object(ei, "_store_call", return_value={"ok": True}) as m:
        result = ei.create_symbols(spec.region.value, spec.symbols.value)

    assert result == {"ok": True}
    m.assert_called_once()
```

That is the whole pattern: parse with no mocks, run the logic with one mock at
the edge.

## Wiring it into your repo

Drop a `conftest.py` in your package's `tests/` directory so every test gets a
fresh harness and clean output:

```python
# tests/conftest.py
from __future__ import annotations

from pathlib import Path

import pytest

from runspec.testing import ParseHarness

# Your package's real runspec.toml. __file__.parent.parent assumes tests/ sits
# inside the package dir next to runspec.toml — adjust if your layout differs.
SPEC = Path(__file__).resolve().parent.parent / "runspec.toml"
RUNNABLE = "static-data"


@pytest.fixture()
def h() -> ParseHarness:
    """A fresh ParseHarness pointed at the real spec for each test."""
    return ParseHarness(script_name=RUNNABLE, config_path=SPEC)


@pytest.fixture(autouse=True)
def _quiet_runspec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never trip into the agent/interactive paths from a stray env var on the
    host."""
    monkeypatch.delenv("RUNSPEC_AGENT", raising=False)
```

`_quiet_runspec` is `autouse`, so it applies everywhere: it clears
`RUNSPEC_AGENT` so the host environment can't change parsing behaviour
mid-suite. No summary-suppression is needed — the `runspec: … completed`
terminal line is opt-in (`summary_console`, default off), so a parse never
leaks it into test output, and the audit record is written to the log file
only.

Then a parse-layer test file is just:

```python
# tests/test_static_data.py
def test_create_parses(h):
    spec = h.expect_ok(["create", "-s", "AAPL,MSFT", "--region", "americas", "--env", "prod"])
    assert spec.runspec_command == "create"

def test_smoke(h):
    h.smoke(values={"region": "americas", "env": "prod", "symbols": "AAPL",
                    "price": "12.5", "volume": "100", "ipo_date": "20240101"})
```

## Worked example — testing a `main()` with real branching logic

Not every `main()` is a one-liner. A migrated runnable often keeps real logic in
`main()` itself — credential guards, conditional rules, fan-out. You don't have
to refactor it to test it: drive `main()` directly, mock only its two I/O
boundaries, and vary the argv. Here is a real `main()` and the tests for it.

```python
# static_data/main.py  (the runnable under test)
from runspec import parse, errors
from static_data.functions import actions, user
import logging

logger = logging.getLogger(__name__)

def main() -> int:
    args = parse("static-data")
    service_user = user.get_service_user(env=args.env)          # boundary 1: credentials
    if service_user.username is None or service_user.password is None:
        raise errors.RunSpecError("* Missing service credentials: ...")
    action = args.runspec_command
    if action is None:
        raise errors.RunSpecError("* Missing RunSpec Command. For choices see --help")
    if action in {"clone", "create", "setup", "copy"} and len(args.symbols) > 1 and args.price.value is not None:
        logger.warning(f"Ignoring --price for {action} with {len(args.symbols)} symbols. ...")
        args.price = None
    if args.region == "asia_and_australia":                         # fan-out
        return (actions.multi(args=args, user=service_user, symbols=args.symbols, region="asia", action=action)
                + actions.multi(args=args, user=service_user, symbols=args.symbols, region="australia", action=action))
    return actions.multi(args=args, user=service_user, symbols=args.symbols, region=args.region, action=action)  # boundary 2: the work
```

The two things to mock are `user.get_service_user` (credential lookup) and
`actions.multi` (the work). Everything between them — the credential guard, the
multi-symbol price-drop rule, and the `asia_and_australia` fan-out — is the
logic worth testing, and it runs for real:

```python
# tests/test_static_data_main.py
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from runspec import errors
import static_data.main as main_mod        # <-- adjust to your module path

SPEC = Path(main_mod.__file__).resolve().parent / "runspec.toml"   # or import from conftest


def drive_main(monkeypatch, argv, *, username="svc", password="pw", multi_return=0):
    """Run main() for a CLI line, mocking both I/O boundaries.

    Returns (exit_code, [calls]) where each call records what main() decided to
    pass to actions.multi — including the post-rule price.
    """
    # main() calls parse("static-data") off sys.argv; point it at the real spec.
    monkeypatch.setenv("RUNSPEC_CONFIG", str(SPEC))
    monkeypatch.setattr(sys, "argv", ["static-data", *argv])

    monkeypatch.setattr(
        main_mod.user, "get_service_user",
        lambda env: SimpleNamespace(username=username, password=password),
    )
    calls: list[dict] = []

    def fake_multi(*, args, user, symbols, region, action):
        price = getattr(args, "price", None)
        price = getattr(price, "value", price)        # an Arg, or None after the drop rule
        calls.append({"region": region, "action": action,
                      "symbols": list(symbols), "price": price})
        return multi_return

    monkeypatch.setattr(main_mod.actions, "multi", fake_multi)
    return main_mod.main(), calls


def test_missing_credentials_raises(monkeypatch):
    with pytest.raises(errors.RunSpecError, match="Credentials"):
        drive_main(monkeypatch, ["create", "-s", "AAPL", "-r", "americas", "-e", "prod"], username=None)


def test_single_region_calls_multi_once(monkeypatch):
    code, calls = drive_main(monkeypatch, ["show", "-s", "AAPL", "-r", "americas", "-e", "prod"])
    assert code == 0
    assert len(calls) == 1
    assert calls[0]["region"] == "americas"
    assert calls[0]["action"] == "show"


def test_asia_and_australia_fans_out(monkeypatch):
    code, calls = drive_main(
        monkeypatch, ["show", "-s", "AAPL", "-r", "asia_and_australia", "-e", "prod"], multi_return=1
    )
    assert [c["region"] for c in calls] == ["asia", "australia"]
    assert code == 2                                   # exit codes summed


def test_price_dropped_for_multi_symbol_create(monkeypatch):
    _, calls = drive_main(
        monkeypatch, ["create", "-s", "AAPL,MSFT", "-p", "12.5", "-r", "americas", "-e", "prod"]
    )
    assert calls[0]["price"] is None                  # the >1-symbol rule applied


def test_price_kept_for_single_symbol_create(monkeypatch):
    _, calls = drive_main(
        monkeypatch, ["create", "-s", "AAPL", "-p", "12.5", "-r", "americas", "-e", "prod"]
    )
    assert calls[0]["price"] == 12.5
```

These five tests cover every decision `main()` makes without touching the
network, credentials, or the static-data store — the boundaries are the only
mocks.

## Validating a whole environment with `runspec test`

`ParseHarness.smoke()` tests one runnable from your own test suite. When you
bundle runnables from *many* packages into one environment — and some authors
may not have written tests at all — the [`runspec test`](cli.md#runspec-test)
CLI applies the same smoke check to **every discovered runnable**, then goes one
step further: it executes each installed entry point with `--help` in a
subprocess, so a runnable whose code fails to import is caught even though its
spec is fine. It exits 1 on any failure, so it drops straight into CI:

```yaml
- name: Smoke-test all runnables
  run: runspec test          # or: runspec test --format json
```

Think of it as `smoke()` generalised across the environment plus a real
import/exec check — no per-runnable test code required.

## Node: `ParseHarness` from `runspec-node/testing`

The Node pack ships the same helper, kept out of the main entry point (mirroring
Python's separate `runspec.testing` import) via a subpath export:

```ts
import { ParseHarness } from 'runspec-node/testing';

test('greet wiring', () => {
  const h = new ParseHarness({ scriptName: 'greet', toml: SPEC }); // or configPath:
  const parsed = h.expectOk(['--name', 'Ada']);
  expect(String(parsed.name)).toBe('Ada');

  h.expectExit(['create'], { code: 1, contains: 'name' }); // missing required arg
  h.smoke({ values: { name: 'Ada' } });                    // walk the whole tree
  h.close();
});
```

The API mirrors Python — `expectOk` / `expectExit({ code, contains })`,
`smoke({ values, checkHelp })`, and the `commandPaths()` / `requiredArgs(path)` /
`requiresCommand(path)` / `argsFor(path)` introspection. Node `parse()` calls
`process.exit(0)` on `--help` and *throws* on validation errors; the harness
intercepts both so a `--help` check never kills your test runner. `runspec test`
works the same in the Node pack, scoped to the venv-shaped folder's runnables.

