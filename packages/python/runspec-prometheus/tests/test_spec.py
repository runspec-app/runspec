"""Wiring / parsing tests for the `prometheus` runnable — no mocks, real spec."""

import sys
from pathlib import Path

from runspec.testing import ParseHarness

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SPEC = Path(__file__).parent.parent / "runspec_prometheus" / "runspec.toml"


def _harness() -> ParseHarness:
    return ParseHarness(script_name="prometheus", config_path=SPEC)


def test_smoke_all_verbs() -> None:
    h = _harness()
    # --help exits 0, require-command is enforced, omitting required args exits 1,
    # and a fully-populated invocation parses — for every verb.
    h.smoke(values={"query": "up", "match": ["up"], "label": "job", "start": "1h", "end": "now"})
    h.close()


def test_autonomy_tiers() -> None:
    commands = tomllib.loads(SPEC.read_text())["prometheus"]["commands"]
    tiers = {name: body["autonomy"] for name, body in commands.items()}
    assert {n for n, t in tiers.items() if t == "manual"} == {"delete-series", "quit"}
    assert {n for n, t in tiers.items() if t == "confirm"} == {"reload", "snapshot", "clean-tombstones"}
    assert all(t == "autonomous" for n, t in tiers.items() if n not in {"delete-series", "quit", "reload", "snapshot", "clean-tombstones"})


def test_query_defaults() -> None:
    h = _harness()
    spec = h.expect_ok(["query", "--query", "up"])
    assert spec.runspec_command == "query"
    assert spec.limit.value == 100 and spec.time.value is None
    h.close()


def test_repeatable_match_and_globals() -> None:
    h = _harness()
    spec = h.expect_ok(["--env", "prod-eu", "--url", "http://p:9090", "series", "--match", "up", "--match", '{job="node"}'])
    assert spec.env.value == "prod-eu" and spec.url.value == "http://p:9090"
    assert list(spec.match.value) == ["up", '{job="node"}']
    h.close()


def test_delete_series_requires_window() -> None:
    h = _harness()
    h.expect_exit(["delete-series", "--match", "up"], contains="start")
    h.close()


def test_list_targets_choices() -> None:
    h = _harness()
    spec = h.expect_ok(["list-targets", "--health", "down"])
    assert spec.state.value == "active" and spec.health.value == "down"
    h.expect_exit(["list-targets", "--health", "sideways"])
    h.close()
