from __future__ import annotations

from pathlib import Path

from conftest import FIXTURE, fixture_runnables

from runspec_tui import catalog


def test_args_are_inferred_like_core(deploy: catalog.Runnable) -> None:
    args = {a["name"]: a for a in deploy.root.args}
    assert args["workers"]["type"] == "int" and args["workers"]["required"] is False
    assert args["ratio"]["type"] == "float"
    assert args["dry-run"]["type"] == "flag"
    assert args["target"]["required"] is True  # no default ⇒ required
    assert args["extra"]["type"] == "rest" and args["extra"]["required"] is False


def test_commands_inherit_globals_and_the_child_wins(db: catalog.Runnable) -> None:
    backup = db.root.at(["backup"])
    by_name = {a["name"]: a for a in backup.args}
    assert by_name["user"]["inherited"] is True
    assert "inherited" not in by_name["name"]
    # the command's own `host` replaces the global one
    assert by_name["host"]["default"] == "db1" and not by_name["host"].get("inherited")
    assert [a["name"] for a in backup.args] == ["user", "name", "host"]


def test_autonomy_falls_back_to_the_package_default(db: catalog.Runnable) -> None:
    assert db.autonomy == "autonomous"
    assert db.root.at(["backup"]).autonomy == "confirm"
    assert db.root.at(["vacuum"]).autonomy == "autonomous"


def test_require_command(db: catalog.Runnable, deploy: catalog.Runnable) -> None:
    assert db.root.can_run is False
    assert db.root.at(["vacuum"]).can_run is True
    assert deploy.root.can_run is True


def test_groups_merge_down_the_path(deploy: catalog.Runnable) -> None:
    assert "plan-or-token" in deploy.root.groups


def test_hidden_runnables() -> None:
    assert "hidden-helper" not in [r.name for r in fixture_runnables()]
    assert "hidden-helper" in [r.name for r in fixture_runnables(include_hidden=True)]


def test_matches_searches_name_description_package_and_commands(db: catalog.Runnable) -> None:
    assert catalog.matches(db, "")
    assert catalog.matches(db, "DATABASE")
    assert catalog.matches(db, "vacuum")
    assert catalog.matches(db, "demo-pkg")
    assert not catalog.matches(db, "kubernetes")


def test_discover_skips_missing_binaries_and_shadowed_names(monkeypatch) -> None:
    monkeypatch.setattr(catalog, "_installed_specs", lambda: [("pkg-a", FIXTURE), ("pkg-b", FIXTURE)])
    monkeypatch.setattr(catalog, "find_script", lambda name: None if name == "db" else [f"/venv/bin/{name}"])
    found = catalog.discover()
    assert [(r.package, r.name) for r in found] == [("pkg-a", "deploy")]  # db has no binary; pkg-b's deploy is shadowed
    assert [r.name for r in catalog.discover(include_hidden=True)] == ["deploy", "hidden-helper"]


def test_discover_skips_a_malformed_spec(monkeypatch, tmp_path: Path) -> None:
    bad = tmp_path / "runspec.toml"
    bad.write_text("this is = = not toml")
    monkeypatch.setattr(catalog, "_installed_specs", lambda: [("broken", bad), ("pkg-a", FIXTURE)])
    monkeypatch.setattr(catalog, "find_script", lambda name: [name])
    assert {r.package for r in catalog.discover()} == {"pkg-a"}


def test_by_package_keeps_order(runnables) -> None:
    assert list(catalog.by_package(runnables)) == ["demo-pkg"]
