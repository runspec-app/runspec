"""Tests for the `discoverable` keyword — audience normalisation/validation."""

from runspec.discovery import (
    is_discoverable_to,
    normalise_discoverable,
    validate_discoverable,
)


class TestNormalise:
    def test_absent_defaults_to_operator(self):
        assert normalise_discoverable(None) == ["operator"]

    def test_true_is_operator(self):
        assert normalise_discoverable(True) == ["operator"]

    def test_false_is_empty(self):
        assert normalise_discoverable(False) == []

    def test_list_passthrough(self):
        assert normalise_discoverable(["operator", "self-service"]) == ["operator", "self-service"]

    def test_self_service_only(self):
        assert normalise_discoverable(["self-service"]) == ["self-service"]

    def test_dedupes_preserving_order(self):
        assert normalise_discoverable(["operator", "operator", "self-service"]) == ["operator", "self-service"]


class TestIsDiscoverableTo:
    def test_default_reaches_operator(self):
        assert is_discoverable_to(None, "operator") is True

    def test_default_excludes_self_service(self):
        # The whole point: bare true never implies self-service.
        assert is_discoverable_to(True, "self-service") is False

    def test_false_reaches_nobody(self):
        assert is_discoverable_to(False, "operator") is False
        assert is_discoverable_to(False, "self-service") is False

    def test_explicit_self_service(self):
        assert is_discoverable_to(["operator", "self-service"], "self-service") is True

    def test_self_service_only_hides_from_operator(self):
        assert is_discoverable_to(["self-service"], "operator") is False


class TestValidate:
    def test_none_true_false_ok(self):
        assert validate_discoverable(None) == []
        assert validate_discoverable(True) == []
        assert validate_discoverable(False) == []

    def test_known_audiences_ok(self):
        assert validate_discoverable(["operator", "self-service"]) == []

    def test_empty_list_rejected(self):
        errs = validate_discoverable([])
        assert errs and "write false" in errs[0]

    def test_unknown_audience_rejected(self):
        errs = validate_discoverable(["operator", "admins"])
        assert errs and "unknown audience" in errs[0]

    def test_wrong_type_rejected(self):
        errs = validate_discoverable("operator")
        assert errs and "must be true, false, or a list" in errs[0]


class TestModelProperty:
    def test_runspec_discoverable_reads_spec(self):
        from pathlib import Path

        from runspec.models import RunSpec

        rs = RunSpec(
            __runspec_runnable__="reset",
            __runspec_source__=Path("/tmp/runspec.toml"),
            __runspec_spec__={"discoverable": ["operator", "self-service"]},
        )
        assert rs.runspec_discoverable == ["operator", "self-service"]
        assert "self-service" in rs.runspec_discoverable

    def test_runspec_discoverable_default(self):
        from pathlib import Path

        from runspec.models import RunSpec

        rs = RunSpec(
            __runspec_runnable__="plain",
            __runspec_source__=Path("/tmp/runspec.toml"),
            __runspec_spec__={},
        )
        assert rs.runspec_discoverable == ["operator"]
