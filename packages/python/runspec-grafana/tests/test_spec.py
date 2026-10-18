"""Wiring / parsing tests for the `grafana` runnable — no mocks, real spec."""

import sys
from pathlib import Path

from runspec.testing import ParseHarness

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

SPEC = Path(__file__).parent.parent / "runspec_grafana" / "runspec.toml"


def _harness() -> ParseHarness:
    return ParseHarness(script_name="grafana", config_path=SPEC)


def test_smoke_all_verbs() -> None:
    h = _harness()
    # --help exits 0, require-command is enforced, omitting required args exits 1,
    # and a fully-populated invocation parses — for every verb.
    h.smoke(
        values={
            "query": "up",
            "uid": "abc",
            "datasource": "prom1",
            "label": "job",
            "metric": "http_request_duration_seconds",
            "text": "deploy",
            "path": "/api/org",
        }
    )
    h.close()


def test_autonomy_tiers() -> None:
    commands = tomllib.loads(SPEC.read_text())["grafana"]["commands"]
    tiers = {name: body["autonomy"] for name, body in commands.items()}
    assert {n for n, t in tiers.items() if t != "autonomous"} == {"create-annotation"}
    assert tiers["create-annotation"] == "confirm"


def test_include_is_a_comma_list() -> None:
    h = _harness()
    spec = h.expect_ok(["get-dashboard", "--uid", "abc", "--include", "queries,links"])
    assert list(spec.include.value) == ["queries", "links"]
    h.expect_exit(["get-dashboard", "--uid", "abc", "--include", "everything"], contains="include")
    h.close()


def test_query_prometheus_defaults() -> None:
    h = _harness()
    spec = h.expect_ok(["query-prometheus", "--query", "up"])
    assert spec.query_type.value == "instant" and spec.limit.value == 50 and spec.datasource.value is None
    h.expect_exit(["query-prometheus", "--query", "up", "--query-type", "sideways"])
    h.close()


def test_globals_and_repeatables() -> None:
    h = _harness()
    spec = h.expect_ok(["--env", "prod-eu", "--org-id", "2", "search-dashboards", "--tag", "prod", "--tag", "api"])
    assert spec.env.value == "prod-eu" and spec.org_id.value == "2"
    assert list(spec.tag.value) == ["prod", "api"]
    h.close()


def test_token_refused_on_command_line() -> None:
    h = _harness()
    h.expect_exit(["--grafana-token", "glsa_secret", "whoami"], contains="grafana-token")
    h.close()


def test_histogram_percentile_range() -> None:
    h = _harness()
    assert h.expect_ok(["query-prometheus-histogram", "--metric", "lat"]).percentile.value == 95.0
    h.expect_exit(["query-prometheus-histogram", "--metric", "lat", "--percentile", "150"], contains="percentile")
    h.close()


def test_auth_args_declare_the_env_vars_the_core_reads() -> None:
    # runspec-console applies an unscoped credential only to a runnable that
    # declares one of its env var names (a "Grafana Service Account" token →
    # GRAFANA_SERVICE_ACCOUNT_TOKEN, a "Grafana" login → GRAFANA_USERNAME), and
    # its Env files window lists only declared names.
    args = tomllib.loads(SPEC.read_text())["grafana"]["args"]

    def envs(name: str) -> list[str]:
        env = args[name]["env"]
        return [env] if isinstance(env, str) else env

    assert envs("url") == ["GRAFANA_URL"]
    assert envs("org-id") == ["GRAFANA_ORG_ID"]
    assert envs("grafana-token") == ["GRAFANA_SERVICE_ACCOUNT_TOKEN", "GRAFANA_TOKEN", "GRAFANA_PAT", "GRAFANA_API_KEY"]
    assert envs("grafana-user") == ["GRAFANA_USERNAME", "GRAFANA_USER"]
    assert envs("grafana-password") == ["GRAFANA_PASSWORD"]
