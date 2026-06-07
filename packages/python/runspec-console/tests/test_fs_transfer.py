import pytest

from runspec_console.fs_transfer import plan_transfer, relay_items


def test_push_plan_local_to_remote():
    plan = plan_transfer("local", ["/home/me/config.cfg"], "prod-1", "/opt/app/etc")
    assert plan.direction == "push"
    assert plan.remote_host == "prod-1"
    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.src == "/home/me/config.cfg"
    assert item.dst == "/opt/app/etc/config.cfg"
    assert item.name == "config.cfg"


def test_pull_plan_remote_to_local():
    plan = plan_transfer(
        "prod-1", ["/var/log/app/fix.log"], "local", "/home/me/pulled"
    )
    assert plan.direction == "pull"
    assert plan.remote_host == "prod-1"
    assert plan.items[0].dst == "/home/me/pulled/fix.log"
    assert plan.items[0].name == "fix.log"


def test_multiple_files():
    plan = plan_transfer("local", ["/a/one.txt", "/a/two.txt"], "h", "/dest")
    assert [i.dst for i in plan.items] == ["/dest/one.txt", "/dest/two.txt"]


def test_windows_local_source_basename():
    # A local Windows source path → basename split on backslash, joined onto the
    # remote (posix) destination dir.
    plan = plan_transfer("local", ["C:\\Users\\me\\risk.csv"], "prod-1", "/opt/in")
    assert plan.items[0].name == "risk.csv"
    assert plan.items[0].dst == "/opt/in/risk.csv"


def test_pull_to_windows_local_dest():
    plan = plan_transfer("prod-1", ["/var/log/fix.log"], "local", "C:\\pulled")
    assert plan.items[0].dst == "C:\\pulled\\fix.log"


def test_empty_selection_raises():
    with pytest.raises(ValueError, match="no source files"):
        plan_transfer("local", [], "prod-1", "/opt")
    with pytest.raises(ValueError, match="no source files"):
        plan_transfer("local", ["", "  "], "prod-1", "/opt")


def test_missing_destination_raises():
    with pytest.raises(ValueError, match="no destination folder"):
        plan_transfer("local", ["/a/x"], "prod-1", "")


def test_local_to_local_rejected():
    with pytest.raises(ValueError, match="one local side and one remote side"):
        plan_transfer("local", ["/a/x"], "local", "/b")


def test_remote_to_remote_rejected():
    with pytest.raises(ValueError, match="one local side and one remote side"):
        plan_transfer("prod-1", ["/a/x"], "prod-2", "/b")


def test_relay_items():
    items = relay_items(["/var/log/app/fix.log", "/var/log/app/err.log"], "/incoming")
    assert [(i.src, i.dst, i.name) for i in items] == [
        ("/var/log/app/fix.log", "/incoming/fix.log", "fix.log"),
        ("/var/log/app/err.log", "/incoming/err.log", "err.log"),
    ]


def test_relay_items_validation():
    with pytest.raises(ValueError, match="no source files"):
        relay_items([], "/incoming")
    with pytest.raises(ValueError, match="no destination folder"):
        relay_items(["/a/x"], "")
