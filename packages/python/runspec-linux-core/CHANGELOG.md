# runspec-linux-core Changelog

## [0.1.0] — 2026-06-09

Initial release.

The pure-Python logic core extracted from `runspec-linux`. Provides the
system-admin helper functions as plain importable functions — **no dependency on
`runspec`, no `runspec.toml`, no console-script entry points** — so a package can
`from runspec_linux_core import nc_send` (and the rest) without surfacing any
runnables in the venv or in `runspec local` / `runspec serve` discovery.

Each function returns plain data and *raises* on failure (`ToolNotFoundError`
when a required system tool is absent, `CommandError` when a subprocess reports
a non-zero exit); the runnable wrappers in `runspec-linux` catch these and render
the JSON/exit behaviour.

Exports:

- **System** — `system_info`, `disk_usage`, `check_memory`, `list_processes`
- **Services** — `check_service`, `list_services`, `restart_service`
- **Logs** — `tail_log`, `search_log`, `journalctl`
- **Network** — `ping_host`, `check_port`, `show_connections`
- **Files** — `find_large_files`, `backup_files`
- **Security** — `last_logins`, `who`
- **Containers** — `list_containers`, `container_logs`, `restart_container`
- **TCP** — `nc_send`
- **Errors** — `LinuxCoreError`, `ToolNotFoundError`, `CommandError`
