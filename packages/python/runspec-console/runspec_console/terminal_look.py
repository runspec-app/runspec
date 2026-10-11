"""terminal_look — the ``[terminal]`` look for PuTTY terminals.

PuTTY keeps fonts, colours and cursor style in a *saved session* (Windows
registry), never on its command line. The console already opens terminals by
loading a saved session it writes itself (the HTTP proxy and jump sessions), so
the look rides the same sessions: :func:`session_values` turns the
``[terminal]`` config into the registry values PuTTY reads, and
``bridge_utils._write_putty_session`` stores them.

Pure (no winreg) so it is testable everywhere. Unset everywhere ⇒ ``{}`` ⇒ the
terminal looks exactly as it did before (PuTTY's own defaults).

```toml
[terminal]
theme = "one-half-dark"      # see THEMES; "" = PuTTY's own colours
font = "Cascadia Mono"       # "" = PuTTY's default font
font_size = 11               # points; 0 = default
cursor = "bar"               # block | underline | bar; "" = default
cursor_blink = true
scrollback = 10000           # lines; 0 = default
padding = 6                  # pixels between the text and the window edge

[terminal.group_themes]      # a host group gets its own colours
prod = "red-alert"
```
"""

from __future__ import annotations

import re
from typing import Any

RGB = tuple[int, int, int]

# Each theme: label, foreground, background, cursor, and the 16 ANSI colours as
# (normal, bright) pairs in PuTTY's order: black, red, green, yellow, blue,
# magenta, cyan, white.
THEMES: dict[str, dict[str, Any]] = {
    "campbell": {
        "label": "Campbell (Windows Terminal)",
        "fg": (204, 204, 204),
        "bg": (12, 12, 12),
        "cursor": (255, 255, 255),
        "ansi": [
            ((12, 12, 12), (118, 118, 118)),
            ((197, 15, 31), (231, 72, 86)),
            ((19, 161, 14), (22, 198, 12)),
            ((193, 156, 0), (249, 241, 165)),
            ((0, 55, 218), (59, 120, 255)),
            ((136, 23, 152), (180, 0, 158)),
            ((58, 150, 221), (97, 214, 214)),
            ((204, 204, 204), (242, 242, 242)),
        ],
    },
    "one-half-dark": {
        "label": "One Half Dark",
        "fg": (220, 223, 228),
        "bg": (40, 44, 52),
        "cursor": (255, 255, 255),
        "ansi": [
            ((40, 44, 52), (90, 99, 116)),
            ((224, 108, 117), (224, 108, 117)),
            ((152, 195, 121), (152, 195, 121)),
            ((229, 192, 123), (229, 192, 123)),
            ((97, 175, 239), (97, 175, 239)),
            ((198, 120, 221), (198, 120, 221)),
            ((86, 182, 194), (86, 182, 194)),
            ((220, 223, 228), (255, 255, 255)),
        ],
    },
    "dracula": {
        "label": "Dracula",
        "fg": (248, 248, 242),
        "bg": (40, 42, 54),
        "cursor": (248, 248, 242),
        "ansi": [
            ((33, 34, 44), (98, 114, 164)),
            ((255, 85, 85), (255, 110, 110)),
            ((80, 250, 123), (105, 255, 148)),
            ((241, 250, 140), (255, 255, 165)),
            ((189, 147, 249), (214, 172, 255)),
            ((255, 121, 198), (255, 146, 223)),
            ((139, 233, 253), (164, 255, 255)),
            ((248, 248, 242), (255, 255, 255)),
        ],
    },
    "nord": {
        "label": "Nord",
        "fg": (216, 222, 233),
        "bg": (46, 52, 64),
        "cursor": (216, 222, 233),
        "ansi": [
            ((59, 66, 82), (76, 86, 106)),
            ((191, 97, 106), (191, 97, 106)),
            ((163, 190, 140), (163, 190, 140)),
            ((235, 203, 139), (235, 203, 139)),
            ((129, 161, 193), (129, 161, 193)),
            ((180, 142, 173), (180, 142, 173)),
            ((136, 192, 208), (143, 188, 187)),
            ((229, 233, 240), (236, 239, 244)),
        ],
    },
    "gruvbox-dark": {
        "label": "Gruvbox Dark",
        "fg": (235, 219, 178),
        "bg": (40, 40, 40),
        "cursor": (235, 219, 178),
        "ansi": [
            ((40, 40, 40), (146, 131, 116)),
            ((204, 36, 29), (251, 73, 52)),
            ((152, 151, 26), (184, 187, 38)),
            ((215, 153, 33), (250, 189, 47)),
            ((69, 133, 136), (131, 165, 152)),
            ((177, 98, 134), (211, 134, 155)),
            ((104, 157, 106), (142, 192, 124)),
            ((168, 153, 132), (235, 219, 178)),
        ],
    },
    "solarized-dark": {
        "label": "Solarized Dark",
        "fg": (131, 148, 150),
        "bg": (0, 43, 54),
        "cursor": (147, 161, 161),
        "ansi": [
            ((7, 54, 66), (0, 43, 54)),
            ((220, 50, 47), (203, 75, 22)),
            ((133, 153, 0), (88, 110, 117)),
            ((181, 137, 0), (101, 123, 131)),
            ((38, 139, 210), (131, 148, 150)),
            ((211, 54, 130), (108, 113, 196)),
            ((42, 161, 152), (147, 161, 161)),
            ((238, 232, 213), (253, 246, 227)),
        ],
    },
    "solarized-light": {
        "label": "Solarized Light",
        "fg": (101, 123, 131),
        "bg": (253, 246, 227),
        "cursor": (88, 110, 117),
        "ansi": [
            ((7, 54, 66), (0, 43, 54)),
            ((220, 50, 47), (203, 75, 22)),
            ((133, 153, 0), (88, 110, 117)),
            ((181, 137, 0), (101, 123, 131)),
            ((38, 139, 210), (131, 148, 150)),
            ((211, 54, 130), (108, 113, 196)),
            ((42, 161, 152), (147, 161, 161)),
            ((238, 232, 213), (253, 246, 227)),
        ],
    },
    "red-alert": {
        # For production groups: the Campbell palette on a dark red background,
        # so a prod window is obvious at a glance.
        "label": "Red alert (production)",
        "fg": (235, 220, 220),
        "bg": (58, 8, 12),
        "cursor": (255, 255, 255),
        "ansi": [
            ((12, 12, 12), (118, 118, 118)),
            ((231, 72, 86), (255, 110, 120)),
            ((22, 198, 12), (80, 230, 70)),
            ((249, 210, 80), (249, 241, 165)),
            ((90, 140, 255), (130, 170, 255)),
            ((200, 90, 210), (230, 120, 230)),
            ((97, 214, 214), (140, 235, 235)),
            ((204, 204, 204), (242, 242, 242)),
        ],
    },
}

CURSORS = {"block": 0, "underline": 1, "bar": 2}

_SLUG = re.compile(r"[^a-z0-9-]+")


def read_terminal(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """The normalised ``[terminal]`` section of a config dict.

    Unknown themes/cursors are dropped (treated as unset) rather than raising,
    so a typo can never stop a terminal from opening."""
    raw = (cfg or {}).get("terminal")
    t = dict(raw) if isinstance(raw, dict) else {}

    def _theme(v: Any) -> str:
        s = str(v or "").strip().lower()
        return s if s in THEMES else ""

    def _int(v: Any, lo: int, hi: int) -> int:
        try:
            n = int(v or 0)
        except (TypeError, ValueError):
            return 0
        return min(hi, max(lo, n)) if n else 0

    cursor = str(t.get("cursor") or "").strip().lower()
    groups_raw = t.get("group_themes")
    groups: dict[str, str] = {}
    if isinstance(groups_raw, dict):
        for g, th in groups_raw.items():
            if str(g).strip() and _theme(th):
                groups[str(g).strip()] = _theme(th)
    blink = t.get("cursor_blink")
    return {
        "theme": _theme(t.get("theme")),
        "font": str(t.get("font") or "").strip(),
        "font_size": _int(t.get("font_size"), 6, 72),
        "cursor": cursor if cursor in CURSORS else "",
        "cursor_blink": None if blink is None else bool(blink),
        "scrollback": _int(t.get("scrollback"), 100, 1_000_000),
        "padding": _int(t.get("padding"), 0, 40),
        "group_themes": groups,
    }


def theme_for(look: dict[str, Any], group: str = "") -> str:
    """The theme a host in ``group`` gets: its group's theme, else the base."""
    g = str(group or "").strip()
    if g:
        for name, theme in (look.get("group_themes") or {}).items():
            if name.lower() == g.lower():
                return str(theme)
    return str(look.get("theme") or "")


def session_suffix(look: dict[str, Any], group: str = "") -> str:
    """A saved-session name suffix for a host whose group has its own theme.

    Each distinct look gets its own session name, so tiling several hosts with
    different group colours never races on one shared session."""
    theme = theme_for(look, group)
    if theme and theme != look.get("theme"):
        return "--" + _SLUG.sub("-", theme)
    return ""


def _rgb(c: RGB) -> str:
    return f"{c[0]},{c[1]},{c[2]}"


def session_values(look: dict[str, Any], group: str = "") -> dict[str, int | str]:
    """PuTTY saved-session registry values for ``look`` (``{}`` when unset).

    ``int`` values are REG_DWORD, ``str`` values REG_SZ."""
    out: dict[str, int | str] = {}
    theme = THEMES.get(theme_for(look, group))
    if theme:
        fg, bg, cur = theme["fg"], theme["bg"], theme["cursor"]
        bright_white = theme["ansi"][7][1]
        colours: list[RGB] = [fg, bright_white, bg, bg, bg, cur]
        for normal, bright in theme["ansi"]:
            colours += [normal, bright]
        out["UseSystemColours"] = 0
        for i, c in enumerate(colours):
            out[f"Colour{i}"] = _rgb(c)
    if look.get("font"):
        out["Font"] = str(look["font"])
        out["FontIsBold"] = 0
        out["FontCharSet"] = 0
        out["FontQuality"] = 3  # ClearType
    if look.get("font_size"):
        out["FontHeight"] = int(look["font_size"])
    if look.get("cursor") in CURSORS:
        out["CurType"] = CURSORS[look["cursor"]]
    if look.get("cursor_blink") is not None:
        out["BlinkCur"] = 1 if look["cursor_blink"] else 0
    if look.get("scrollback"):
        out["ScrollbackLines"] = int(look["scrollback"])
    if look.get("padding"):
        out["WindowBorder"] = int(look["padding"])
    return out


def themes_for_ui() -> list[dict[str, Any]]:
    """The theme list for the Settings preview (id, label, colours as hex)."""

    def hx(c: RGB) -> str:
        return "#%02x%02x%02x" % c

    return [
        {
            "id": tid,
            "label": t["label"],
            "fg": hx(t["fg"]),
            "bg": hx(t["bg"]),
            "cursor": hx(t["cursor"]),
            "ansi": [hx(n) for n, _ in t["ansi"]] + [hx(b) for _, b in t["ansi"]],
        }
        for tid, t in THEMES.items()
    ]
