"""
Tests for rundeckgen — rendering Rundeck job definitions (JSON) from runspec
runnables (`runspec emit --rundeck`).
"""

from __future__ import annotations

import json

from runspec import rundeckgen

# ── name helpers ──────────────────────────────────────────────────────────────


def test_rd_option_token():
    assert rundeckgen.rd_option_token("target") == "@option.target@"
    assert rundeckgen.rd_option_token("node-filter") == "@option.node-filter@"


def test_secret_env_var_keys_on_runnable_not_leaf():
    # Mirrors parser._apply_env / serve._env_key: RUNSPEC_<RUNNABLE>_ARG_<NAME>,
    # keyed on the top-level runnable, hyphens -> underscores, uppercased.
    assert rundeckgen.secret_env_var("deploy", "token") == "RUNSPEC_DEPLOY_ARG_TOKEN"
    assert rundeckgen.secret_env_var("runspec-jira", "jira-pat") == "RUNSPEC_RUNSPEC_JIRA_ARG_JIRA_PAT"


def test_job_filename():
    assert rundeckgen.job_filename("cron_add") == "cron_add.json"


# ── option rendering ──────────────────────────────────────────────────────────


def test_option_string_with_default():
    opt = rundeckgen.render_option("pattern", {"type": "str", "default": "*.tmp", "description": "Glob"})
    assert opt == {"name": "pattern", "description": "Glob", "value": "*.tmp"}


def test_option_int_gets_validating_regex_and_stringified_default():
    opt = rundeckgen.render_option("depth", {"type": "int", "default": 3})
    assert opt["value"] == "3"
    assert opt["regex"] == r"^-?\d+$"


def test_option_float_regex():
    opt = rundeckgen.render_option("ratio", {"type": "float", "default": 1.5})
    assert opt["regex"] == r"^-?\d+(\.\d+)?$"
    assert opt["value"] == "1.5"


def test_option_required_when_no_default():
    opt = rundeckgen.render_option("target", {"type": "str", "required": True})
    assert opt["required"] is True
    assert "value" not in opt


def test_option_choice_is_enforced_values():
    opt = rundeckgen.render_option("format", {"type": "choice", "options": ["text", "json"], "default": "json"})
    assert opt["values"] == ["text", "json"]
    assert opt["enforced"] is True
    assert opt["value"] == "json"


def test_option_flag_is_true_false_enforced():
    opt = rundeckgen.render_option("verbose", {"type": "flag", "default": False})
    assert opt["values"] == ["true", "false"]
    assert opt["enforced"] is True
    assert opt["value"] == "false"


def test_option_flag_default_true():
    opt = rundeckgen.render_option("cache", {"type": "flag", "default": True})
    assert opt["value"] == "true"


def test_option_password_is_secure_exposed_no_value():
    opt = rundeckgen.render_option("token", {"type": "password", "required": True, "description": "API token"})
    assert opt["secure"] is True
    assert opt["valueExposed"] is True  # substituted into the script via @option.token@
    assert opt["required"] is True
    assert "value" not in opt  # a secret never carries a default value


def test_option_description_notes_constraints():
    opt = rundeckgen.render_option("name", {"type": "str", "required": True, "position": 1, "description": "Host"})
    assert "positional #1" in opt["description"]


def test_option_description_notes_rest_range_pattern():
    rest = rundeckgen.render_option("extra", {"type": "rest"})
    assert "after --" in rest["description"]
    ranged = rundeckgen.render_option("n", {"type": "int", "range": [1, 9], "description": "N"})
    assert "range 1–9" in ranged["description"]
    patt = rundeckgen.render_option("id", {"type": "str", "pattern": "^x", "description": "ID"})
    assert "pattern ^x" in patt["description"]


def test_option_multiple_list_default_is_delimiter_joined():
    """A list default is written the way Rundeck's multi-valued option holds it
    — joined by its delimiter — never Python's list repr."""
    arg = {"type": "choice", "options": ["Missing", "Stale", "Ok"], "multiple": True, "default": ["Missing", "Stale"]}
    opt = rundeckgen.render_option("states", arg)
    assert opt["multivalued"] is True
    assert opt["delimiter"] == ","
    assert opt["values"] == ["Missing", "Stale", "Ok"]
    assert opt["enforced"] is True
    assert opt["value"] == "Missing,Stale"
    assert 'several values separated by ","' in opt["description"]


def test_option_multiple_uses_the_args_delimiter_and_no_numeric_regex():
    opt = rundeckgen.render_option("ports", {"type": "int", "multiple": True, "delimiter": ";", "default": [80, 443]})
    assert opt["delimiter"] == ";"
    assert opt["value"] == "80;443"
    assert "regex" not in opt  # would test the joined text; runspec checks each value


def test_option_multiple_scalar_empty_and_no_default():
    assert rundeckgen.render_option("a", {"type": "str", "multiple": True, "default": "one"})["value"] == "one"
    assert "value" not in rundeckgen.render_option("a", {"type": "str", "multiple": True, "default": []})
    assert "value" not in rundeckgen.render_option("a", {"type": "str", "multiple": True, "required": True})


def test_option_multiple_line_break_delimiter_falls_back_to_comma():
    # A line break can't reach the script (the guard refuses it).
    assert rundeckgen.render_option("a", {"type": "str", "multiple": True, "delimiter": "\n"})["delimiter"] == ","


def test_option_multiple_default_value_containing_delimiter_is_refused():
    import pytest

    with pytest.raises(ValueError, match="delimiter"):
        rundeckgen.render_option("a", {"type": "str", "multiple": True, "default": ["x,y", "z"]})


def test_option_password_and_rest_are_never_multivalued():
    assert "multivalued" not in rundeckgen.render_option("t", {"type": "password", "multiple": True})
    assert "multivalued" not in rundeckgen.render_option("r", {"type": "rest", "multiple": True})


# ── script step ───────────────────────────────────────────────────────────────


def _script(runnable, args, command_path=None, invocation="/opt/app/.venv/bin/app"):
    return rundeckgen.render_script(runnable, command_path or [], args, invocation)


def test_script_assigns_every_arg_from_option_token():
    args = {
        "target": {"type": "str"},
        "verbose": {"type": "flag", "default": False},
        "first": {"type": "str", "position": 1},
        "extra": {"type": "rest"},
        "token": {"type": "password"},
    }
    s = _script("deploy", args)
    for name, var in [("target", "TARGET"), ("verbose", "VERBOSE"), ("first", "FIRST"), ("extra", "EXTRA"), ("token", "TOKEN")]:
        assert f"IFS= read -r RUNSPEC_DEPLOY_ARG_{var} <<'RUNSPEC_VALUE'\nx@option.{name}@\nRUNSPEC_VALUE\n" in s
        assert f"RUNSPEC_DEPLOY_ARG_{var}=${{RUNSPEC_DEPLOY_ARG_{var}#x}}" in s


def test_script_never_reads_rd_option_env_vars():
    s = _script("deploy", {"target": {"type": "str"}, "token": {"type": "password"}})
    assert "RD_OPTION" not in s


def test_script_puts_no_value_on_the_command_line():
    s = _script("deploy", {"target": {"type": "str"}, "token": {"type": "password"}}, command_path=["start"])
    assert "--target" not in s and "--token" not in s
    assert s.strip().endswith("exec /opt/app/.venv/bin/app start")


def test_script_exports_only_non_empty_values():
    s = _script("deploy", {"target": {"type": "str"}})
    assert 'if [ -n "${!rs_var}" ]; then export "$rs_var"; else unset "$rs_var"; fi' in s


def test_script_no_args_just_execs():
    s = _script("deploy", {}, command_path=["start"])
    assert "for rs_var" not in s
    assert s.strip().endswith("exec /opt/app/.venv/bin/app start")


def test_script_run_as_sends_env_frame_on_stdin_to_venv_python():
    s = rundeckgen.render_script("deploy", ["start"], {"token": {"type": "password"}}, "/opt/app/.venv/bin/deploy", run_as="svc", venv="/opt/app/.venv")
    assert "rs_user=svc\n" in s
    assert 'rs_frame RUNSPEC_DEPLOY_ARG_TOKEN | sudo -n -u "$rs_user" /opt/app/.venv/bin/python -c "$RS_PRELUDE" /opt/app/.venv/bin/deploy start' in s
    # sudo resets the environment, so values ride stdin; the only export is the
    # no-sudo branch taken when the Rundeck user already is the account.
    assert s.count("export") == 1
    assert 'if [ "$rs_user" = "$(id -un)" ]; then' in s


def test_script_run_as_without_venv_uses_shell_prelude():
    s = rundeckgen.render_script("deploy", [], {"x": {"type": "str"}}, "deploy", run_as="svc")
    assert '| sudo -n -u "$rs_user" sh -c "$RS_PRELUDE" sh deploy' in s
    assert "command -v" not in s  # sudo's PATH isn't the Rundeck user's


def test_script_run_as_no_args_execs_sudo():
    s = rundeckgen.render_script("deploy", [], {}, "/opt/v/bin/deploy", run_as="svc", venv="/opt/v")
    assert s.strip().endswith('exec sudo -n -u "$rs_user" /opt/v/bin/deploy')


def test_script_run_as_rejects_shell_metacharacters():
    import pytest

    with pytest.raises(ValueError):
        rundeckgen.render_script("deploy", [], {}, "deploy", run_as="svc; id")


def _run_substituted(tmp_path, script, values, env=None):
    """Substitute tokens the way pre-5.20 Rundeck does (raw text, no escaping),
    write the script to a file and run it with /bin/bash like the job's
    scriptInterpreter does."""
    import re
    import subprocess

    script = re.sub(r"@option\.([\w-]+)@", lambda m: values[m.group(1)], script)
    path = tmp_path / "rd-script.sh"
    path.write_text(script)
    return subprocess.run(["bash", str(path)], capture_output=True, text=True, cwd=tmp_path, env=env)


def _show_binary(tmp_path):
    import sys

    show = tmp_path / "bin" / "show"
    show.parent.mkdir(exist_ok=True)
    show.write_text(f"#!{sys.executable}\nimport json, os, sys\nprint(json.dumps([{{k: v for k, v in os.environ.items() if k.startswith('RUNSPEC_SHOW_')}}, sys.argv[1:]]))\n")
    show.chmod(0o755)
    return show


def _needs_bash():
    import shutil

    import pytest

    if not shutil.which("bash") or not shutil.which("od") or not shutil.which("awk"):
        pytest.skip("needs bash + od + awk")


def test_script_values_round_trip_through_bash(tmp_path):
    """Raw (unescaped) substitution: every single-line value reaches the
    runnable's env byte for byte, as data, and none is in argv."""
    _needs_bash()
    show = _show_binary(tmp_path)
    values = {
        "plain": "3",
        "tricky": "a b'c\"$(touch pwned) `touch pwned`; touch pwned \\ ü  ",
        "delim": "RUNSPEC_VALUE",
        "unset": "",
    }
    for venv in (None, str(tmp_path)):
        script = rundeckgen.render_script("show", ["sub"], {k: {"type": "str"} for k in values}, str(show), venv=venv)
        out = _run_substituted(tmp_path, script, values)
        assert out.returncode == 0, out.stderr
        env, argv = json.loads(out.stdout)
        assert env == {"RUNSPEC_SHOW_ARG_PLAIN": "3", "RUNSPEC_SHOW_ARG_TRICKY": values["tricky"], "RUNSPEC_SHOW_ARG_DELIM": "RUNSPEC_VALUE"}
        assert argv == ["sub"]
    assert not (tmp_path / "pwned").exists()


def test_script_multiple_values_reach_runspec_as_a_list(tmp_path):
    """Rundeck joins a multi-valued option with its delimiter; the script turns
    that into runspec's newline-delimited env value, which parse() splits back
    into the list — including for a delimiter that is a pattern character."""
    from runspec.parser import _env_value

    _needs_bash()
    show = _show_binary(tmp_path)
    args = {
        "states": {"type": "choice", "options": ["Missing", "Stale"], "multiple": True, "default": ["Missing", "Stale"]},
        "paths": {"type": "str", "multiple": True, "delimiter": "/"},
        "globs": {"type": "str", "multiple": True, "delimiter": "*"},
        "one": {"type": "str"},
    }
    values = {
        "states": rundeckgen.render_option("states", args["states"])["value"],
        "paths": "a/b'c/$(touch pwned)",
        "globs": "x*y",
        "one": "a,b",
    }
    for venv in (None, str(tmp_path)):
        script = rundeckgen.render_script("show", [], args, str(show), venv=venv)
        out = _run_substituted(tmp_path, script, values)
        assert out.returncode == 0, out.stderr
        env = json.loads(out.stdout)[0]
        assert _env_value(env["RUNSPEC_SHOW_ARG_STATES"], args["states"]) == ["Missing", "Stale"]
        assert _env_value(env["RUNSPEC_SHOW_ARG_PATHS"], args["paths"]) == ["a", "b'c", "$(touch pwned)"]
        assert _env_value(env["RUNSPEC_SHOW_ARG_GLOBS"], args["globs"]) == ["x", "y"]
        assert env["RUNSPEC_SHOW_ARG_ONE"] == "a,b"  # a single-value option is never split
    assert not (tmp_path / "pwned").exists()


def test_script_refuses_a_value_with_a_line_break(tmp_path):
    """A value can only leave its heredoc through a line break; the guard on
    line 2 counts the script's lines and stops before any value is read."""
    _needs_bash()
    show = _show_binary(tmp_path)
    attacks = [
        "a\nRUNSPEC_VALUE\ntouch pwned\n",
        "a\nRUNSPEC_VALUE\ntouch pwned",
        "\ntouch pwned",
        "a\r\ntouch pwned",
    ]
    for attack in attacks:
        for run_as in (None, "svc"):
            script = rundeckgen.render_script("show", [], {"a": {"type": "str"}, "b": {"type": "password"}}, str(show), run_as=run_as)
            out = _run_substituted(tmp_path, script, {"a": "ok", "b": attack})
            assert out.returncode == 64, (attack, out.stdout, out.stderr)
            assert "line break" in out.stderr
            assert out.stdout == ""
    assert not (tmp_path / "pwned").exists()


def test_script_guard_tolerates_blank_lines(tmp_path):
    """A trailing newline added on the way to the node, or a value that is only
    a line break, adds blank lines only — the guard ignores them and the
    heredoc still reads just its own line."""
    _needs_bash()
    show = _show_binary(tmp_path)
    script = rundeckgen.render_script("show", [], {"a": {"type": "str"}}, str(show)) + "\n\n"
    out = _run_substituted(tmp_path, script, {"a": "\n"})
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)[0] == {}


def test_script_guard_count_matches_rendered_script():
    script = rundeckgen.render_script("deploy", ["start"], {"t": {"type": "password"}}, "/opt/v/bin/d", run_as="svc", venv="/opt/v")
    count = sum(1 for line in script.split("\n") if line.strip())
    assert script.split("\n")[2].startswith('[ "$(awk ')
    assert f"= {count} ]" in script.split("\n")[2]


def test_script_makes_its_own_file_private(tmp_path):
    """The job script's first command makes the temp file Rundeck copied to the
    node readable by its owner only (it may be left behind if a job is killed)."""
    _needs_bash()
    show = _show_binary(tmp_path)
    script = rundeckgen.render_script("show", [], {"a": {"type": "str"}}, str(show))
    assert script.split("\n")[1].startswith('chmod 700 -- "${BASH_SOURCE[0]:-$0}"')
    path = tmp_path / "rd-script.sh"
    out = _run_substituted(tmp_path, script, {"a": "secret"})
    assert out.returncode == 0, out.stderr
    assert path.stat().st_mode & 0o777 == 0o700


def test_script_stops_when_runnable_missing_from_venv(tmp_path):
    _needs_bash()
    (tmp_path / "bin").mkdir()
    script = rundeckgen.render_script("app", [], {"a": {"type": "str"}}, str(tmp_path / "bin" / "app"), venv=str(tmp_path))
    out = _run_substituted(tmp_path, script, {"a": "x"})
    assert out.returncode == 127
    assert f"runspec: app not found at {tmp_path}/bin/app (checked as " in out.stderr
    assert out.stdout == ""


def test_script_stops_when_runnable_missing_from_path(tmp_path):
    _needs_bash()
    import os

    script = rundeckgen.render_script("rs-no-such-runnable", [], {}, "rs-no-such-runnable")
    out = _run_substituted(tmp_path, script, {}, env={**os.environ, "PATH": "/usr/bin:/bin"})
    assert out.returncode == 127
    assert "runspec: rs-no-such-runnable not found on PATH (checked as " in out.stderr


def test_script_owner_runs_directly_when_already_the_owner(tmp_path):
    """`@owner` resolves the venv's owner on the node; the test venv is ours
    (or root's, which @owner never escalates to), so no sudo is involved."""
    _needs_bash()
    import os

    show = _show_binary(tmp_path)
    for venv in (str(tmp_path), None):
        invocation = str(show) if venv else "show"
        script = rundeckgen.render_script("show", ["sub"], {"a": {"type": "str"}}, invocation, run_as="@owner", venv=venv)
        env = {**os.environ, "PATH": f"{tmp_path}/bin:/usr/bin:/bin"}
        out = _run_substituted(tmp_path, script, {"a": "v"}, env=env)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout) == [{"RUNSPEC_SHOW_ARG_A": "v"}, ["sub"]]


def test_script_owner_escalates_to_the_venv_owner(tmp_path):
    """A venv owned by another account: the job sudoes to that account and the
    values cross on stdin. `stat` and `sudo` are stubbed to fake the owner."""
    _needs_bash()
    import os
    import sys

    show = _show_binary(tmp_path)
    (tmp_path / "bin" / "python").symlink_to(sys.executable)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "stat").write_text('#!/bin/sh\ncase "$2" in %U) echo svc-owner;; *) echo 4242;; esac\n')
    (stubs / "sudo").write_text('#!/bin/sh\necho "$*" > "$SUDO_LOG"\nshift 3\nexec "$@"\n')
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}:{tmp_path}/bin:/usr/bin:/bin", "SUDO_LOG": str(tmp_path / "sudo.log")}
    for venv in (str(tmp_path), None):
        invocation = str(show) if venv else "show"
        script = rundeckgen.render_script("show", ["sub"], {"a": {"type": "str"}, "b": {"type": "password"}}, invocation, run_as="@owner", venv=venv)
        out = _run_substituted(tmp_path, script, {"a": "v", "b": "s3cret"}, env=env)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout) == [{"RUNSPEC_SHOW_ARG_A": "v", "RUNSPEC_SHOW_ARG_B": "s3cret"}, ["sub"]]
        sudo_argv = (tmp_path / "sudo.log").read_text()
        assert sudo_argv.startswith("-n -u svc-owner ")
        assert "s3cret" not in sudo_argv and str(show) in sudo_argv


def test_script_env_frame_matches_spec():
    """The bash `rs_frame` helper writes the same bytes as `become.env_frame`."""
    import shutil
    import subprocess

    from runspec.become import env_frame

    if not shutil.which("bash") or not shutil.which("od"):
        import pytest

        pytest.skip("needs bash + od")
    value = "x y'z\n\u00fc$HOME"
    script = rundeckgen._FRAME_FUNC + '\nA="$1"\nB=\nrs_frame A B'
    out = subprocess.run(["bash", "-c", script, "bash", value], capture_output=True, check=True)
    assert out.stdout == env_frame({"A": value})


# ── leaf expansion ────────────────────────────────────────────────────────────


def test_expand_leaves_no_commands_single_leaf():
    script = {"description": "d", "args": {"x": {"type": "str"}}, "commands": {}}
    leaves = rundeckgen.expand_leaves("tool", script)
    assert len(leaves) == 1
    assert leaves[0]["name"] == "tool"
    assert leaves[0]["command_path"] == []
    assert set(leaves[0]["args"]) == {"x"}


def test_expand_leaves_flattens_subcommands_with_inherited_globals():
    script = {
        "description": "root",
        "args": {"g": {"type": "str"}},  # global, inherited
        "commands": {
            "add": {"description": "add", "args": {"name": {"type": "str"}}, "commands": {}},
            "list": {"description": "list", "args": {}, "commands": {}},
        },
    }
    leaves = {leaf["name"]: leaf for leaf in rundeckgen.expand_leaves("cron", script)}
    assert set(leaves) == {"cron_add", "cron_list"}
    assert set(leaves["cron_add"]["args"]) == {"g", "name"}  # global inherited
    assert leaves["cron_add"]["command_path"] == ["add"]
    assert leaves["cron_list"]["description"] == "list"


def test_expand_leaves_child_overrides_parent_on_name_clash():
    script = {
        "args": {"mode": {"type": "str", "default": "root"}},
        "commands": {"run": {"args": {"mode": {"type": "str", "default": "child"}}, "commands": {}}},
    }
    leaf = rundeckgen.expand_leaves("t", script)[0]
    assert leaf["args"]["mode"]["default"] == "child"


# ── settings resolution ───────────────────────────────────────────────────────


def test_merge_settings_precedence_cli_over_meta():
    settings = rundeckgen.merge_settings(
        "app",
        cli_opts={"group": "cli-group", "venv": None, "node_filter": None, "project": None},
        config_meta={"group": "config-group"},
        runnable_meta={"group": "runnable-group"},
    )
    assert settings["group"] == "cli-group"


def test_merge_settings_runnable_over_config():
    settings = rundeckgen.merge_settings("app", cli_opts={}, config_meta={"schedule": "cfg"}, runnable_meta={"schedule": "run"})
    assert settings["schedule"] == "run"


def test_merge_settings_invocation_from_venv():
    settings = rundeckgen.merge_settings("app", {"venv": "/opt/v/"}, {}, {})
    assert settings["invocation"] == "/opt/v/bin/app"


def test_merge_settings_run_as_precedence():
    assert rundeckgen.merge_settings("app", {"run_as": "cli"}, {"run_as": "cfg"}, {"run_as": "run"}, "own")["run_as"] == "cli"
    assert rundeckgen.merge_settings("app", {}, {"run_as": "cfg"}, {"run_as": "run"}, "own")["run_as"] == "run"
    assert rundeckgen.merge_settings("app", {}, {"run_as": "cfg"}, {}, "own")["run_as"] == "cfg"
    assert rundeckgen.merge_settings("app", {}, {}, {}, "own")["run_as"] == "own"


def test_merge_settings_run_as_ignores_forms_a_job_cannot_resolve():
    for value in ("$SVC_USER", {"web-*": "svc"}, "", "@other"):
        assert rundeckgen.merge_settings("app", {}, {}, {}, value)["run_as"] is None


def test_merge_settings_run_as_owner_and_unset_default_to_owner():
    assert rundeckgen.merge_settings("app", {}, {}, {}, "@owner")["run_as"] == "@owner"
    assert rundeckgen.merge_settings("app", {}, {}, {}, None)["run_as"] == "@owner"
    assert rundeckgen.merge_settings("app", {}, {}, {})["run_as"] == "@owner"
    # An explicit "" anywhere opts out, like the core.
    assert rundeckgen.merge_settings("app", {"run_as": ""}, {}, {}, "own")["run_as"] is None
    assert rundeckgen.merge_settings("app", {}, {}, {"run_as": ""}, None)["run_as"] is None


def test_merge_settings_invocation_bare_without_venv():
    settings = rundeckgen.merge_settings("app", {}, {}, {})
    assert settings["invocation"] == "app"
    assert settings["venv"] is None


def test_orchestrator_defaults_subset_with_filter_none_without():
    with_filter = rundeckgen.merge_settings("app", {"node_filter": "tags: web"}, {}, {})
    assert with_filter["orchestrator"] == "subset"
    without = rundeckgen.merge_settings("app", {}, {}, {})
    assert without["orchestrator"] == "none"


# ── notifications ─────────────────────────────────────────────────────────────


def test_render_notifications_email_and_webhook():
    out = rundeckgen.render_notifications(
        {
            "onfailure": {"email": "ops@example.com"},
            "onsuccess": {"webhook": "https://h/x"},
        }
    )
    assert out["onfailure"]["email"]["recipients"] == "ops@example.com"
    assert out["onsuccess"]["webhook"]["urls"] == "https://h/x"


def test_render_notifications_email_dict_with_subject():
    out = rundeckgen.render_notifications({"onfailure": {"email": {"recipients": "a@b", "subject": "Failed", "attachLog": True}}})
    assert out["onfailure"]["email"] == {"recipients": "a@b", "subject": "Failed", "attachLog": True}


def test_render_notifications_webhook_dict_form():
    out = rundeckgen.render_notifications({"onstart": {"url": {"urls": "https://h/y"}}})
    assert out["onstart"]["webhook"]["urls"] == "https://h/y"


def test_render_notifications_ignores_unknown_and_empty():
    assert rundeckgen.render_notifications({"whenever": {"email": "x@y"}}) == {}
    assert rundeckgen.render_notifications({"onfailure": {}}) == {}


# ── job assembly ──────────────────────────────────────────────────────────────


def _settings(**over):
    base = rundeckgen.merge_settings("app", {}, {}, {})
    base.update(over)
    return base


def _leaf(name="app", args=None):
    return {"name": name, "description": "d", "command_path": [], "args": args or {}}


def test_render_job_core_shape():
    job = rundeckgen.render_job("app", _leaf(args={"x": {"type": "str", "required": True}}), _settings())
    assert job["name"] == "app"
    assert job["loglevel"] == "INFO"
    assert job["sequence"]["strategy"] == "node-first"
    assert job["sequence"]["commands"][0]["scriptInterpreter"] == "/bin/bash"
    assert job["options"][0]["name"] == "x"


def test_render_job_no_args_omits_options():
    job = rundeckgen.render_job("app", _leaf(args={}), _settings())
    assert "options" not in job


def test_render_job_nodefilter_adds_orchestrator_subset():
    job = rundeckgen.render_job("app", _leaf(), _settings(node_filter="tags: web", orchestrator="subset", threadcount=1))
    assert job["nodefilters"]["filter"] == "tags: web"
    assert job["nodefilters"]["dispatch"]["threadcount"] == 1
    assert job["orchestrator"] == {"type": "subset", "configuration": {"count": "1"}}


def test_render_job_no_nodefilter_no_orchestrator():
    job = rundeckgen.render_job("app", _leaf(), _settings())
    assert "nodefilters" not in job
    assert "orchestrator" not in job


def test_render_job_schedule():
    job = rundeckgen.render_job("app", _leaf(), _settings(schedule="0 0 2 * * ?"))
    assert job["schedule"] == {"crontab": "0 0 2 * * ?"}
    assert job["scheduleEnabled"] is True


def test_render_job_timeout_and_retry():
    job = rundeckgen.render_job("app", _leaf(), _settings(timeout="30m", retry=2))
    assert job["timeout"] == "30m"
    assert job["retry"] == "2"


def test_render_job_notification_from_settings():
    job = rundeckgen.render_job("app", _leaf(), _settings(notifications={"onfailure": {"email": "ops@x"}}))
    assert job["notification"]["onfailure"]["email"]["recipients"] == "ops@x"


def test_render_job_uuid_is_deterministic():
    a = rundeckgen.render_job("app", _leaf(), _settings())
    b = rundeckgen.render_job("app", _leaf(), _settings())
    assert a["uuid"] == b["uuid"]
    # different job name -> different uuid
    c = rundeckgen.render_job("app", _leaf(name="other"), _settings())
    assert c["uuid"] != a["uuid"]


# ── serialization / determinism ───────────────────────────────────────────────


def test_dumps_is_stable_and_valid_json_array():
    jobs = rundeckgen.render_runnable(
        "cron",
        {
            "args": {},
            "commands": {
                "add": {"args": {"schedule": {"type": "str", "required": True}}, "commands": {}},
                "list": {"args": {}, "commands": {}},
            },
        },
        _settings(),
    )
    text1 = rundeckgen.dumps(jobs)
    text2 = rundeckgen.dumps(jobs)
    assert text1 == text2  # byte-stable for --check
    parsed = json.loads(text1)
    assert isinstance(parsed, list)
    assert {j["name"] for j in parsed} == {"cron_add", "cron_list"}


def test_render_runnable_password_option_present_and_env_exported():
    jobs = rundeckgen.render_runnable(
        "deploy",
        {"args": {"token": {"type": "password"}}, "commands": {}},
        _settings(invocation="/opt/deploy/.venv/bin/deploy"),
    )
    job = jobs[0]
    token_opt = next(o for o in job["options"] if o["name"] == "token")
    assert token_opt["secure"] is True
    assert "x@option.token@" in job["sequence"]["commands"][0]["script"]


def test_render_job_run_as_wraps_in_sudo():
    job = rundeckgen.render_job("app", _leaf(args={"x": {"type": "str"}}), _settings(run_as="svc", venv="/opt/v"))
    assert '| sudo -n -u "$rs_user" /opt/v/bin/python' in job["sequence"]["commands"][0]["script"]
