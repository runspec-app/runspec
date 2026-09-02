"""Wiring / parsing tests for the `confluence` runnable — no mocks, real spec."""

from pathlib import Path

from runspec.testing import ParseHarness

SPEC = Path(__file__).parent.parent / "runspec_confluence" / "runspec.toml"

VALUES = {
    "cql": "type = page",
    "query": "onboarding",
    "page-id": "12345",
    "space": "DOCS",
    "space-key": "DOCS",
    "title": "Home",
    "content": "<p>hi</p>",
    "label": "runbook",
    "text": "hello",
    "dest": "/tmp/out",
    "file": "/tmp/f.pdf",
    "filename": "doc.pdf",
}


def _harness() -> ParseHarness:
    return ParseHarness(script_name="confluence", config_path=SPEC)


def test_smoke_all_verbs() -> None:
    h = _harness()
    done = h.smoke(values=VALUES)
    assert len(done) > 30
    h.close()


def test_global_env_choice_inherited() -> None:
    h = _harness()
    spec = h.expect_ok(["--env", "prod", "get-page", "--page-id", "1"])
    assert spec.env.value == "prod"
    h.close()


def test_content_format_defaults_storage() -> None:
    h = _harness()
    spec = h.expect_ok(["create-page", "--space", "DOCS", "--title", "T", "--content", "<p/>"])
    assert spec.content_format.value == "storage"
    h.close()


def test_bad_content_format_rejected() -> None:
    h = _harness()
    h.expect_exit(
        ["create-page", "--space", "DOCS", "--title", "T", "--content", "x", "--content-format", "rst"],
        contains="content-format",
    )
    h.close()


def test_limit_coerces_int() -> None:
    h = _harness()
    spec = h.expect_ok(["search", "--cql", "type = page", "--limit", "7"])
    assert spec.limit.value == 7
    assert isinstance(spec.limit.value, int)
    h.close()


def test_minor_edit_flag() -> None:
    h = _harness()
    spec = h.expect_ok(["update-page", "--page-id", "1", "--title", "T", "--minor-edit"])
    assert spec.minor_edit.value is True
    h.close()


def test_missing_required_cql_rejected() -> None:
    h = _harness()
    h.expect_exit(["search"], contains="cql")
    h.close()


def test_require_command_enforced() -> None:
    h = _harness()
    h.expect_exit([])
    h.close()
