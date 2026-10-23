# Changelog — runspec-o365

All notable changes to this package are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Version numbers follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Docstring and README wording: Graph "blocked by tenant policy". No
  behaviour change.

---


## [0.1.0] — 2026-08-21

### Added
- **Initial release.** The 13 Microsoft 365 / Graph runnables — Outlook
  (`outlook-unread`, `outlook-search`, `outlook-folders`), Calendar
  (`calendar-upcoming`, `calendar-today`, `next-meeting`), OneDrive
  (`onedrive-recent`, `onedrive-search`, `onedrive-list`), Teams
  (`teams-list-chats`, `teams-read-chat`), and account (`graph-login`,
  `graph-whoami`) — **split out of `runspec-windows`** into this standalone
  package. `runspec-console` no longer depends on them, so a console / fleet
  where Microsoft Graph is unusable (blocked by tenant policy) doesn't carry unusable
  tools; install this package explicitly to opt in.

  Pure HTTP via `httpx` + delegated auth via `msal` device-code flow — no
  pywin32, no client secret. `msal`/`httpx` are hard dependencies here (they were
  a `[graph]` extra on `runspec-windows`). Public Python API `graph_get` /
  `get_token` for corporate-wrapper packages.

### Changed
- **MSAL token cache / config moved to `%APPDATA%/runspec-o365/`** (was
  `%APPDATA%/runspec-windows/`). Existing users run `graph-login` once to
  re-authenticate. The env vars `RUNSPEC_GRAPH_CLIENT_ID` / `RUNSPEC_GRAPH_TENANT`
  are unchanged.
