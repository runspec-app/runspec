"""Tests for the `discoverable` keyword — boolean visibility gate."""

from runspec.discovery import (
    normalise_discoverable,
    validate_discoverable,
)


class TestNormalise:
    def test_absent_defaults_to_visible(self):
        assert normalise_discoverable(None) is True

    def test_true_is_visible(self):
        assert normalise_discoverable(True) is True

    def test_false_is_hidden(self):
        assert normalise_discoverable(False) is False


class TestValidate:
    def test_none_true_false_ok(self):
        assert validate_discoverable(None) == []
        assert validate_discoverable(True) == []
        assert validate_discoverable(False) == []

    def test_list_rejected(self):
        errs = validate_discoverable(["operator", "self-service"])
        assert errs and "must be true or false" in errs[0]

    def test_wrong_type_rejected(self):
        errs = validate_discoverable("operator")
        assert errs and "must be true or false" in errs[0]


class TestModelProperty:
    def test_runspec_discoverable_reads_spec(self):
        from pathlib import Path

        from runspec.models import RunSpec

        rs = RunSpec(
            __runspec_runnable__="helper",
            __runspec_source__=Path("/tmp/runspec.toml"),
            __runspec_spec__={"discoverable": False},
        )
        assert rs.runspec_discoverable is False

    def test_runspec_discoverable_default(self):
        from pathlib import Path

        from runspec.models import RunSpec

        rs = RunSpec(
            __runspec_runnable__="plain",
            __runspec_source__=Path("/tmp/runspec.toml"),
            __runspec_spec__={},
        )
        assert rs.runspec_discoverable is True
