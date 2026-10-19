from __future__ import annotations

from pathlib import Path

import pytest
from runspec.loader import load_raw

from runspec_tui import catalog

FIXTURE = Path(__file__).parent / "fixtures" / "runspec.toml"


def fixture_runnables(include_hidden: bool = False) -> list[catalog.Runnable]:
    raw = load_raw(FIXTURE)
    out = [catalog.build_runnable(name, "demo-pkg", FIXTURE, raw) for name in raw["runnables"]]
    return [r for r in out if r.discoverable or include_hidden]


@pytest.fixture
def runnables() -> list[catalog.Runnable]:
    return fixture_runnables()


@pytest.fixture
def deploy(runnables: list[catalog.Runnable]) -> catalog.Runnable:
    return next(r for r in runnables if r.name == "deploy")


@pytest.fixture
def db(runnables: list[catalog.Runnable]) -> catalog.Runnable:
    return next(r for r in runnables if r.name == "db")


@pytest.fixture(autouse=True)
def _quiet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing from the developer's shell may leak into a form's env hints."""
    import os

    for key in list(os.environ):
        if key.startswith("RUNSPEC_") or key in ("DB_USER", "DEPLOY_OWNER"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RUNSPEC_ENV_FILE", str(FIXTURE.parent / "no-such.runspec_env"))
