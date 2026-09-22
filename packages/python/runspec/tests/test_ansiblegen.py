"""
Tests for ansiblegen — rendering Ansible modules (Python) from runspec runnables
(`runspec emit --ansible`).
"""

from __future__ import annotations

import pytest

from runspec import ansiblegen

# ── name helpers ──────────────────────────────────────────────────────────────


def test_module_name_sanitizes_hyphens():
    assert ansiblegen.module_name("cert-check") == "cert_check"
    assert ansiblegen.module_name("filesystem_read-text-file") == "filesystem_read_text_file"


def test_module_filename():
    assert ansiblegen.module_filename("cert-check") == "cert_check.py"


def test_param_name_sanitizes_hyphens():
    assert ansiblegen.param_name("older-than") == "older_than"


# ── argument_spec ─────────────────────────────────────────────────────────────


def test_argspec_type_mapping():
    assert ansiblegen.arg_spec_entry({"type": "str"})["type"] == "str"
    assert ansiblegen.arg_spec_entry({"type": "int"})["type"] == "int"
    assert ansiblegen.arg_spec_entry({"type": "float"})["type"] == "float"
    assert ansiblegen.arg_spec_entry({"type": "flag"})["type"] == "bool"
    assert ansiblegen.arg_spec_entry({"type": "path"})["type"] == "path"


def test_argspec_choice_carries_choices():
    entry = ansiblegen.arg_spec_entry({"type": "choice", "options": ["text", "json"]})
    assert entry["type"] == "str"
    assert entry["choices"] == ["text", "json"]


def test_argspec_rest_is_list_of_str():
    entry = ansiblegen.arg_spec_entry({"type": "rest"})
    assert entry == {"type": "list", "elements": "str"}  # no default even if inference set []


def test_argspec_password_is_no_log():
    entry = ansiblegen.arg_spec_entry({"type": "password", "required": True})
    assert entry["no_log"] is True
    assert entry["required"] is True
    assert "default" not in entry


def test_argspec_default_is_native_not_stringified():
    assert ansiblegen.arg_spec_entry({"type": "int", "default": 7})["default"] == 7
    assert ansiblegen.arg_spec_entry({"type": "flag", "default": False})["default"] is False


def test_argspec_required_flag():
    assert ansiblegen.arg_spec_entry({"type": "str", "required": True})["required"] is True
    assert "required" not in ansiblegen.arg_spec_entry({"type": "str", "default": "x"})


# ── argv metadata ─────────────────────────────────────────────────────────────


def test_args_metadata_categories_and_secret_env():
    args = {
        "target": {"type": "str"},
        "verbose": {"type": "flag", "default": False},
        "name": {"type": "str", "position": 1},
        "extra": {"type": "rest"},
        "token": {"type": "password"},
    }
    rows = {r[0]: r for r in ansiblegen._args_metadata("deploy", args)}
    assert rows["target"][1] == "named"
    assert rows["verbose"][1] == "flag"
    assert rows["name"][1] == "positional" and rows["name"][3] == 1
    assert rows["extra"][1] == "rest"
    assert rows["token"][1] == "secret" and rows["token"][4] == "RUNSPEC_DEPLOY_ARG_TOKEN"
    # cli flag is the declared name; secret_env only on the secret
    assert rows["target"][2] == "--target"
    assert rows["verbose"][4] is None


def test_args_metadata_cli_flag_keeps_hyphens_param_underscored():
    rows = ansiblegen._args_metadata("t", {"older-than": {"type": "int"}})
    assert rows[0][0] == "older_than"  # param
    assert rows[0][2] == "--older-than"  # cli flag


# ── documentation ─────────────────────────────────────────────────────────────


def test_documentation_lists_options_with_type_choices_default():
    doc = ansiblegen.render_documentation("scan", "Scan things", {"format": {"type": "choice", "options": ["text", "json"], "default": "json", "description": "Fmt"}})
    assert "module: scan" in doc
    assert "format:" in doc
    assert "type: str" in doc
    assert 'choices: ["text", "json"]' in doc
    assert 'default: "json"' in doc


def test_documentation_escapes_colon_in_description():
    doc = ansiblegen.render_documentation("m", "Do a thing: fast", {})
    # A raw colon would break the YAML; it is double-quoted+escaped.
    assert '"Do a thing: fast"' in doc


def test_documentation_rest_arg_declares_list_elements():
    doc = ansiblegen.render_documentation("m", "d", {"extra": {"type": "rest"}})
    assert "type: list" in doc
    assert "elements: str" in doc


def test_short_description_strips_trailing_period():
    assert ansiblegen._short_description("Scan a directory.") == "Scan a directory"


def test_examples_omit_password_args():
    ex = ansiblegen.render_examples("deploy", {"token": {"type": "password", "required": True}, "env": {"type": "str"}})
    assert "token" not in ex
    assert "env" in ex


# ── module assembly ───────────────────────────────────────────────────────────


def _leaf(name="scan", args=None, command_path=None, description="Scan things"):
    return {
        "name": name,
        "description": description,
        "command_path": command_path or [],
        "args": args or {},
    }


def test_render_module_is_valid_python():
    leaf = _leaf(
        args={
            "directory": {"type": "path", "default": ".", "description": "Dir"},
            "older_than": {"type": "int", "default": 7},
            "format": {"type": "choice", "options": ["text", "json"], "default": "json"},
            "delete": {"type": "flag", "default": False},
            "token": {"type": "password", "required": True},
        }
    )
    src = ansiblegen.render_module("scan", leaf, "/opt/app/.venv/bin/scan")
    compile(src, "scan.py", "exec")  # must be syntactically valid
    assert 'RUNNABLE = "/opt/app/.venv/bin/scan"' in src
    assert "from ansible.module_utils.basic import AnsibleModule" in src
    assert "supports_check_mode=True" in src
    assert "RUNSPEC_AUTONOMY" in src  # non-interactive


def test_render_module_secret_via_env_not_argv():
    leaf = _leaf(args={"token": {"type": "password", "required": True}})
    src = ansiblegen.render_module("deploy", leaf, "deploy")
    # secret rides the env channel, keyed on the runnable
    assert '"RUNSPEC_DEPLOY_ARG_TOKEN"' in src
    assert '"no_log": True' in src


def test_render_module_subcommand_path():
    leaf = _leaf(name="deploy_start", command_path=["start"], args={})
    src = ansiblegen.render_module("deploy", leaf, "/v/bin/deploy")
    assert 'COMMAND_PATH = [\n    "start",\n]' in src


def test_render_runnable_flattens_subcommands():
    script = {
        "description": "Cron",
        "args": {},
        "commands": {
            "add": {"description": "add", "args": {"schedule": {"type": "str", "required": True}}, "commands": {}},
            "list": {"description": "list", "args": {}, "commands": {}},
        },
    }
    files = ansiblegen.render_runnable("cron", script, "/v/bin/cron")
    assert set(files) == {"cron_add.py", "cron_list.py"}
    for src in files.values():
        compile(src, "m.py", "exec")


def test_render_runnable_is_deterministic():
    leaf_script = {"args": {"x": {"type": "str", "required": True}}, "commands": {}}
    a = ansiblegen.render_runnable("tool", leaf_script, "/v/bin/tool")
    b = ansiblegen.render_runnable("tool", leaf_script, "/v/bin/tool")
    assert a == b  # byte-stable for --check


# ── YAML validity (optional — only if PyYAML is available) ─────────────────────


def test_doc_blocks_are_valid_yaml():
    yaml = pytest.importorskip("yaml")
    leaf = _leaf(
        args={
            "directory": {"type": "path", "default": ".", "description": "Dir: the target"},
            "format": {"type": "choice", "options": ["text", "json"], "default": "json"},
            "token": {"type": "password", "required": True, "description": "API token"},
        }
    )
    src = ansiblegen.render_module("scan", leaf, "/v/bin/scan")
    ns: dict = {}
    # Execute only the module-level string assignments by extracting them.
    for block in ("DOCUMENTATION", "EXAMPLES", "RETURN"):
        start = src.index(f"{block} = r'''") + len(f"{block} = r'''")
        end = src.index("'''", start)
        parsed = yaml.safe_load(src[start:end])
        assert parsed is not None
        ns[block] = parsed
    assert ns["DOCUMENTATION"]["module"] == "scan"
    assert ns["DOCUMENTATION"]["options"]["format"]["choices"] == ["text", "json"]
