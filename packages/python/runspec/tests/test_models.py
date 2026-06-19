"""
Tests for the explicit-value Arg model and the RunSpec namespace.

Since 0.38.0 Arg is NOT a transparent proxy: callers read ``.value`` for the
coerced native value. Arg deliberately does not masquerade as its value —
arithmetic / indexing / iteration / ``int()`` / ``len()`` / ``os.fspath`` all
raise, and the two contexts that would otherwise be *silently* wrong without a
``.value`` (truthiness and equality) raise a guiding TypeError.
"""

import os
from pathlib import Path

import pytest

from runspec.models import Arg, Group, RunSpec


def arg(value, *, type="str", name="x", **kw):
    """Minimal Arg constructor for tests."""
    return Arg(value=value, name=name, type=type, **kw)


class TestValueAccess:
    def test_value_is_the_native_value(self):
        assert arg("hello").value == "hello"
        assert arg(4, type="int").value == 4
        assert arg(3.14, type="float").value == 3.14

    def test_unset_optional_value_is_real_none(self):
        # The whole point: an unset optional is honestly None via .value,
        # not an Arg wrapping None that fails `is None`.
        a = arg(None, type="str", name="since")
        assert a.value is None

    def test_metadata_alongside_value(self):
        a = arg("env", type="choice", name="environment", source="cli")
        assert a.value == "env"
        assert a.name == "environment"
        assert a.type == "choice"
        assert a.source == "cli"

    def test_repr_shows_it_is_an_arg(self):
        # repr must not masquerade as the bare value — a stray print should
        # look wrong, prompting the developer to add .value.
        assert repr(arg("hello", name="greeting")) == "Arg(name='greeting', value='hello')"


class TestNoTransparentProtocol:
    """Operations the old transparent proxy supported now fail loudly."""

    def test_bool_raises(self):
        with pytest.raises(TypeError, match=r"boolean context"):
            bool(arg(True, type="flag", name="dry_run"))

    def test_if_statement_raises(self):
        with pytest.raises(TypeError, match=r"\.value"):
            if arg(False, type="flag", name="force"):
                pass

    def test_eq_raises(self):
        with pytest.raises(TypeError, match=r"compared with `==`"):
            _ = arg("fast", name="mode") == "fast"

    def test_ne_raises(self):
        with pytest.raises(TypeError, match=r"compared with `!=`"):
            _ = arg("fast", name="mode") != "slow"

    def test_arithmetic_raises(self):
        with pytest.raises(TypeError):
            _ = arg(3, type="int") + 1

    def test_int_raises(self):
        with pytest.raises(TypeError):
            int(arg(4, type="int"))

    def test_iter_raises(self):
        with pytest.raises(TypeError):
            list(arg(["a", "b"], type="str", multiple=True))

    def test_len_raises(self):
        with pytest.raises(TypeError):
            len(arg(["a", "b"], type="str", multiple=True))

    def test_getitem_raises(self):
        with pytest.raises(TypeError):
            _ = arg(["a", "b"], type="str", multiple=True)[0]

    def test_fspath_raises(self, tmp_path):
        with pytest.raises(TypeError):
            os.fspath(arg(tmp_path, type="path"))

    def test_attribute_delegation_gone(self, tmp_path):
        # args.path.exists() no longer delegates — use args.path.value.exists().
        a = arg(tmp_path, type="path")
        with pytest.raises(AttributeError):
            a.exists()
        assert a.value.exists()  # the explicit form works


class TestMeta:
    def test_meta_accessible(self):
        a = Arg(
            value="web-01",
            name="server",
            type="choice",
            meta={"web-01": {"datacenter": "us-east"}, "web-02": {"datacenter": "us-west"}},
        )
        assert a.meta["web-01"]["datacenter"] == "us-east"

    def test_meta_absent_is_none(self):
        assert Arg(value="hello", name="name", type="str").meta is None

    def test_meta_lookup_by_value(self):
        a = Arg(
            value="web-02",
            name="server",
            type="choice",
            meta={"web-01": {"datacenter": "us-east"}, "web-02": {"datacenter": "us-west"}},
        )
        assert a.meta[a.value]["datacenter"] == "us-west"


class TestPythonType:
    """Arg.python_type reflects the effective value type, including lists for
    multiple-valued args (where .type alone is just the item type)."""

    def test_scalar_types(self):
        assert Arg(value="x", name="s", type="str").python_type == "str"
        assert Arg(value=1, name="i", type="int").python_type == "int"
        assert Arg(value=1.0, name="f", type="float").python_type == "float"
        assert Arg(value=True, name="b", type="flag").python_type == "bool"

    def test_path_maps_to_Path(self):
        assert Arg(value="/x", name="p", type="path").python_type == "Path"

    def test_multiple_wraps_in_list(self):
        assert Arg(value=["a", "b"], name="t", type="str", multiple=True).python_type == "list[str]"
        assert Arg(value=[1, 2], name="n", type="int", multiple=True).python_type == "list[int]"

    def test_rest_is_list_str(self):
        assert Arg(value=["--x"], name="e", type="rest").python_type == "list[str]"

    def test_custom_type_passthrough(self):
        a = Arg(value=[1], name="j", type="json-file", multiple=True)
        assert a.python_type == "list[json-file]"

    def test_type_field_unchanged(self):
        a = Arg(value=["a"], name="t", type="str", multiple=True)
        assert a.type == "str"


class TestRunSpecMetadataProperties:
    """Public properties mirror the __runspec_*__ dunder fields without the ugly access."""

    def _make(self, **overrides):
        defaults = {
            "__runspec_runnable__": "clean",
            "__runspec_source__": Path("/tmp/runspec.toml"),
            "__runspec_command_path__": [],
            "__runspec_autonomy__": "confirm",
            "__runspec_agent__": False,
            "__runspec_spec__": {"description": "test"},
            "__runspec_groups__": [],
        }
        defaults.update(overrides)
        return RunSpec(**defaults)

    def test_runspec_runnable(self):
        assert self._make().runspec_runnable == "clean"

    def test_runspec_source(self):
        rs = self._make(__runspec_source__=Path("/etc/runspec.toml"))
        assert rs.runspec_source == Path("/etc/runspec.toml")

    def test_runspec_autonomy(self):
        assert self._make(__runspec_autonomy__="autonomous").runspec_autonomy == "autonomous"

    def test_runspec_agent_false_by_default(self):
        assert self._make().runspec_agent is False

    def test_runspec_agent_true_under_agent(self):
        assert self._make(__runspec_agent__=True).runspec_agent is True

    def test_runspec_spec(self):
        assert self._make(__runspec_spec__={"description": "Greet"}).runspec_spec == {"description": "Greet"}

    def test_runspec_groups(self):
        g = Group(name="auth", args=["user", "pass"], inclusive=True)
        assert self._make(__runspec_groups__=[g]).runspec_groups == [g]

    def test_runspec_command_none_when_no_subcommand(self):
        assert self._make().runspec_command is None

    def test_runspec_command_path_empty_when_no_subcommand(self):
        assert self._make().runspec_command_path == []

    def test_runspec_command_returns_leaf(self):
        rs = self._make(__runspec_command_path__=["run", "stage"])
        assert rs.runspec_command == "stage"
        assert rs.runspec_command_path == ["run", "stage"]


class TestRunSpecArgAccess:
    """Attribute access, safe get(), membership, and dir()."""

    def _make_with_args(self, **args):
        rs = RunSpec(
            __runspec_runnable__="deploy",
            __runspec_source__=Path("/tmp/runspec.toml"),
        )
        for name, a in args.items():
            rs._set_arg(name, a)
        return rs

    def test_attribute_access_returns_arg(self):
        rs = self._make_with_args(region=arg("eu", name="region"))
        assert isinstance(rs.region, Arg)
        assert rs.region.value == "eu"

    def test_hyphen_normalised_to_underscore(self):
        rs = self._make_with_args(**{"dry-run": arg(True, type="flag", name="dry-run")})
        assert rs.dry_run.value is True

    def test_missing_arg_raises_attribute_error_with_available(self):
        rs = self._make_with_args(region=arg("eu", name="region"))
        with pytest.raises(AttributeError, match=r"No argument 'symbol'.*Available: region"):
            _ = rs.symbol

    def test_contains_true_for_present_arg(self):
        rs = self._make_with_args(region=arg("eu", name="region"))
        assert "region" in rs
        assert "region-name".replace("-", "_") not in rs  # sanity

    def test_contains_normalises_hyphens(self):
        rs = self._make_with_args(**{"dry-run": arg(True, type="flag", name="dry-run")})
        assert "dry-run" in rs
        assert "dry_run" in rs

    def test_contains_false_for_other_subcommand_arg(self):
        rs = self._make_with_args(region=arg("eu", name="region"))
        assert "symbol" not in rs

    def test_get_present_returns_real_arg(self):
        a = arg("eu", name="region")
        rs = self._make_with_args(region=a)
        assert rs.get("region") is a
        assert rs.get("region").value == "eu"

    def test_get_absent_returns_not_set_arg_with_none(self):
        rs = self._make_with_args(region=arg("eu", name="region"))
        got = rs.get("symbol")
        assert isinstance(got, Arg)
        assert got.value is None
        assert got.source == "not_set"

    def test_get_absent_with_default(self):
        rs = self._make_with_args(region=arg("eu", name="region"))
        assert rs.get("symbol", "ABC123").value == "ABC123"

    def test_get_never_raises_supports_safe_value_access(self):
        # The boilerplate-killer: no up-front guard needed.
        rs = self._make_with_args(region=arg("eu", name="region"))
        assert rs.get("since").value is None

    def test_dir_includes_active_args(self):
        rs = self._make_with_args(region=arg("eu", name="region"), env=arg("qa", name="env"))
        names = dir(rs)
        assert "region" in names
        assert "env" in names
        assert "runspec_runnable" in names  # real properties still present
