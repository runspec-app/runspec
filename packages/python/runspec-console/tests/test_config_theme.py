"""
test_config_theme.py — the white-label [console.theme] helpers and the
Bridge.brand_theme() / app icon colour that consume them.

Covers: valid_accent / valid_image_ref validation (the pure rules shared by the
bridge and the icon builder), theme_config shape, brand_theme() passthrough +
dropping of invalid values, and the icon square taking its colour from
accent_color (cached per-colour).
"""

from __future__ import annotations

import runspec_console.config as cfg


def _cfg_path(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(cfg, "app_dir", lambda: tmp_path)
    monkeypatch.setattr("runspec_console.config.app_dir", lambda: tmp_path)


# ── pure validation ──────────────────────────────────────────────────────────────


def test_valid_accent_accepts_hex():
    assert cfg.valid_accent("#b3001b") == "#b3001b"
    assert cfg.valid_accent("#abc") == "#abc"
    assert cfg.valid_accent("  #ABC123  ") == "#ABC123"  # trimmed


def test_valid_accent_rejects_non_hex():
    for bad in ["red", "b3001b", "#12", "#1234", "#xyzxyz", "", None, 42, "rgb(1,2,3)"]:
        assert cfg.valid_accent(bad) is None


def test_valid_image_ref_accepts_url_and_data():
    assert cfg.valid_image_ref("https://x/logo.png") == "https://x/logo.png"
    assert cfg.valid_image_ref("http://x/logo.png") == "http://x/logo.png"
    assert cfg.valid_image_ref("data:image/png;base64,AAAA").startswith("data:image/")


def test_valid_image_ref_rejects_other_schemes():
    for bad in ["javascript:alert(1)", "/etc/passwd", "file:///x", "ftp://x", "", None]:
        assert cfg.valid_image_ref(bad) is None


def test_valid_watermark_style():
    assert cfg.valid_watermark_style("tiled") == "tiled"
    assert cfg.valid_watermark_style("banner") == "banner"
    assert cfg.valid_watermark_style("  center  ") == "center"
    for bad in ["diagonal", "BANNER", "", None, 1, "tiledd"]:
        assert cfg.valid_watermark_style(bad) is None


def test_theme_config_shape(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    cfg.write_config({"console": {"theme": {"accent_color": "#b3001b"}}})
    assert cfg.theme_config() == {"accent_color": "#b3001b"}
    cfg.write_config({"console": {}})
    assert cfg.theme_config() == {}


# ── Bridge.brand_theme ───────────────────────────────────────────────────────────


def _brand_theme():
    from runspec_console.bridge import Bridge

    class _B:
        brand_theme = Bridge.brand_theme

    return _B().brand_theme()


def test_brand_theme_passes_valid_drops_invalid(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    cfg.write_config(
        {
            "console": {
                "theme": {
                    "accent_color": "#b3001b",
                    "brand_name": "  MyCorp Ops  ",
                    "watermark_text": "Internal",
                    "watermark_logo": "https://x/logo.png",
                    "watermark_style": "banner",
                    "icon": "C:/brand/app.ico",  # not a UI field — not returned
                }
            }
        }
    )
    out = _brand_theme()
    assert out == {
        "accent_color": "#b3001b",
        "brand_name": "MyCorp Ops",
        "watermark_text": "Internal",
        "watermark_logo": "https://x/logo.png",
        "watermark_style": "banner",
    }


def test_brand_theme_drops_garbage(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    cfg.write_config(
        {
            "console": {
                "theme": {
                    "accent_color": "octarine",  # invalid hex → dropped
                    "watermark_logo": "javascript:alert(1)",  # bad scheme → dropped
                    "brand_name": "   ",  # blank → dropped
                }
            }
        }
    )
    assert _brand_theme() == {}


def test_brand_theme_empty_when_no_theme(monkeypatch, tmp_path):
    _cfg_path(monkeypatch, tmp_path)
    cfg.write_config({"llm": {"provider": "anthropic"}})
    assert _brand_theme() == {}


# ── app icon colour ──────────────────────────────────────────────────────────────


def test_icon_colour_follows_accent(monkeypatch, tmp_path):
    from runspec_console import app as app_mod

    _cfg_path(monkeypatch, tmp_path)
    # default (no theme) → runspec blue
    assert app_mod._icon_rgb() == (9, 88, 217)

    cfg.write_config({"console": {"theme": {"accent_color": "#b3001b"}}})
    assert app_mod._icon_rgb() == (0xB3, 0x00, 0x1B)
    # #rgb shorthand expands
    cfg.write_config({"console": {"theme": {"accent_color": "#abc"}}})
    assert app_mod._icon_rgb() == (0xAA, 0xBB, 0xCC)


def test_build_icon_caches_per_colour(monkeypatch, tmp_path):
    from runspec_console import app as app_mod

    _cfg_path(monkeypatch, tmp_path)
    cfg.write_config({"console": {"theme": {"accent_color": "#b3001b"}}})
    p = app_mod._build_icon()
    assert p is not None and p.name == "runspec_console-b3001b-rgba.ico"
    assert p.exists()
    # a different accent → a different cache file
    cfg.write_config({"console": {"theme": {"accent_color": "#0958d9"}}})
    p2 = app_mod._build_icon()
    assert p2 is not None and p2.name == "runspec_console-0958d9-rgba.ico"


def test_build_icon_emits_opaque_rgba(monkeypatch, tmp_path):
    # The generated .ico must embed a true 32-bit RGBA PNG (colour type 6) so the
    # declared 32bpp matches a real opaque alpha — otherwise Windows invents the
    # alpha and renders the square with a dark-grey artifact.
    from runspec_console import app as app_mod

    _cfg_path(monkeypatch, tmp_path)
    cfg.write_config({"console": {"theme": {"accent_color": "#b3001b"}}})
    data = app_mod._build_icon().read_bytes()
    png_start = data.index(b"\x89PNG\r\n\x1a\n")
    ihdr = data[png_start + 8 :]
    assert ihdr[4:8] == b"IHDR"
    # IHDR payload: width(4) height(4) bitdepth(1) colourtype(1) …
    assert ihdr[16] == 8  # 8-bit depth
    assert ihdr[17] == 6  # colour type 6 = RGBA (was 2 = RGB before the fix)


def test_build_icon_explicit_path_wins(monkeypatch, tmp_path):
    from runspec_console import app as app_mod

    _cfg_path(monkeypatch, tmp_path)
    brand = tmp_path / "brand.ico"
    brand.write_bytes(b"icodata")
    cfg.write_config({"console": {"theme": {"icon": str(brand)}}})
    assert app_mod._build_icon() == brand


class _FakeRes:
    """Minimal importlib.resources Traversable for a single bundled file."""

    def __init__(self, data: bytes | None):
        self._data = data  # None ⇒ not a file

    def __truediv__(self, _part: str) -> "_FakeRes":
        return self

    def is_file(self) -> bool:
        return self._data is not None

    def read_bytes(self) -> bytes:
        assert self._data is not None
        return self._data


def test_build_icon_pkg_ref_resolves_and_caches(monkeypatch, tmp_path):
    import importlib.resources as resources

    from runspec_console import app as app_mod

    _cfg_path(monkeypatch, tmp_path)
    monkeypatch.setattr(resources, "files", lambda module: _FakeRes(b"BRANDICO"))
    cfg.write_config(
        {"console": {"theme": {"icon": "pkg:mycorp_console:assets/app.ico"}}}
    )
    p = app_mod._build_icon()
    assert p is not None
    assert p.parent == tmp_path  # copied into app-data
    assert p.suffix == ".ico"
    assert p.read_bytes() == b"BRANDICO"


def test_build_icon_pkg_ref_missing_falls_back_to_square(monkeypatch, tmp_path):
    import importlib.resources as resources

    from runspec_console import app as app_mod

    _cfg_path(monkeypatch, tmp_path)
    # Module resolves but the resource isn't a file → fall back to the generated
    # accent square (not a crash).
    monkeypatch.setattr(resources, "files", lambda module: _FakeRes(None))
    cfg.write_config(
        {
            "console": {
                "theme": {
                    "icon": "pkg:mycorp_console:nope.ico",
                    "accent_color": "#b3001b",
                }
            }
        }
    )
    p = app_mod._build_icon()
    assert p is not None and p.name == "runspec_console-b3001b-rgba.ico"
