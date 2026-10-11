"""Terminal look: [terminal] config → PuTTY saved-session values."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

from runspec_console import terminal_look as tl

from tests.test_terminal import _make_bridge


def test_unset_look_writes_nothing():
    look = tl.read_terminal({})
    assert tl.session_values(look) == {}


def test_theme_font_cursor_values():
    look = tl.read_terminal(
        {
            "terminal": {
                "theme": "One-Half-Dark",
                "font": "Cascadia Mono",
                "font_size": 11,
                "cursor": "bar",
                "cursor_blink": False,
                "scrollback": 10000,
                "padding": 6,
            }
        }
    )
    v = tl.session_values(look)
    assert v["UseSystemColours"] == 0
    assert v["Colour0"] == "220,223,228"  # foreground
    assert v["Colour2"] == "40,44,52"  # background
    assert v["Colour8"] == "224,108,117"  # ANSI red
    assert len([k for k in v if k.startswith("Colour")]) == 22
    assert v["Font"] == "Cascadia Mono"
    assert v["FontHeight"] == 11
    assert v["FontQuality"] == 3
    assert v["CurType"] == 2
    assert v["BlinkCur"] == 0
    assert v["ScrollbackLines"] == 10000
    assert v["WindowBorder"] == 6


def test_bad_values_are_ignored_not_fatal():
    look = tl.read_terminal(
        {"terminal": {"theme": "nope", "cursor": "fat", "font_size": "x"}}
    )
    assert tl.session_values(look) == {}


def test_group_theme_overrides_and_gets_own_session():
    look = tl.read_terminal(
        {"terminal": {"theme": "nord", "group_themes": {"Prod": "red-alert"}}}
    )
    assert tl.theme_for(look, "prod") == "red-alert"
    assert tl.session_values(look, "prod")["Colour2"] == "58,8,12"
    assert tl.session_suffix(look, "prod") == "--red-alert"
    assert tl.session_suffix(look, "dev") == ""
    assert tl.session_values(look, "dev")["Colour2"] == "46,52,64"


def test_group_theme_without_base_theme():
    look = tl.read_terminal({"terminal": {"group_themes": {"prod": "red-alert"}}})
    assert tl.session_values(look, "dev") == {}
    assert tl.session_values(look, "prod")["Colour2"] == "58,8,12"


def test_themes_for_ui_has_16_ansi_hex():
    themes = tl.themes_for_ui()
    assert {t["id"] for t in themes} == set(tl.THEMES)
    for t in themes:
        assert len(t["ansi"]) == 16
        assert t["bg"].startswith("#") and len(t["bg"]) == 7


class _FakeKey:
    def __init__(self, store: dict, path: str):
        self.store, self.path = store, path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_winreg(store: dict[str, dict[str, tuple]]):
    w = types.ModuleType("winreg")
    w.HKEY_CURRENT_USER = "HKCU"
    w.REG_DWORD, w.REG_SZ = 4, 1

    def open_key(root, path):
        if path not in store:
            raise OSError("missing")
        return _FakeKey(store, path)

    def create_key(root, path):
        store.setdefault(path, {})
        return _FakeKey(store, path)

    def enum_value(key, i):
        items = list(store[key.path].items())
        if i >= len(items):
            raise OSError("no more")
        name, (data, typ) = items[i]
        return name, data, typ

    def set_value(key, name, _r, typ, data):
        store[key.path][name] = (data, typ)

    def delete_value(key, name):
        if name not in store[key.path]:
            raise OSError("missing")
        del store[key.path][name]

    w.OpenKey, w.CreateKey, w.EnumValue = open_key, create_key, enum_value
    w.SetValueEx, w.DeleteValue = set_value, delete_value
    return w


_SESS = r"Software\SimonTatham\PuTTY\Sessions"


def test_session_copies_default_settings_and_overlays():
    from runspec_console.bridge_utils import _write_putty_session

    store = {
        rf"{_SESS}\Default%20Settings": {
            "Font": ("Lucida Console", 1),
            "TermWidth": (120, 4),
            "HostName": ("somewhere", 1),
        }
    }
    with patch.dict(sys.modules, {"winreg": _fake_winreg(store)}):
        name = _write_putty_session("s1", {"FontHeight": 12, "ProxyHost": "p"})
    assert name == "s1"
    sess = store[rf"{_SESS}\s1"]
    assert sess["Font"] == ("Lucida Console", 1)  # operator's own default kept
    assert sess["TermWidth"] == (120, 4)
    assert sess["FontHeight"] == (12, 4)
    assert sess["ProxyHost"] == ("p", 1)
    assert "HostName" not in sess  # the command line supplies the host


def test_turning_look_off_removes_old_look_values():
    from runspec_console.bridge_utils import _write_putty_session

    store = {rf"{_SESS}\s1": {"Colour2": ("40,44,52", 1), "FontHeight": (14, 4)}}
    with patch.dict(sys.modules, {"winreg": _fake_winreg(store)}):
        _write_putty_session("s1", {"ProxyMethod": 3})
    assert store[rf"{_SESS}\s1"] == {"ProxyMethod": (3, 4)}


def _launch(b, cfg, **patches):
    with (
        patch(
            "runspec_console.bridge_utils._find_putty_exe",
            side_effect=lambda n="putty.exe": Path(f"C:/PuTTY/{n}"),
        ),
        patch.object(b, "get_config", return_value=cfg),
        patch("pathlib.Path.exists", return_value=True),
        patch("subprocess.Popen") as popen,
    ):
        b.launch_terminal(patches.get("host", "no-key"))
    return popen.call_args.args[0]


def test_no_look_no_proxy_loads_no_session():
    b = _make_bridge()
    cmd = _launch(b, {"ssh": {}})
    assert "-load" not in cmd


def test_look_loads_terminal_session():
    b = _make_bridge()
    with patch(
        "runspec_console.bridge_utils._ensure_putty_look_session",
        side_effect=lambda look, name: name,
    ) as ensure:
        cmd = _launch(b, {"ssh": {}, "terminal": {"theme": "dracula"}})
    assert cmd[1:3] == ["-load", "runspec-console-terminal"]
    assert ensure.call_args.args[0]["Colour2"] == "40,42,54"


def test_group_theme_uses_its_own_session_name():
    b = _make_bridge()
    b._hosts[1]["group"] = "prod"
    with patch(
        "runspec_console.bridge_utils._ensure_putty_look_session",
        side_effect=lambda look, name: name,
    ):
        cmd = _launch(
            b,
            {
                "ssh": {},
                "terminal": {"theme": "nord", "group_themes": {"prod": "red-alert"}},
            },
        )
    assert cmd[1:3] == ["-load", "runspec-console-terminal--red-alert"]


def test_look_rides_the_proxy_session():
    b = _make_bridge()
    with patch(
        "runspec_console.bridge_internals._ensure_putty_proxy_session",
        side_effect=lambda proxy, name, look=None: name,
    ) as ensure:
        cmd = _launch(
            b, {"ssh": {"proxy": "http://p:8080"}, "terminal": {"font_size": 12}}
        )
    assert cmd[1:3] == ["-load", "runspec-console-proxy"]
    assert ensure.call_args.kwargs["look"] == {"FontHeight": 12}


def test_look_rides_the_jump_session():
    b = _make_bridge()
    b._hosts.append({"name": "behind", "ssh": "svc@10.0.0.5", "jump": "bastion"})
    with patch(
        "runspec_console.bridge_utils._ensure_putty_jump_session",
        return_value="runspec-console-jump-bastion",
    ) as ensure:
        cmd = _launch(
            b, {"ssh": {}, "terminal": {"cursor": "underline"}}, host="behind"
        )
    assert cmd[1:3] == ["-load", "runspec-console-jump-bastion"]
    assert ensure.call_args.kwargs["look"] == {"CurType": 1}
