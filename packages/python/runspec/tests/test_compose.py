"""
Tests for compose.py — ``include`` and ``shared-args`` / ``use``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from runspec.compose import has_include, resolve
from runspec.loader import load_raw, load_text, normalise, read_resolved
from runspec.parser import parse

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

REPO = Path(__file__).parents[4]
FIXTURES = REPO / "tests" / "integration" / "fixtures"
COMPOSED = FIXTURES / "composed"


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root / "runspec.toml"


def _repo_specs() -> list[Path]:
    skip = {"node_modules", ".venv", "dist", "build"}
    return sorted(p for p in REPO.glob("**/runspec.toml") if not skip & set(p.parts) and COMPOSED not in p.parents)


# ── Existing specs are untouched ──────────────────────────────────────────────


class TestExistingSpecsUnchanged:
    """Every runspec.toml in the repo uses neither feature, so resolving it must
    be a no-op and loading it must give exactly what the old loader gave."""

    def test_found_specs(self):
        assert len(_repo_specs()) > 20

    @pytest.mark.parametrize("path", _repo_specs(), ids=lambda p: str(p.relative_to(REPO)))
    def test_resolve_is_identity(self, path):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert resolve(data) == data

    @pytest.mark.parametrize("path", _repo_specs(), ids=lambda p: str(p.relative_to(REPO)))
    def test_load_raw_matches_plain_normalise(self, path):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        try:
            expected = normalise(data)
        except ValueError:
            pytest.skip("deliberately invalid fixture")
        assert load_raw(path) == expected


class TestComplianceFixture:
    """Shared with the Node pack: the composed fixture equals its flat twin."""

    def test_composed_equals_flat(self):
        assert load_raw(COMPOSED / "runspec.toml") == load_raw(FIXTURES / "composed_flat.toml")


# ── include ───────────────────────────────────────────────────────────────────

BASH_SCRIPTS = {
    "runspec.toml": """
[bash-scripts]
description = "Collection of bash scripts"
require-command = true

[bash-scripts.shared-args]
cluster = {type = "str", default = "main", description = "Cluster to act on"}

[bash-scripts.commands.europe]
include = "scripts/europe/europe.toml"
""",
    "scripts/europe/europe.toml": """
description = "Scripts for the European estate"
require-command = true

[commands.find-nodes]
include = "find-nodes.toml"

[commands.delete-nodes]
include = "delete-nodes.toml"
""",
    "scripts/europe/find-nodes.toml": """
description = "Find nodes matching a pattern"
autonomy = "autonomous"

[args]
pattern = {description = "Node name glob"}
cluster = {use = "cluster"}
""",
    "scripts/europe/delete-nodes.toml": """
description = "Delete nodes"
autonomy = "confirm"

[args]
pattern = {description = "Node name glob"}
cluster = {use = "cluster", required = true}
""",
}


class TestInclude:
    def test_nested_includes_resolve(self, tmp_path):
        spec = load_raw(_write(tmp_path, BASH_SCRIPTS))["runnables"]["bash-scripts"]
        europe = spec["commands"]["europe"]
        assert europe["description"] == "Scripts for the European estate"
        assert europe["require_command"] is True
        find = europe["commands"]["find-nodes"]
        assert find["autonomy"] == "autonomous"
        assert find["args"]["cluster"]["default"] == "main"
        assert find["args"]["cluster"]["description"] == "Cluster to act on"
        delete = europe["commands"]["delete-nodes"]
        assert delete["args"]["cluster"]["required"] is True
        assert delete["args"]["cluster"]["default"] == "main"

    def test_same_as_single_file(self, tmp_path):
        split = load_raw(_write(tmp_path / "split", BASH_SCRIPTS))
        single = load_raw(
            _write(
                tmp_path / "single",
                {
                    "runspec.toml": """
[bash-scripts]
description = "Collection of bash scripts"
require-command = true

[bash-scripts.commands.europe]
description = "Scripts for the European estate"
require-command = true

[bash-scripts.commands.europe.commands.find-nodes]
description = "Find nodes matching a pattern"
autonomy = "autonomous"

[bash-scripts.commands.europe.commands.find-nodes.args]
pattern = {description = "Node name glob"}
cluster = {type = "str", default = "main", description = "Cluster to act on"}

[bash-scripts.commands.europe.commands.delete-nodes]
description = "Delete nodes"
autonomy = "confirm"

[bash-scripts.commands.europe.commands.delete-nodes.args]
pattern = {description = "Node name glob"}
cluster = {type = "str", default = "main", description = "Cluster to act on", required = true}
"""
                },
            )
        )
        assert split == single

    def test_parse_runs_through_includes(self, tmp_path):
        cfg = _write(tmp_path, BASH_SCRIPTS)
        args = parse(
            script_name="bash-scripts",
            argv=["europe", "delete-nodes", "--pattern", "web*", "--cluster", "eu-1"],
            config_path=cfg,
        )
        assert args.runspec_command_path == ["europe", "delete-nodes"]
        assert args.pattern.value == "web*"
        assert args.cluster.value == "eu-1"

    def test_runnable_level_include(self, tmp_path):
        cfg = _write(
            tmp_path,
            {
                "runspec.toml": '[disk-usage]\ninclude = "runnables/disk-usage.toml"\n',
                "runnables/disk-usage.toml": 'description = "Show disk usage"\n[args]\npath = {default = "/"}\n',
            },
        )
        spec = load_raw(cfg)["runnables"]["disk-usage"]
        assert spec["description"] == "Show disk usage"
        assert spec["args"]["path"]["default"] == "/"

    def test_paths_relative_to_including_file(self, tmp_path):
        cfg = _write(
            tmp_path,
            {
                "runspec.toml": '[r]\n[r.commands.a]\ninclude = "a/a.toml"\n',
                "a/a.toml": '[commands.b]\ninclude = "../b/b.toml"\n',
                "b/b.toml": 'description = "from b"\n',
            },
        )
        assert load_raw(cfg)["runnables"]["r"]["commands"]["a"]["commands"]["b"]["description"] == "from b"

    def test_chained_include(self, tmp_path):
        cfg = _write(
            tmp_path,
            {
                "runspec.toml": '[r]\ninclude = "one.toml"\n',
                "one.toml": 'description = "one"\ninclude = "two.toml"\n',
                "two.toml": 'autonomy = "autonomous"\n',
            },
        )
        spec = load_raw(cfg)["runnables"]["r"]
        assert (spec["description"], spec["autonomy"]) == ("one", "autonomous")

    def test_meta_survives_for_emit(self, tmp_path):
        cfg = _write(
            tmp_path,
            {
                "runspec.toml": '[r]\ninclude = "r.toml"\n',
                "r.toml": '[meta.rundeck]\nschedule = "0 2 * * *"\n',
            },
        )
        assert read_resolved(cfg)["r"]["meta"]["rundeck"]["schedule"] == "0 2 * * *"

    def test_cycle_is_an_error(self, tmp_path):
        cfg = _write(
            tmp_path,
            {
                "runspec.toml": '[r]\n[r.commands.a]\ninclude = "a.toml"\n',
                "a.toml": '[commands.b]\ninclude = "b.toml"\n',
                "b.toml": '[commands.c]\ninclude = "a.toml"\n',
            },
        )
        with pytest.raises(ValueError, match=r"cycle: a\.toml → b\.toml → a\.toml"):
            load_raw(cfg)

    def test_including_runspec_toml_is_a_cycle(self, tmp_path):
        cfg = _write(tmp_path, {"runspec.toml": '[r]\ninclude = "runspec.toml"\n'})
        with pytest.raises(ValueError, match="cycle"):
            load_raw(cfg)

    def test_depth_cap(self, tmp_path):
        files = {"runspec.toml": '[r]\ninclude = "f1.toml"\n'}
        for i in range(1, 11):
            files[f"f{i}.toml"] = f'include = "f{i + 1}.toml"\n'
        files["f11.toml"] = 'description = "deep"\n'
        with pytest.raises(ValueError, match="more than 8 files deep"):
            load_raw(_write(tmp_path, files))

    def test_duplicate_key_is_an_error(self, tmp_path):
        cfg = _write(
            tmp_path,
            {
                "runspec.toml": '[r]\ndescription = "here"\ninclude = "r.toml"\n',
                "r.toml": 'description = "there"\n',
            },
        )
        with pytest.raises(ValueError, match="sets description both in runspec.toml and in its include r.toml"):
            load_raw(cfg)

    def test_missing_file(self, tmp_path):
        cfg = _write(tmp_path, {"runspec.toml": '[r]\ninclude = "nope.toml"\n'})
        with pytest.raises(ValueError, match="nope.toml.*not found"):
            load_raw(cfg)

    def test_invalid_toml_in_fragment(self, tmp_path):
        cfg = _write(tmp_path, {"runspec.toml": '[r]\ninclude = "bad.toml"\n', "bad.toml": "x = = 1\n"})
        with pytest.raises(ValueError, match="bad.toml is not valid TOML"):
            load_raw(cfg)

    @pytest.mark.parametrize(
        "path, message",
        [
            ("../outside.toml", "outside the package"),
            ("a/../../outside.toml", "outside the package"),
            ("/etc/x.toml", "relative path"),
            ("C:/x.toml", "relative path"),
            ("a\\\\b.toml", "use '/'"),
            ("notes.txt", "must name a .toml file"),
        ],
    )
    def test_bad_paths(self, tmp_path, path, message):
        cfg = _write(tmp_path, {"runspec.toml": f'[r]\ninclude = "{path}"\n'})
        with pytest.raises(ValueError, match=message):
            load_raw(cfg)

    def test_non_string_include(self, tmp_path):
        cfg = _write(tmp_path, {"runspec.toml": "[r]\ninclude = 3\n"})
        with pytest.raises(ValueError, match="must be a path string"):
            load_raw(cfg)

    def test_symlink_out_of_package_refused(self, tmp_path):
        outside = tmp_path / "outside.toml"
        outside.write_text('description = "x"\n')
        pkg = tmp_path / "pkg"
        cfg = _write(pkg, {"runspec.toml": '[r]\ninclude = "link.toml"\n'})
        try:
            (pkg / "link.toml").symlink_to(outside)
        except OSError:
            pytest.skip("symlinks not available")
        with pytest.raises(ValueError, match="outside the package directory"):
            load_raw(cfg)

    def test_load_text_with_reader(self):
        files = {"cmd/x.toml": 'description = "x"\n'}
        spec = load_text('[r]\n[r.commands.x]\ninclude = "cmd/x.toml"\n', files.get)
        assert spec["runnables"]["r"]["commands"]["x"]["description"] == "x"

    def test_load_text_without_reader(self):
        with pytest.raises(ValueError, match="no file access"):
            load_text('[r]\ninclude = "x.toml"\n')

    def test_has_include(self):
        assert has_include('[r]\ninclude = "x.toml"\n')
        assert has_include('[r]\n  include="x.toml"\n')
        assert not has_include('[r]\ndescription = "include = x"\n')
        assert not has_include("[r]\nincluded = 1\n")


# ── shared-args / use ─────────────────────────────────────────────────────────


class TestSharedArgs:
    def test_runnable_level(self):
        spec = load_text(
            """
[gitlab.shared-args]
project = {type = "str", required = false, env = "GITLAB_PROJECT", description = "Project id or path"}

[gitlab.commands.get-issue.args]
project = {use = "project"}

[gitlab.commands.create-issue.args]
project = {use = "project", required = true}
source-project = {use = "project"}
"""
        )["runnables"]["gitlab"]
        get_issue = spec["commands"]["get-issue"]["args"]["project"]
        assert get_issue["required"] is False
        assert get_issue["env"] == ["GITLAB_PROJECT"]
        create = spec["commands"]["create-issue"]["args"]
        assert create["project"]["required"] is True
        assert create["project"]["description"] == "Project id or path"
        assert create["source-project"]["name"] == "source-project"
        assert create["source-project"]["env"] == ["GITLAB_PROJECT"]

    def test_config_level_across_runnables(self):
        spec = load_text(
            """
[config.shared-args]
proxy = {required = false, description = "HTTP proxy"}

[cert-check.args]
proxy = {use = "proxy"}

[http-check.args]
proxy = {use = "proxy"}
"""
        )
        for name in ("cert-check", "http-check"):
            assert spec["runnables"][name]["args"]["proxy"]["description"] == "HTTP proxy"

    def test_nearest_definition_wins(self):
        spec = load_text(
            """
[config.shared-args]
limit = 10

[r.shared-args]
limit = 20

[r.commands.a.shared-args]
limit = 30

[r.commands.a.args]
limit = {use = "limit"}

[r.commands.b.args]
limit = {use = "limit"}
"""
        )["runnables"]["r"]["commands"]
        assert spec["a"]["args"]["limit"]["default"] == 30
        assert spec["b"]["args"]["limit"]["default"] == 20

    def test_runnable_args_can_use(self):
        spec = load_text('[r.shared-args]\nverbose = false\n[r.args]\nverbose = {use = "verbose"}\n')
        assert spec["runnables"]["r"]["args"]["verbose"]["default"] is False

    def test_shared_args_table_removed(self):
        data = tomllib.loads('[config.shared-args]\nx = 1\n[r.shared-args]\ny = 2\n[r.args]\nx = {use = "x"}\n')
        out = resolve(data)
        assert "shared-args" not in out["config"]
        assert "shared-args" not in out["r"]
        assert out["r"]["args"]["x"] == {"default": 1}

    def test_unused_definitions_are_fine(self):
        spec = load_text("[r.shared-args]\nx = 1\n[r.args]\ny = 2\n")
        assert list(spec["runnables"]["r"]["args"]) == ["y"]

    def test_unknown_name_suggests(self):
        with pytest.raises(ValueError, match="(?s)unknown shared arg 'projet'.*Did you mean 'project'"):
            load_text('[r.shared-args]\nproject = "x"\n[r.args]\np = {use = "projet"}\n')

    def test_unknown_name_without_any_table(self):
        with pytest.raises(ValueError, match=r"No \[shared-args\] table"):
            load_text('[r.args]\np = {use = "project"}\n')

    def test_sibling_scope_not_visible(self):
        with pytest.raises(ValueError, match="unknown shared arg 'x'"):
            load_text('[r.commands.a.shared-args]\nx = 1\n[r.commands.b.args]\nx = {use = "x"}\n')

    def test_definition_cannot_use(self):
        with pytest.raises(ValueError, match="can't itself use"):
            load_text('[r.shared-args]\na = 1\nb = {use = "a"}\n')

    def test_use_must_be_a_name(self):
        with pytest.raises(ValueError, match="use must name a shared arg"):
            load_text("[r.shared-args]\na = 1\n[r.args]\na = {use = 3}\n")

    def test_definitions_not_shared_between_uses(self):
        spec = load_text('[r.shared-args]\nm = {options = ["a", "b"]}\n[r.commands.x.args]\nm = {use = "m"}\n[r.commands.y.args]\nm = {use = "m"}\n')["runnables"]["r"]["commands"]
        spec["x"]["args"]["m"]["options"].append("c")
        assert spec["y"]["args"]["m"]["options"] == ["a", "b"]
