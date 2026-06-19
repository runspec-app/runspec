"""
Tests for runspec init — creates runspec.toml with a scaffold.
"""

from __future__ import annotations

import pytest

from runspec import stubgen
from runspec.cli import _sanitize_name, cmd_init

# ── _sanitize_name ────────────────────────────────────────────────────────────


def test_sanitize_name_simple():
    assert _sanitize_name("myapp") == "myapp"


def test_sanitize_name_hyphens():
    assert _sanitize_name("my-app") == "my_app"


def test_sanitize_name_spaces():
    assert _sanitize_name("My App") == "my_app"


def test_sanitize_name_leading_trailing_specials():
    assert _sanitize_name("--myapp--") == "myapp"


def test_sanitize_name_empty_falls_back():
    assert _sanitize_name("---") == "myscript"


def test_sanitize_name_mixed():
    assert _sanitize_name("Analytics-Pipeline_v2") == "analytics_pipeline_v2"


# ── creates runspec.toml when no file exists ──────────────────────────────────


def test_creates_runspec_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--name", "greeter"])

    toml = tmp_path / "runspec.toml"
    assert toml.exists()
    content = toml.read_text()
    assert "[greeter]" in content
    assert 'autonomy    = "confirm"' in content
    assert "[greeter.args]" in content


def test_creates_runspec_toml_default_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init([])

    toml = tmp_path / "runspec.toml"
    assert toml.exists()
    name = _sanitize_name(tmp_path.name)
    assert f"[{name}]" in toml.read_text()


# ── stubs wiring (A+B) ────────────────────────────────────────────────────────


def test_init_writes_placeholder_stub(tmp_path, monkeypatch):
    """init drops the sentinel placeholder so the editor nudges 'run runspec stubs'."""
    monkeypatch.chdir(tmp_path)
    cmd_init(["--name", "greeter"])

    stub = tmp_path / "typings" / "runspec" / "__init__.pyi"
    assert stub.exists()
    assert stub.read_text() == stubgen.render_sentinel_stub()


def test_init_write_project_wires_precommit_and_mypy(tmp_path, monkeypatch):
    pkg = tmp_path / "toolkit"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    cmd_init(["--name", "deploy", "--write-project"])

    # placeholder at the project root, not inside the package dir
    assert (tmp_path / "typings" / "runspec" / "__init__.pyi").exists()

    precommit = tmp_path / ".pre-commit-config.yaml"
    assert precommit.exists()
    assert "runspec stubs" in precommit.read_text()

    pyproject = (tmp_path / "pyproject.toml").read_text()
    assert "[tool.mypy]" in pyproject
    assert 'mypy_path = "typings"' in pyproject


# ── safety check: refuse if cwd already has pyproject.toml ───────────────────


def test_refuses_when_cwd_has_pyproject(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'existing'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_init(["--name", "greeter"])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "pyproject.toml" in out
    assert "--force" in out
    # And no runspec.toml was created
    assert not (tmp_path / "runspec.toml").exists()


def test_force_overrides_pyproject_check(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'existing'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cmd_init(["--name", "greeter", "--force"])
    assert (tmp_path / "runspec.toml").exists()
    assert "[greeter]" in (tmp_path / "runspec.toml").read_text()


def test_no_pyproject_no_check(tmp_path, monkeypatch):
    # Sanity: with no pyproject.toml, init proceeds without --force
    monkeypatch.chdir(tmp_path)
    cmd_init(["--name", "greeter"])
    assert (tmp_path / "runspec.toml").exists()


# ── idempotency — refuse if already initialized ──────────────────────────────


def test_refuses_if_runspec_toml_exists(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runspec.toml").write_text('[greeter]\ndescription = "hi"\n', encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        cmd_init(["--name", "greeter"])
    assert exc.value.code == 1
    assert "already exists" in capsys.readouterr().out


# ── generated files are valid TOML and parseable by runspec ──────────────────


def test_runspec_toml_is_valid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--name", "myscript"])

    from runspec.loader import load_raw

    raw = load_raw(tmp_path / "runspec.toml")
    assert "myscript" in raw["runnables"]


# ── codegen — default Python stub ────────────────────────────────────────────


def test_creates_python_stub_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--name", "greet"])

    stub = tmp_path / "greet.py"
    assert stub.exists()
    content = stub.read_text()
    assert "from runspec import parse" in content
    assert "def main():" in content
    assert 'if __name__ == "__main__"' in content


def test_python_stub_is_executable_as_script(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--name", "greet"])
    # compile-check: no syntax errors
    import ast

    ast.parse((tmp_path / "greet.py").read_text())


# ── codegen — --lang overrides ────────────────────────────────────────────────


def test_lang_typescript_generates_ts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--name", "greet", "--lang", "typescript"])

    stub = tmp_path / "greet.ts"
    assert stub.exists()
    content = stub.read_text()
    assert "import { parse } from 'runspec-node'" in content
    assert "function main()" in content
    assert "main();" in content


def test_lang_javascript_generates_js(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--name", "greet", "--lang", "javascript"])

    stub = tmp_path / "greet.js"
    assert stub.exists()
    content = stub.read_text()
    assert "require('runspec-node')" in content
    assert "main();" in content


def test_unknown_lang_exits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        cmd_init(["--name", "greet", "--lang", "ruby"])
    assert exc.value.code == 1


# ── codegen — no-overwrite behaviour ─────────────────────────────────────────


def test_does_not_overwrite_existing_stub(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "greet.py"
    existing.write_text("# existing\n", encoding="utf-8")

    cmd_init(["--name", "greet"])

    assert existing.read_text() == "# existing\n"
    assert "already exists" in capsys.readouterr().out


# ── install message ───────────────────────────────────────────────────────────


def test_install_message_shown_on_standard_init(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--name", "myapp"])
    out = capsys.readouterr().out
    assert "pip install -e ." in out
    assert "uv sync" in out
    assert "poetry install" in out
    assert "runspec local" in out


def test_pyproject_snippet_shown_on_standard_init(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--name", "greet"])
    out = capsys.readouterr().out
    assert "[project.scripts]" in out
    assert "greet" in out


# ── --example mode ────────────────────────────────────────────────────────────


def test_example_defaults_to_clean_runnable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])

    toml = tmp_path / "runspec.toml"
    assert toml.exists()
    assert "[clean]" in toml.read_text()
    assert (tmp_path / "clean.py").exists()


def test_example_with_custom_name_ignores_name(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example", "--name", "sweep"])

    toml = tmp_path / "runspec.toml"
    assert "[clean]" in toml.read_text()
    assert "[scan]" in toml.read_text()
    assert "--name ignored" in capsys.readouterr().out


def test_example_toml_has_all_arg_types(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])

    content = (tmp_path / "runspec.toml").read_text()
    assert 'type = "path"' in content
    assert 'type = "str"' in content
    assert 'type = "int"' in content
    assert 'type = "choice"' in content
    assert 'type = "flag"' in content
    assert 'autonomy    = "confirm"' in content


def test_example_toml_is_valid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])

    from runspec.loader import load_raw

    raw = load_raw(tmp_path / "runspec.toml")
    assert "clean" in raw["runnables"]
    args = raw["runnables"]["clean"]["args"]
    assert set(args) == {"directory", "pattern", "older_than", "format", "delete"}


def test_example_python_stub_syntax(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])

    import ast

    ast.parse((tmp_path / "clean.py").read_text())


def test_example_python_stub_uses_parse(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])

    content = (tmp_path / "clean.py").read_text()
    assert "from runspec import parse" in content
    assert "args = parse()" in content
    assert "args.delete" in content
    assert "args.format" in content


def test_example_install_message_shown(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])
    out = capsys.readouterr().out
    assert "pip install -e ." in out
    assert "uv sync" in out
    assert "poetry install" in out


# ── --example dual runnable (clean + scan) ────────────────────────────────────


def test_example_creates_scan_stub(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])

    assert (tmp_path / "scan.py").exists()


def test_example_scan_stub_syntax(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])

    import ast

    ast.parse((tmp_path / "scan.py").read_text())


def test_example_scan_stub_uses_parse(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])

    content = (tmp_path / "scan.py").read_text()
    assert "from runspec import parse" in content
    assert "args = parse()" in content
    assert "args.directory.value" in content


def test_example_toml_has_scan_runnable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])

    from runspec.loader import load_raw

    raw = load_raw(tmp_path / "runspec.toml")
    assert "scan" in raw["runnables"]
    scan = raw["runnables"]["scan"]
    assert scan.get("autonomy") == "autonomous"
    assert scan.get("output") == "json"


def test_example_toml_scan_has_no_delete_arg(tmp_path, monkeypatch):
    """scan is read-only — it never has a --delete flag."""
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])

    from runspec.loader import load_raw

    raw = load_raw(tmp_path / "runspec.toml")
    scan_args = raw["runnables"]["scan"].get("args", {})
    assert "delete" not in scan_args


def test_example_toml_scan_has_format_arg_defaulting_to_json(tmp_path, monkeypatch):
    """scan supports --format (text|json); default is json to match output = json."""
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])

    from runspec.loader import load_raw

    raw = load_raw(tmp_path / "runspec.toml")
    scan_args = raw["runnables"]["scan"].get("args", {})
    assert "format" in scan_args
    assert scan_args["format"]["default"] == "json"
    assert scan_args["format"]["options"] == ["text", "json"]


def test_example_next_steps_shows_demo_prep(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])
    out = capsys.readouterr().out
    assert "touch -t 202401010000 report.tmp cache.tmp session.tmp" in out
    assert "scan" in out
    assert "clean --delete" in out


def test_example_pyproject_snippet_includes_both_entry_points(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cmd_init(["--example"])
    out = capsys.readouterr().out
    assert "clean" in out
    assert "scan" in out
    assert ".clean:main" in out
    assert ".scan:main" in out


# ── --write-project: file generation ─────────────────────────────────────────


def test_write_project_creates_pyproject(tmp_path, monkeypatch):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])
    assert (tmp_path / "pyproject.toml").exists()


def test_write_project_creates_init_py(tmp_path, monkeypatch):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])
    assert (pkg / "__init__.py").exists()


def test_write_project_pyproject_content(tmp_path, monkeypatch):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])

    content = (tmp_path / "pyproject.toml").read_text()
    assert 'name            = "greet"' in content
    assert 'dependencies    = ["runspec"]' in content
    assert 'greet = "mypkg.greet:main"' in content
    assert 'build-backend = "hatchling.build"' in content
    assert 'requires      = ["hatchling"]' in content
    assert 'packages = ["mypkg"]' in content


def test_write_project_pyproject_is_valid_toml(tmp_path, monkeypatch):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])

    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib
    with open(tmp_path / "pyproject.toml", "rb") as f:
        parsed = tomllib.load(f)
    assert parsed["project"]["name"] == "greet"
    assert parsed["project"]["scripts"]["greet"] == "mypkg.greet:main"
    assert parsed["build-system"]["build-backend"] == "hatchling.build"


def test_write_project_skips_existing_pyproject(tmp_path, monkeypatch, capsys):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    existing = tmp_path / "pyproject.toml"
    existing.write_text("[project]\nname = 'existing'\n", encoding="utf-8")

    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])

    assert existing.read_text() == "[project]\nname = 'existing'\n"
    out = capsys.readouterr().out
    assert "already exists" in out
    assert "[project.scripts]" in out
    assert "greet" in out


def test_write_project_with_example(tmp_path, monkeypatch):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    cmd_init(["--example", "--write-project", "--project-dir", str(tmp_path)])

    assert (tmp_path / "pyproject.toml").exists()
    assert (pkg / "clean.py").exists()
    assert (pkg / "scan.py").exists()
    assert (pkg / "__init__.py").exists()
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'clean = "mypkg.clean:main"' in content
    assert 'scan  = "mypkg.scan:main"' in content


def test_write_project_skips_existing_init_py(tmp_path, monkeypatch, capsys):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    existing = pkg / "__init__.py"
    existing.write_text("# existing\n", encoding="utf-8")

    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])

    assert existing.read_text() == "# existing\n"
    assert "already exists" in capsys.readouterr().out


def test_write_project_creates_gitignore(tmp_path, monkeypatch):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])
    assert (tmp_path / ".gitignore").exists()


def test_write_project_gitignore_content(tmp_path, monkeypatch):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])
    content = (tmp_path / ".gitignore").read_text()
    assert "*.egg-info/" in content
    assert "__pycache__/" in content
    assert ".venv/" in content
    assert ".DS_Store" in content


def test_write_project_skips_existing_gitignore(tmp_path, monkeypatch, capsys):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    (tmp_path / ".gitignore").write_text("# existing\n", encoding="utf-8")
    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])
    assert (tmp_path / ".gitignore").read_text() == "# existing\n"
    assert ".gitignore already exists" in capsys.readouterr().out


def test_write_project_creates_claude_md(tmp_path, monkeypatch):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])
    assert (tmp_path / "CLAUDE.md").exists()


def test_write_project_claude_md_content(tmp_path, monkeypatch):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])
    content = (tmp_path / "CLAUDE.md").read_text()
    assert "runspec" in content
    assert "runspec.toml" in content
    assert "mypkg" in content
    assert "autonomy" in content


def test_write_project_skips_existing_claude_md(tmp_path, monkeypatch, capsys):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    (tmp_path / "CLAUDE.md").write_text("# existing\n", encoding="utf-8")
    cmd_init(["--name", "greet", "--write-project", "--project-dir", str(tmp_path)])
    assert (tmp_path / "CLAUDE.md").read_text() == "# existing\n"
    assert "CLAUDE.md already exists" in capsys.readouterr().out


def test_write_project_install_path_in_next_steps(tmp_path, monkeypatch, capsys):
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    monkeypatch.chdir(pkg)
    project_root = str(tmp_path)
    cmd_init(["--name", "greet", "--write-project", "--project-dir", project_root])

    out = capsys.readouterr().out
    assert f"pip install -e {project_root}" in out
    assert f"run from {project_root}" in out


# ── main() — top-level dispatch ───────────────────────────────────────────────


def test_main_version_long(monkeypatch, capsys):
    from runspec.cli import main

    monkeypatch.setattr("sys.argv", ["runspec", "--version"])
    main()
    out = capsys.readouterr().out
    assert "runspec" in out
    # Version line contains at least one digit (the actual version string)
    assert any(c.isdigit() for c in out)


def test_main_version_short(monkeypatch, capsys):
    from runspec.cli import main

    monkeypatch.setattr("sys.argv", ["runspec", "-V"])
    main()
    out = capsys.readouterr().out
    assert "runspec" in out
    assert any(c.isdigit() for c in out)


def test_main_version_takes_precedence_over_help(monkeypatch, capsys):
    """--version is checked before --help; first arg wins."""
    from runspec.cli import main

    monkeypatch.setattr("sys.argv", ["runspec", "--version"])
    main()
    out = capsys.readouterr().out
    # Should be a single-line version, not the full help block
    assert "Usage:" not in out
    assert "Commands:" not in out
