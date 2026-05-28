# Changelog — runspec-console

All notable changes to this package are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

---

## [0.1.3] — 2026-05-28

### Added
- **Smart output rendering** — run blocks that produce a JSON array of objects are automatically rendered as a sortable table; JSON objects render as a key-value grid. Numeric fields with `_mb`, `_kb`, `_bytes`, `_pct`, or `_seconds` suffixes are humanised (e.g. `1073741824 bytes` → `1.0 GB`).
- **View toggle** — table/grid blocks show table-view and raw-output toggle buttons so you can switch between the structured view and the raw JSON at any time.
- **Copy output** — each run block has a copy-to-clipboard button; copies the formatted table text when in table view, raw JSON otherwise. Uses `execCommand` fallback for WebView2 compatibility.
- **Ask LLM** — each run block has a robot button that forwards the raw JSON output into the chat input so the LLM can reason over it.
- **`RUNSPEC_AGENT=1` env var** — runnables invoked via the LLM chat (MCP tool calls) now have `RUNSPEC_AGENT=1` set in their environment, so they can detect agent context and adjust output format accordingly.

---

## [0.1.2] — 2026-05-27

### Added
- **Multi-venv host support** — hosts config now accepts `runspec_paths` (a list of runspec binary paths, one per virtual environment). A single jump host entry can now target multiple venvs; the console discovers runnables from all of them and routes invocations to the correct one automatically. Old `runspec_path` (single string) configs are read transparently.
- **Taskbar / window icon** — the window and taskbar now show the runspec blue icon instead of the Python logo. Generated at first launch and cached in the app-data directory (no binary asset bundled).

---

## [0.1.1] — 2026-05-27

### Fixed
- Window is now resizable again — frameless mode is kept (custom title bar intact) and 8 transparent resize handles are added at all window edges and corners. Drag any edge or corner to resize; the window respects a 1024 × 600 minimum.
- `bridge.resize_window(w, h)` and `bridge.move_window(x, y)` added to the Python bridge.

---

## [0.1.0] — 2026-05-27

### Added

- **runspec-console** — desktop GUI for runspec, packaged as a pip-installable wheel.
  Ships a pywebview window hosting a Vite/React UI (the `console-ui` package built
  and bundled at release time).
- **Frameless window** with custom title bar: drag regions on sidebar and tab bar,
  minimize / maximize-toggle / close controls (— □ ×).
- **Hosts view** — displays connected and disconnected jump hosts; one-click SSH
  connection test per host.
- **Runnables view** — l