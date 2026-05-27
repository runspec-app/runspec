# Changelog — runspec-console

All notable changes to this package are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

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
- **Runnables view** — lists all runnables discovered on each host; shows group,
  host, and autonomy level.
- **Forms view** — per-runnable argument form with type-aware controls (text, number,
  boolean toggle, choice dropdown), range validation, and positional-arg support.
- **Console view** — live streaming output for in-flight invocations; collapsible
  log blocks; truncation guard for large outputs.
- **History view** — full invocation audit trail per host with arg provenance.
- **Schedules view** — create, list, and delete cron-style scheduled invocations.
- **Settings drawer** — jump-host config, SSH key generation and 90-day rotation
  reminder, general preferences.
- **Today summary** — at-a-glance counts of today's runs, failures, and upcoming
  scheduled tasks.
- **Built-in runnables** — `generate-ssh-key`, `disk-usage`, `ping-host`,
  `flush-dns`, `check-port` shipped as console entry points.
- **Chat integration** (optional extras `anthropic`, `openai`, `bedrock`) — natural-
  language invocation via the slash menu.

---
