# runspec-linux Changelog

## [0.1.0] — 2026-05-27

Initial release.

21 runnables covering Linux system administration:

**System monitoring** — `system-info`, `disk-usage`, `check-memory`, `list-processes`

**Services** — `check-service`, `list-services`, `restart-service`

**Logs** — `tail-log`, `search-log`, `journalctl`

**Network** — `ping-host`, `check-port`, `show-connections`

**Files** — `find-large-files`, `backup-files`

**Security** — `last-logins`, `who`

**Containers** — `list-containers`, `container-logs`, `restart-container`

**TCP interfaces** — `nc-command` (also exports `nc_send()` as a public Python API for wrapper runnables)

All read-only runnables are `autonomy = "autonomous"`. State-changing runnables (`restart-service`, `backup-files`, `restart-container`) are `autonomy = "confirm"`. Missing tools (`docker`, `systemctl`, `journalctl`) return a structured JSON error rather than a stack trace.
