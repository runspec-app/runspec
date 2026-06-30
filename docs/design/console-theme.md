# Console theme — render the white-label `[console.theme]` keys

> **Status: shipped** — `runspec-console` 0.153.0. The package config seed
> (`docs/design/config-seed.md`) already *delivers* `[console.theme]` values, but
> nothing rendered them. This adds the rendering: accent colour, a content
> watermark, a sidebar brand name, and a brand-coloured app icon — all **read-only
> from config** (set by a white-label package's seed or hand-edited), no Settings
> editor in v1.

---

## 1. Scope

A white-label distribution wants the console to *look* like theirs, not runspec's.
The seed can carry the values; this change makes four surfaces honour them:

| Surface | Key | Where |
|---|---|---|
| Accent colour | `accent_color` (hex) | Ant `ConfigProvider` `token.colorPrimary` (`App.tsx`) **+** the generated app icon colour (`app.py _build_icon`) |
| Content watermark | `watermark_text`, `watermark_logo`, `watermark_style` | `App.tsx` `BrandWatermark`: `tiled` (Ant `<Watermark>`, default), `banner` (single large mark, upper third of the content area), or `center` (single large mark, middle); a logo image is used instead of text when given |
| Sidebar brand name | `brand_name` | the sidebar title (`App.tsx`) — unset keeps the literal `runspec`, so default installs are unchanged |
| App icon | `icon` (path or `pkg:` ref) + `accent_color` | `app.py _build_icon` / `_resolve_icon_override` — an explicit `icon` wins (a filesystem path, or `pkg:<module>:<relpath>` for an icon **bundled in a package**, copied into app-data so pywebview gets a stable path even from a zipped wheel); else the generated square is painted in `accent_color` (the "blue square" becomes the brand colour, now emitted as opaque RGBA) |

**Source is config only.** The frontend reads a new `brand_theme()` bridge method
(parallel to `app_title`/`docs_url`); the sidebar name reuses the same channel. No
operator Settings editor in v1 — the brand owner controls the look, re-asserted on
package upgrade (the same model as the rest of the seed). `[console.theme]` is part
of `config.toml`, so it flows through the seed/sync merge with no new plumbing.

## 2. `[console.theme]` shape

```toml
[console.theme]
accent_color   = "#b3001b"                 # hex (#rgb or #rrggbb); drives colorPrimary + icon
brand_name     = "MyCorp Ops"              # sidebar title (unset → "runspec")
watermark_text = "MyCorp"                  # faint brand mark behind content
watermark_logo = "https://…/logo.png"      # optional; a URL or data:image/… (instead of text)
watermark_style = "banner"                 # tiled (default) | banner (large, upper third) | center
icon           = "pkg:mycorp_console:app.ico"  # optional; overrides the generated icon — a path OR a pkg:<module>:<relpath> bundled-in-package reference
```

Every field is optional; an absent/invalid one falls back (no theme = today's look).

## 3. Validation (pure, shared)

Two helpers in `config.py`, used by **both** the bridge method and the icon builder
so the rules can't drift, and unit-tested directly:

* `valid_accent(value)` → the colour if it matches `^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$`,
  else `None`. `colorPrimary` is an Ant token (not raw CSS), but validating keeps a
  garbage value from poisoning the theme and bounds what reaches the icon's RGB math.
* `valid_image_ref(value)` → the string if it's an `https://`, `http://`, or
  `data:image/` reference, else `None` — so `watermark_logo` can't smuggle a
  `javascript:` URL into an `<img>`.

`theme_config()` returns the `[console.theme]` table (or `{}`), mirroring
`sounds_config()`.

## 4. Backend — `brand_theme()` + icon

* `Bridge.brand_theme()` (new, in `bridge_config_ssh.py` next to `app_title`):
  returns `{accent_color, brand_name, watermark_text, watermark_logo}` — each
  validated/cleaned, absent keys dropped. Added to the frozen API surface.
* `app.py _build_icon()`: the cache key includes the resolved colour
  (`runspec_console-<hex>.ico`) so a changed `accent_color` regenerates; an explicit
  `theme.icon` path (a readable `.ico`) is used verbatim and wins. Default (no theme)
  → the existing runspec-blue square, byte-identical, same cache.

## 5. Frontend — `App.tsx`

* Load `brand_theme()` in the existing branding `useEffect`; hold in state.
* `ConfigProvider` `token`: spread `colorPrimary` from `accent_color` when present.
* Sidebar title: `brand_name || 'runspec'`.
* Wrap the content column in `<Watermark content={watermark_text} image={watermark_logo}>`
  only when one is set; otherwise render children unwrapped (no empty overlay).
* `brand_theme` added to the bridge interface + mock + a `BrandTheme` type.

## 6. Out of scope (later)

* A Settings → Appearance editor (operator self-customisation). v1 is config-only.
* A fuller palette beyond `colorPrimary`.
* Shipping the watermark logo / icon **inside** the seed package as binary data
  (today: a URL / data-URL for the logo, a filesystem path for the icon). Bundling
  binary package data in the seed is a separate change.

## 7. Tests

* `test_config_theme.py` — `valid_accent` (accepts #rgb/#rrggbb, rejects names /
  `javascript:` / too-long), `valid_image_ref` (accepts http/https/data:image,
  rejects `javascript:` / bare paths), `theme_config` shape.
* `test_bridge_api_surface.py` — `brand_theme` added to `EXPECTED`.
* A bridge `brand_theme()` test (valid passthrough + invalid dropped).
* Icon: a unit test that `_build_icon` colours the square from `accent_color` and
  caches per-colour (importable without a display).
* Frontend `tsc --noEmit`.
