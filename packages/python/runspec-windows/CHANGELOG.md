# runspec-windows Changelog

## [0.1.1] — 2026-06-03

### Fixed
- `query-eventlog` and `recent-errors` now emit a top-level JSON array of flat
  rows (like `list-processes`/`list-services`) instead of a dict wrapping nested
  arrays — so runspec-console renders them as tables instead of `[object
  Object]`. `recent-errors` flattens both logs into one array, tagging each row
  with a leading `log` column (`System`/`Application`).


## [0.1.0] — 2026-06-03

Initial release.

35 runnables covering Windows system administration and Microsoft 365:

**System monitoring** — `system-info`, `disk-usage`, `check-memory`, `uptime`

**Processes** — `list-processes`, `process-info`, `kill-process`

**Services** — `list-services`, `check-service`, `restart-service`, `stop-service`

**Network** — `ping-host`, `check-port`, `show-connections`, `list-adapters`, `flush-dns`

**Event log** — `query-eventlog`, `recent-errors`

**Scheduled tasks** — `list-scheduled-tasks`

**Installed software** — `installed-software`

**Sessions** — `who`, `current-user`

**Microsoft Graph (Outlook + Teams + Calendar + OneDrive)** — `graph-login`, `graph-whoami`, `outlook-unread`, `outlook-search`, `outlook-folders`, `teams-list-chats`, `teams-read-chat`, `calendar-upcoming`, `calendar-today`, `next-meeting`, `onedrive-recent`, `onedrive-search`, `onedrive-list`

Read-only runnables are `autonomy = "autonomous"`; state-changing ones
(`kill-process`, `restart-service`, `stop-service`, `flush-dns`, `graph-login`)
are `autonomy = "confirm"`. Windows-only runnables return a structured JSON
error (not a traceback) when run on a non-Windows host.

Microsoft Graph runnables use delegated **device-code** auth via MSAL — no
client secret. The optional `msal`/`httpx` dependencies live behind the
`[graph]` extra. The package also exports `graph_get()` and `get_token()` as a
public Python API for building further Graph-backed wrapper runnables.
