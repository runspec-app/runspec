"""
test_config.py — the TOML config loader: file load, env-apply precedence, the
session-secret command, and team parsing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from console_room.config import (
    apply_config_to_env,
    config_allowed_domains,
    config_teams,
    find_config_path,
    load_config,
    resolve_session_secret,
)


def _write(tmp_path, text):
    p = tmp_path / "console_room.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_missing_is_empty(tmp_path):
    assert load_config(tmp_path / "nope.toml") == {}
    assert load_config(None) == {}


def test_load_broken_is_empty(tmp_path):
    p = _write(tmp_path, "this is = = not toml")
    assert load_config(p) == {}


def test_find_config_path_precedence(monkeypatch):
    # CLI wins over env.
    monkeypatch.setenv("CONSOLE_ROOM_CONFIG", "/from/env.toml")
    assert find_config_path("/from/cli.toml") == Path("/from/cli.toml")
    # Env when no CLI.
    assert find_config_path(None) == Path("/from/env.toml")


def test_apply_config_to_env_sets_unset(tmp_path, monkeypatch):
    for var in ("CONSOLE_ROOM_HOST", "CONSOLE_ROOM_PORT", "CONSOLE_ROOM_PUBLIC_URL"):
        monkeypatch.delenv(var, raising=False)
    config = load_config(
        _write(
            tmp_path,
            '[server]\nhost = "0.0.0.0"\nport = 9443\n'
            'public_url = "https://rooms.corp.com"\n',
        )
    )
    apply_config_to_env(config)
    import os

    assert os.environ["CONSOLE_ROOM_HOST"] == "0.0.0.0"
    assert os.environ["CONSOLE_ROOM_PORT"] == "9443"
    assert os.environ["CONSOLE_ROOM_PUBLIC_URL"] == "https://rooms.corp.com"


def test_apply_config_does_not_clobber_real_env(tmp_path, monkeypatch):
    # A real env var (or CLI, which feeds env) must always win over the file.
    monkeypatch.setenv("CONSOLE_ROOM_PORT", "8080")
    config = load_config(_write(tmp_path, "[server]\nport = 9443\n"))
    apply_config_to_env(config)
    import os

    assert os.environ["CONSOLE_ROOM_PORT"] == "8080"


def test_session_secret_literal(tmp_path):
    config = load_config(_write(tmp_path, '[auth]\nsession_secret = "abc123"\n'))
    assert resolve_session_secret(config) == "abc123"


def test_session_secret_command(tmp_path):
    config = load_config(
        _write(tmp_path, '[auth]\nsession_secret_command = "echo from-cmd"\n')
    )
    assert resolve_session_secret(config) == "from-cmd"


def test_session_secret_literal_beats_command(tmp_path):
    config = load_config(
        _write(
            tmp_path,
            '[auth]\nsession_secret = "literal"\nsession_secret_command = "echo cmd"\n',
        )
    )
    assert resolve_session_secret(config) == "literal"


def test_session_secret_command_failure_is_fatal(tmp_path):
    config = load_config(
        _write(tmp_path, '[auth]\nsession_secret_command = "exit 3"\n')
    )
    with pytest.raises(SystemExit):
        resolve_session_secret(config)


def test_session_secret_none_when_unset(tmp_path):
    assert resolve_session_secret(load_config(_write(tmp_path, "[server]\n"))) is None


def test_apply_config_injects_secret_command(tmp_path, monkeypatch):
    monkeypatch.delenv("CONSOLE_ROOM_SESSION_SECRET", raising=False)
    config = load_config(
        _write(tmp_path, '[auth]\nsession_secret_command = "echo injected"\n')
    )
    apply_config_to_env(config)
    import os

    assert os.environ["CONSOLE_ROOM_SESSION_SECRET"] == "injected"


def test_config_teams_parse_and_slug(tmp_path):
    config = load_config(
        _write(
            tmp_path,
            '[[teams]]\nname = "Engineering"\n'
            '[[teams]]\nname = "Field Ops"\n'
            '[[teams]]\nslug = "fin"\nname = "Finance"\n',
        )
    )
    teams = config_teams(config)
    assert [(t.slug, t.name) for t in teams] == [
        ("engineering", "Engineering"),
        ("field-ops", "Field Ops"),
        ("fin", "Finance"),
    ]


def test_config_teams_skips_nameless(tmp_path):
    config = load_config(
        _write(tmp_path, '[[teams]]\nslug = "x"\n[[teams]]\nname=""\n')
    )
    assert config_teams(config) == []


def test_config_teams_empty_when_absent(tmp_path):
    assert config_teams(load_config(_write(tmp_path, "[server]\n"))) == []


def test_config_allowed_domains_array(tmp_path):
    config = load_config(
        _write(
            tmp_path,
            '[auth]\nallowed_domains = ["@Company1.com", "company1.company2.com",'
            ' "company1.com"]\n',
        )
    )
    assert config_allowed_domains(config) == [
        "company1.com",
        "company1.company2.com",
    ]


def test_config_allowed_domains_string(tmp_path):
    config = load_config(
        _write(tmp_path, '[auth]\nallowed_domains = "company1.com, company2.com"\n')
    )
    assert config_allowed_domains(config) == ["company1.com", "company2.com"]


def test_config_allowed_domains_empty_when_absent(tmp_path):
    assert config_allowed_domains(load_config(_write(tmp_path, "[server]\n"))) == []


def test_apply_config_sets_allowed_domains(tmp_path, monkeypatch):
    monkeypatch.delenv("CONSOLE_ROOM_ALLOWED_DOMAINS", raising=False)
    config = load_config(
        _write(tmp_path, '[auth]\nallowed_domains = ["company1.com", "company2.com"]\n')
    )
    apply_config_to_env(config)
    import os

    assert os.environ["CONSOLE_ROOM_ALLOWED_DOMAINS"] == "company1.com,company2.com"


def test_apply_config_does_not_clobber_allowed_domains_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONSOLE_ROOM_ALLOWED_DOMAINS", "real.com")
    config = load_config(
        _write(tmp_path, '[auth]\nallowed_domains = ["company1.com"]\n')
    )
    apply_config_to_env(config)
    import os

    assert os.environ["CONSOLE_ROOM_ALLOWED_DOMAINS"] == "real.com"
