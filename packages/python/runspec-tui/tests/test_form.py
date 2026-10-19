from __future__ import annotations

import datetime as dt

import pytest

from runspec_tui import form


def _args(node):
    return {a["name"]: a for a in node.args}


class TestLayout:
    def test_only_args_with_a_default_fold(self, deploy) -> None:
        visible, defaulted = form.partition_args(deploy.root.args)
        assert {a["name"] for a in visible} >= {"target", "release", "token", "manifest", "when"}
        # a flag's false and a $VAR ref count as a default (as in the console)
        assert {"workers", "ratio", "dry-run", "outdir", "owner"} <= {a["name"] for a in defaulted}

    def test_split_inherited(self, db) -> None:
        inherited, own = form.split_inherited(db.root.at(["backup"]).args)
        assert [a["name"] for a in inherited] == ["user"]
        assert [a["name"] for a in own] == ["name", "host"]

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("dry-run", "flag"),
            ("target", "choice"),
            ("regions", "multichoice"),
            ("token", "password"),
            ("workers", "number"),
            ("ratio", "number"),
            ("manifest", "path"),
            ("outdir", "directory"),
            ("when", "date"),
            ("start", "datetime"),
            ("at", "time"),
            ("period", "month"),
            ("colour", "color"),
            ("tag", "lines"),
            ("extra", "rest"),
            ("release", "text"),
        ],
    )
    def test_widget_kind(self, deploy, name, kind) -> None:
        assert form.widget_kind(_args(deploy.root)[name]) == kind

    def test_type_label_matches_the_console_tags(self, deploy) -> None:
        args = _args(deploy.root)
        assert form.type_label(args["outdir"]) == "folder"
        assert form.type_label(args["when"]) == "date"
        assert form.type_label(args["workers"]) == "int"

    def test_placeholder(self, deploy) -> None:
        args = _args(deploy.root)
        assert form.placeholder(args["workers"]) == "1 – 16"
        assert form.placeholder(args["owner"]) == "$DEPLOY_OWNER"
        assert "never on the command line" in form.placeholder(args["token"])

    def test_debug_switch_is_offered_unless_declared(self, deploy) -> None:
        assert form.offer_debug(deploy.root.args)
        assert not form.offer_debug([{"name": "debug", "type": "flag"}])


class TestValues:
    def test_initial_values(self, deploy) -> None:
        args = _args(deploy.root)
        assert form.initial_value(args["workers"]) == "4"
        assert form.initial_value(args["dry-run"]) is False
        assert form.initial_value(args["owner"]) == ""  # $VAR ref left blank
        assert form.initial_value(args["regions"]) == []
        assert form.initial_value(args["target"]) is None

    def test_prefill_never_fills_a_secret(self, deploy) -> None:
        args = _args(deploy.root)
        assert form.initial_value(args["target"], {"target": "uat"}) == "uat"
        assert form.initial_value(args["token"], {"token": "s3cret"}) == ""

    def test_ui_values_are_normalised_to_what_the_console_sends(self) -> None:
        assert form.normalise_ui_value("date", "2026-10-19") == "2026-10-19"
        assert form.normalise_ui_value("month", "2026-10") == "2026-10"
        assert form.normalise_ui_value("time", "09:30") == "09:30:00"
        assert form.normalise_ui_value("datetime", "2026-10-19T20:22:07+02:00") == "2026-10-19T18:22:07.000Z"
        local = dt.datetime(2026, 10, 19, 9, 0).astimezone()
        assert form.normalise_ui_value("datetime", "2026-10-19 09:00") == local.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            form.normalise_ui_value("date", "19/10/2026")


class TestValidation:
    def test_core_coercion_errors_land_on_the_field(self, deploy) -> None:
        args = _args(deploy.root)
        assert form.field_error(args["workers"], "40") == "Value 40 is out of range [1, 16]"
        assert form.field_error(args["workers"], "x") == "expected a whole number"
        assert form.field_error(args["ratio"], "half") == "expected a number"
        assert form.field_error(args["release"], "1.0") == "Expected: a value matching pattern '^v[0-9]+$'"
        assert form.field_error(args["extra"], "'unclosed") == "can't split into arguments: No closing quotation"
        assert form.field_error(args["release"], "v12") is None
        assert form.field_error(args["colour"], "blue") == "expected a colour like #1677ff"
        assert form.field_error(args["when"], "tomorrow") == "expected YYYY-MM-DD"

    def test_required(self, deploy) -> None:
        args = _args(deploy.root)
        assert form.field_error(args["release"], "") == "Required"
        assert form.field_error(args["release"], "", env_supplied=True) is None
        assert form.field_error(args["regions"], []) is None

    def test_a_custom_type_is_left_to_the_runnable(self) -> None:
        assert form.field_error({"name": "x", "type": "semver", "required": True}, "1.2.3") is None

    def test_groups_see_only_explicit_values(self, deploy) -> None:
        values = {"target": "dev", "release": "v1", "dry-run": True, "token": "t"}
        errors, groups = form.validate(deploy.root.args, deploy.root.groups, values)
        assert errors == {}
        assert groups and "dry-run" in groups[0] and "token" in groups[0]
        _, groups = form.validate(deploy.root.args, deploy.root.groups, {**values, "dry-run": False})
        assert groups == []

    def test_field_errors_are_reported_first(self, deploy) -> None:
        errors, groups = form.validate(deploy.root.args, deploy.root.groups, {"dry-run": True, "token": "t"})
        assert set(errors) == {"target", "release"} and groups == []


class TestCommand:
    def test_defaults_are_left_to_core_and_secrets_ride_the_env(self, deploy) -> None:
        values = {
            "target": "prod",
            "release": "v7",
            "workers": "4",  # the default: not sent
            "ratio": "0.25",
            "token": "s3cret",
            "regions": ["eu", "ap"],
            "tag": "a\n\n b \n",
            "outdir": ".",
            "extra": "--force 'two words'",
            "dry-run": False,
        }
        argv, env = form.build_command("deploy", (), deploy.root.args, values)
        assert argv == [
            "--target", "prod",
            "--release", "v7",
            "--ratio", "0.25",
            "--regions", "eu", "--regions", "ap",
            "--tag", "a", "--tag", "b",
            "--", "--force", "two words",
        ]  # fmt: skip
        assert env == {"RUNSPEC_DEPLOY_ARG_TOKEN": "s3cret"}
        assert "s3cret" not in " ".join(argv)

    def test_command_path_and_debug(self, db) -> None:
        node = db.root.at(["vacuum"])
        argv, env = form.build_command("db", ("vacuum",), node.args, {"full": True}, debug=True)
        assert argv == ["vacuum", "--full"]
        assert env == {"RUNSPEC_DB_ARG_DEBUG": "1"}

    def test_display_masks_secrets_only(self) -> None:
        line = form.display_command("deploy", ["--target", "prod env"], {"RUNSPEC_DEPLOY_ARG_TOKEN": "s3cret", "RUNSPEC_DEPLOY_ARG_DEBUG": "1"})
        assert line == "RUNSPEC_DEPLOY_ARG_TOKEN=*** RUNSPEC_DEPLOY_ARG_DEBUG=1 deploy --target 'prod env'"


class TestEnvironment:
    def test_env_source(self, db) -> None:
        user = _args(db.root)["user"]
        assert form.env_source(user, "db", {}, {}) is None
        assert form.env_source(user, "db", {"DB_USER": "x"}, {}) == ".runspec_env"
        assert form.env_source(user, "db", {}, {"RUNSPEC_DB_ARG_USER": "x"}) == "$RUNSPEC_DB_ARG_USER"
        assert form.env_source(user, "db", {}, {"DB_USER": "x"}) == "$DB_USER"
