"""
Tests for installed package discovery in cli.py.

Covers _deduplicate, _check_dist_files, _check_editable_source,
and _discover_installed using lightweight fakes — no real distributions needed.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from runspec.cli import (
    _check_dist_files,
    _check_editable_source,
    _deduplicate,
    _discover_installed,
    _requires_runspec,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_dist_file(name: str, content: str, base: Path) -> Any:
    """Create a fake dist file object whose locate() returns a real temp file."""
    p = base / name
    p.write_text(content, encoding="utf-8")

    obj = SimpleNamespace(name=name)
    obj.locate = lambda: p  # type: ignore[attr-defined]
    return obj


# ── _deduplicate ──────────────────────────────────────────────────────────────


def test_deduplicate_empty():
    assert _deduplicate([]) == []


def test_deduplicate_no_dupes(tmp_path):
    a = tmp_path / "a.toml"
    b = tmp_path / "b.toml"
    a.touch()
    b.touch()
    items = [
        {"source": str(a), "runnable": "foo", "spec": {}},
        {"source": str(b), "runnable": "bar", "spec": {}},
    ]
    assert _deduplicate(items) == items


def test_deduplicate_removes_exact_dupes(tmp_path):
    p = tmp_path / "runspec.toml"
    p.touch()
    items = [
        {"source": str(p), "runnable": "foo", "spec": {}},
        {"source": str(p), "runnable": "foo", "spec": {}},
    ]
    result = _deduplicate(items)
    assert len(result) == 1


def test_deduplicate_different_runnables_same_source(tmp_path):
    p = tmp_path / "runspec.toml"
    p.touch()
    items = [
        {"source": str(p), "runnable": "foo", "spec": {}},
        {"source": str(p), "runnable": "bar", "spec": {}},
    ]
    result = _deduplicate(items)
    assert len(result) == 2


def test_deduplicate_resolves_paths(tmp_path):
    p = tmp_path / "runspec.toml"
    p.touch()
    # Use a path with trailing component that resolves to the same file
    items = [
        {"source": str(p), "runnable": "foo", "spec": {}},
        {"source": str(p.parent / "." / p.name), "runnable": "foo", "spec": {}},
    ]
    result = _deduplicate(items)
    assert len(result) == 1


# ── _check_dist_files ─────────────────────────────────────────────────────────


def test_check_dist_files_none_files():
    dist = SimpleNamespace(files=None)
    assert _check_dist_files(dist) == []


def test_check_dist_files_no_runspec_toml(tmp_path):
    other = _make_dist_file("other.txt", "x", tmp_path)
    dist = SimpleNamespace(files=[other])
    assert _check_dist_files(dist) == []


def test_check_dist_files_finds_runspec_toml(tmp_path):
    toml_content = textwrap.dedent("""\
        [greet]
        description = "Say hello"
        [greet.args.name]
        type = "str"
    """)
    f = _make_dist_file("runspec.toml", toml_content, tmp_path)
    dist = SimpleNamespace(files=[f])

    result = _check_dist_files(dist)
    assert len(result) == 1
    assert result[0]["runnable"] == "greet"
    assert result[0]["spec"]["description"] == "Say hello"
    assert str(tmp_path / "runspec.toml") == result[0]["source"] or Path(result[0]["source"]).name == "runspec.toml"


def test_check_dist_files_carries_config_autonomy_default(tmp_path):
    # [config] autonomy-default must reach the discovered item as config_autonomy
    # so `runspec local --format json` consumers (the console Forms tab) resolve
    # a no-autonomy subcommand to the package default, not "confirm". Regression:
    # this read the "autonomy" key while load_raw normalises to "autonomy_default".
    toml_content = textwrap.dedent("""\
        [config]
        autonomy-default = "autonomous"
        [deploy]
        description = "Deploy"
        require-command = true
        [deploy.commands.rollout]
        description = "Roll out"
    """)
    f = _make_dist_file("runspec.toml", toml_content, tmp_path)
    dist = SimpleNamespace(files=[f])

    result = _check_dist_files(dist)
    assert len(result) == 1
    assert result[0]["config_autonomy"] == "autonomous"


def test_check_dist_files_config_autonomy_defaults_to_confirm(tmp_path):
    # No [config] autonomy-default → the library default "confirm" (loader fills
    # it), so consumers still get a concrete value rather than None.
    toml_content = textwrap.dedent("""\
        [greet]
        description = "Say hello"
    """)
    f = _make_dist_file("runspec.toml", toml_content, tmp_path)
    dist = SimpleNamespace(files=[f])

    result = _check_dist_files(dist)
    assert result[0]["config_autonomy"] == "confirm"


def test_check_editable_source_carries_config_autonomy_default(tmp_path):
    toml_content = textwrap.dedent("""\
        [config]
        autonomy-default = "autonomous"
        [compress]
        description = "Compress files"
    """)
    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "runspec.toml").write_text(toml_content, encoding="utf-8")

    data = {"url": tmp_path.as_uri(), "dir_info": {"editable": True}}
    f = _make_dist_file("direct_url.json", json.dumps(data), tmp_path)
    dist = SimpleNamespace(files=[f])

    result = _check_editable_source(dist)
    assert len(result) == 1
    assert result[0]["config_autonomy"] == "autonomous"


def test_check_dist_files_empty_runnables(tmp_path):
    toml_content = textwrap.dedent("""\
        [config]
        autonomy_default = "confirm"
    """)
    f = _make_dist_file("runspec.toml", toml_content, tmp_path)
    dist = SimpleNamespace(files=[f])
    assert _check_dist_files(dist) == []


# ── _check_editable_source ────────────────────────────────────────────────────


def test_check_editable_source_none_files():
    dist = SimpleNamespace(files=None)
    assert _check_editable_source(dist) == []


def test_check_editable_source_no_direct_url(tmp_path):
    other = _make_dist_file("RECORD", "", tmp_path)
    dist = SimpleNamespace(files=[other])
    assert _check_editable_source(dist) == []


def test_check_editable_source_not_editable(tmp_path):
    data = {"url": f"file://{tmp_path}", "dir_info": {"editable": False}}
    f = _make_dist_file("direct_url.json", json.dumps(data), tmp_path)
    dist = SimpleNamespace(files=[f])
    assert _check_editable_source(dist) == []


def test_check_editable_source_non_file_url(tmp_path):
    data = {"url": "https://example.com/pkg.tar.gz", "dir_info": {"editable": True}}
    f = _make_dist_file("direct_url.json", json.dumps(data), tmp_path)
    dist = SimpleNamespace(files=[f])
    assert _check_editable_source(dist) == []


def test_check_editable_source_finds_runspec_toml(tmp_path):
    toml_content = textwrap.dedent("""\
        [compress]
        description = "Compress files"
        [compress.args.input]
        type = "path"
    """)
    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir()
    (pkg_dir / "runspec.toml").write_text(toml_content, encoding="utf-8")

    data = {"url": tmp_path.as_uri(), "dir_info": {"editable": True}}
    f = _make_dist_file("direct_url.json", json.dumps(data), tmp_path)
    dist = SimpleNamespace(files=[f])

    result = _check_editable_source(dist)
    assert len(result) == 1
    assert result[0]["runnable"] == "compress"


def test_check_editable_source_no_config_file(tmp_path):
    # No subdirectories with runspec.toml — empty result
    data = {"url": tmp_path.as_uri(), "dir_info": {"editable": True}}
    f = _make_dist_file("direct_url.json", json.dumps(data), tmp_path)
    dist = SimpleNamespace(files=[f])
    assert _check_editable_source(dist) == []


# ── _requires_runspec ─────────────────────────────────────────────────────────


def test_requires_runspec_no_requires():
    dist = SimpleNamespace(requires=None)
    assert _requires_runspec(dist) is False


def test_requires_runspec_empty():
    dist = SimpleNamespace(requires=[])
    assert _requires_runspec(dist) is False


def test_requires_runspec_found_plain():
    dist = SimpleNamespace(requires=["runspec"])
    assert _requires_runspec(dist) is True


def test_requires_runspec_found_with_version():
    dist = SimpleNamespace(requires=["runspec>=0.1.0"])
    assert _requires_runspec(dist) is True


def test_requires_runspec_found_with_marker():
    dist = SimpleNamespace(requires=["runspec; python_version >= '3.10'"])
    assert _requires_runspec(dist) is True


def test_requires_runspec_found_case_insensitive():
    dist = SimpleNamespace(requires=["Runspec>=0.1"])
    assert _requires_runspec(dist) is True


def test_requires_runspec_not_found():
    dist = SimpleNamespace(requires=["requests>=2.0", "click>=8.0"])
    assert _requires_runspec(dist) is False


def test_requires_runspec_no_false_prefix_match():
    dist = SimpleNamespace(requires=["runspec-extra>=1.0"])
    assert _requires_runspec(dist) is False


# ── _discover_installed ───────────────────────────────────────────────────────


def test_discover_installed_returns_list():
    # Smoke test — real environment may have 0 or more runspec-aware packages
    result = _discover_installed()
    assert isinstance(result, list)
    for item in result:
        assert "source" in item
        assert "runnable" in item
        assert "spec" in item


def test_discover_installed_skips_non_runspec_deps():
    # A dist without runspec as a dependency should be ignored entirely
    dist = SimpleNamespace(files=None, requires=["requests>=2.0"])
    with patch("importlib.metadata.distributions", return_value=[dist]):
        result = _discover_installed()
    assert result == []


def test_discover_installed_skips_exceptions():
    # A dist that requires runspec but has files=None raises nothing, returns []
    bad_dist = SimpleNamespace(files=None, requires=["runspec"])
    with patch("importlib.metadata.distributions", return_value=[bad_dist]):
        result = _discover_installed()
    assert result == []


def test_discover_installed_strategy1_wins(tmp_path):
    toml_content = textwrap.dedent("""\
        [tool]
        description = "A tool"
    """)
    f = _make_dist_file("runspec.toml", toml_content, tmp_path)
    dist = SimpleNamespace(files=[f], requires=["runspec>=0.1"])

    with patch("importlib.metadata.distributions", return_value=[dist]):
        result = _discover_installed()

    assert len(result) == 1
    assert result[0]["runnable"] == "tool"
