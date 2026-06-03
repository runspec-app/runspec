"""
Tests for the Arg transparent value access protocol.

Arg wraps a resolved value and should behave like that value in expressions —
no .value unwrapping required by callers.
"""

import sys

import pytest

from runspec.models import Arg


def arg(value, *, type="str", name="x"):
    """Minimal Arg constructor for tests."""
    return Arg(value=value, name=name, type=type)


class TestStringProtocol:
    def test_str(self):
        assert str(arg("hello")) == "hello"

    def test_repr(self):
        assert repr(arg("hello")) == "'hello'"

    def test_fstring_plain(self):
        a = arg("world")
        assert f"Hello, {a}!" == "Hello, world!"

    def test_fstring_format_spec(self):
        a = arg(3.14159, type="float")
        assert f"{a:.2f}" == "3.14"

    def test_fstring_alignment(self):
        a = arg("hi", type="str")
        assert f"{a:>10}" == "        hi"

    def test_fstring_int_format(self):
        a = arg(255, type="int")
        assert f"{a:#010x}" == "0x000000ff"


class TestNumericProtocol:
    def test_int(self):
        assert int(arg(4.9, type="float")) == 4

    def test_float(self):
        assert float(arg(3, type="int")) == 3.0

    def test_index_in_range(self):
        a = arg(3, type="int")
        assert list(range(a)) == [0, 1, 2]

    def test_index_in_list_slice(self):
        a = arg(2, type="int")
        assert [10, 20, 30, 40][a] == 30

    def test_addition(self):
        assert arg(3, type="int") + 2 == 5

    def test_radd(self):
        assert 2 + arg(3, type="int") == 5

    def test_subtraction(self):
        assert arg(10, type="int") - 3 == 7

    def test_multiplication(self):
        assert arg(4, type="int") * 3 == 12

    def test_rmul(self):
        assert 3 * arg(4, type="int") == 12

    def test_truediv(self):
        assert arg(10, type="float") / 4 == 2.5

    def test_floordiv(self):
        assert arg(10, type="int") // 3 == 3

    def test_mod(self):
        assert arg(10, type="int") % 3 == 1


class TestComparisonProtocol:
    def test_eq_to_native(self):
        assert arg(5, type="int") == 5

    def test_eq_to_arg(self):
        assert arg(5, type="int") == arg(5, type="int")

    def test_lt(self):
        assert arg(3, type="int") < 5

    def test_le(self):
        assert arg(5, type="int") <= 5

    def test_gt(self):
        assert arg(7, type="int") > 5

    def test_ge(self):
        assert arg(5, type="int") >= 5

    def test_comparison_returns_bool(self):
        result = arg(3, type="int") < 5
        assert type(result) is bool

    def test_eq_returns_bool(self):
        result = arg(3, type="int") == 3
        assert type(result) is bool


class TestBoolProtocol:
    def test_truthy_flag(self):
        assert bool(arg(True, type="flag")) is True

    def test_falsy_flag(self):
        assert bool(arg(False, type="flag")) is False

    def test_if_statement(self):
        a = arg(True, type="flag")
        triggered = False
        if a:
            triggered = True
        assert triggered


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
        a = Arg(value="hello", name="name", type="str")
        assert a.meta is None

    def test_meta_lookup_by_value(self):
        a = Arg(
            value="web-02",
            name="server",
            type="choice",
            meta={"web-01": {"datacenter": "us-east"}, "web-02": {"datacenter": "us-west"}},
        )
        datacenter = a.meta[a.value]["datacenter"]
        assert datacenter == "us-west"


class TestIterationProtocol:
    def test_iter_on_list_value(self):
        a = arg(["a", "b", "c"], type="str")
        assert list(a) == ["a", "b", "c"]

    def test_len_on_list_value(self):
        a = arg(["x", "y"], type="str")
        assert len(a) == 2

    def test_len_on_string(self):
        assert len(arg("hello")) == 5


class TestPathProtocol:
    def test_fspath_returns_string(self, tmp_path):
        import os

        a = arg(tmp_path, type="path")
        assert os.fspath(a) == str(tmp_path)

    def test_path_constructor_accepts_arg(self, tmp_path):
        from pathlib import Path

        a = arg(tmp_path, type="path")
        assert Path(a) == tmp_path

    @pytest.mark.skipif(sys.version_info < (3, 12), reason="Path.glob() requires exact str before Python 3.12 (pathlib internals rewritten in 3.12)")
    def test_glob_accepts_str_arg_as_pattern(self, tmp_path):
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.txt").touch()
        directory = arg(tmp_path, type="path")
        pattern = arg("*.txt", type="str")
        matches = list(directory.glob(pattern))
        assert len(matches) == 2

    def test_fspath_raises_for_non_path(self):
        import os

        a = arg(42, type="int")
        try:
            os.fspath(a)
            raise AssertionError("expected TypeError")
        except TypeError:
            pass


class TestHashProtocol:
    def test_arg_hashable(self):
        a = arg("hello")
        assert hash(a) == hash("hello")

    def test_arg_usable_in_set(self):
        a = arg("x")
        b = arg("y")
        s = {a, b}
        assert len(s) == 2

    def test_arg_usable_as_dict_key(self):
        a = arg("key")
        d = {a: "value"}
        assert d[a] == "value"

    def test_equal_args_have_same_hash(self):
        a = arg(5, type="int")
        b = arg(5, type="int")
        assert a == b
        assert hash(a) == hash(b)


class TestIndexProtocol:
    def test_getitem_on_list(self):
        a = arg(["x", "y", "z"], type="str")
        assert a[0] == "x"
        assert a[2] == "z"

    def test_getitem_on_string(self):
        a = arg("hello")
        assert a[1] == "e"

    def test_getitem_slice(self):
        a = arg([10, 20, 30, 40], type="int")
        assert a[1:3] == [20, 30]


class TestRunSpecMetadataProperties:
    """Public properties mirror the __runspec_*__ dunder fields without the ugly access."""

    def _make(self, **overrides):
        from pathlib import Path

        from runspec.models import RunSpec

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
        rs = self._make()
        assert rs.runspec_runnable == "clean"

    def test_runspec_source(self):
        from pathlib import Path

        rs = self._make(__runspec_source__=Path("/etc/runspec.toml"))
        assert rs.runspec_source == Path("/etc/runspec.toml")

    def test_runspec_autonomy(self):
        rs = self._make(__runspec_autonomy__="autonomous")
        assert rs.runspec_autonomy == "autonomous"

    def test_runspec_agent_false_by_default(self):
        rs = self._make()
        assert rs.runspec_agent is False

    def test_runspec_agent_true_under_agent(self):
        rs = self._make(__runspec_agent__=True)
        assert rs.runspec_agent is True

    def test_runspec_spec(self):
        rs = self._make(__runspec_spec__={"description": "Greet"})
        assert rs.runspec_spec == {"description": "Greet"}

    def test_runspec_groups(self):
        from runspec.models import Group

        g = Group(name="auth", args=["user", "pass"], inclusive=True)
        rs = self._make(__runspec_groups__=[g])
        assert rs.runspec_groups == [g]

    def test_runspec_command_none_when_no_subcommand(self):
        rs = self._make()
        assert rs.runspec_command is None

    def test_runspec_command_path_empty_when_no_subcommand(self):
        rs = self._make()
        assert rs.runspec_command_path == []

    def test_runspec_command_returns_leaf(self):
        rs = self._make(__runspec_command_path__=["run", "stage"])
        assert rs.runspec_command == "stage"
        assert rs.runspec_command_path == ["run", "stage"]


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
        # .type stays the item type — it drives per-item coercion.
        a = Arg(value=["a"], name="t", type="str", multiple=True)
        assert a.type == "str"
