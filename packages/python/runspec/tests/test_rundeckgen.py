"""
Tests for rundeckgen — rendering Rundeck job definitions (JSON) from runspec
runnables (`runspec emit --rundeck`).
"""

from __future__ import annotations

import json

from runspec import rundeckgen

# ── name helpers ──────────────────────────────────────────────────────────────


def test_rd_env_var():
    assert rundeckgen.rd_env_var("target") == "RD_OPTION_TARGET"
    assert rundeckgen.rd_env_var("node-filter") == "RD_OPTION_NODE_FILTER"
    assert rundeckgen.rd_env_var("older_than") == "RD_OPTION_OLDER_THAN"


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
    assert opt["valueExposed"] is True  # exposed to the script as RD_OPTION_TOKEN
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


# ── script step ───────────────────────────────────────────────────────────────


def _script(runnable, args, command_path=None, invocation="/opt/app/.venv/bin/app"):
    return rundeckgen.render_script(runnable, command_path or [], args, invocation)


def test_script_named_arg_guarded_nonempty():
    s = _script("app", {"target": {"type": "str"}})
    assert 'if [ -n "${RD_OPTION_TARGET:-}" ]; then args+=(--target "${RD_OPTION_TARGET:-}"); fi' in s


def test_script_flag_conditional():
    s = _script("app", {"verbose": {"type": "flag", "default": False}})
    assert 'if [ "${RD_OPTION_VERBOSE:-}" = "true" ]; then args+=(--verbose); fi' in s


def test_script_positionals_in_position_order():
    args = {
        "second": {"type": "str", "position": 2},
        "first": {"type": "str", "position": 1},
    }
    s = _script("app", args)
    assert s.index("RD_OPTION_FIRST") < s.index("RD_OPTION_SECOND")


def test_script_rest_is_word_split_after_double_dash():
    s = _script("app", {"extra": {"type": "rest"}})
    assert "args+=(-- ${RD_OPTION_EXTRA:-})" in s  # unquoted -> word-splits


def test_script_secret_exported_not_on_command_line():
    s = _script("deploy", {"token": {"type": "password"}})
    assert 'export RUNSPEC_DEPLOY_ARG_TOKEN="${RD_OPTION_TOKEN:-}"' in s
    assert "--token" not in s  # never a CLI flag


def test_script_invocation_includes_command_path():
    s = _script("deploy", {}, command_path=["start"])
    assert s.strip().endswith('exec /opt/app/.venv/bin/app start "${args[@]}"')


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
    assert 'export RUNSPEC_DEPLOY_ARG_TOKEN="${RD_OPTION_TOKEN:-}"' in job["sequence"]["commands"][0]["script"]
