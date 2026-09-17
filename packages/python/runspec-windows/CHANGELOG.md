# runspec-windows Changelog

## [0.9.0] — 2026-09-17

### Added — `directory-size` verb on the folded `filesystem` runnable

- The folded `filesystem` runnable gains a `directory-size` verb:
  `filesystem directory-size --path <dir> [--limit N] [--depth N]` reports the recursive
  total size of each immediate child of a directory, largest first — the
  folder-level "what's filling this drive" view that `disk-usage` (per-drive
  free/used space) doesn't provide. Each subdirectory's size is the total of
  everything beneath it, so the console agent can trace disk pressure and drill
  into the biggest folder. `--depth N` (default 1) nests the breakdown N levels
  deep in one call (each folder carries its own sorted `children`), so several
  levels need no call per level. Autonomous; `runspec serve` exposes it as the
  `filesystem_directory-size` leaf MCP tool. `--path` is a directory arg
  (`ui = "directory"`) — a folder picker in the console Forms tab.
- Backed by the new `runspec_fs_core.directory_size` helper (pin bumped to
  `runspec-fs-core>=0.3.0`).

## [0.8.0] — 2026-09-11

### Changed — the folded filesystem runnable `fs` is now `filesystem`

- **Breaking:** the filesystem runnable (folded from `runspec-fs`) is renamed
  from `fs` to `filesystem` — clearer at a glance. Its `runspec.toml` section is
  now `[filesystem]` and the installed entry point is `filesystem` (was `fs`);
  invoke it as `filesystem list-directory …`.
- `runspec serve` now emits `filesystem_<verb>` leaf MCP tools (e.g.
  `filesystem_read-text-file`) where they were `fs_<verb>` — agents / configs
  referencing the old names must update.
- No behaviour change: every verb and argument is unchanged, only the runnable
  name moved. The `runspec_windows.fs` module and `runspec-fs-core` backing are
  unchanged.

## [0.7.0] — 2026-09-01

### Added — `--ca-bundle` on the web/TLS runnables

- `cert-check`, `cert-chain`, `tls-info`, `http-check`, `http-headers`,
  `redirect-trace`, and `api-spec` gained an optional `--ca-bundle` argument (a
  PEM path) to verify TLS against a **private/internal CA or a TLS-intercepting
  proxy** — the common companion to `--proxy`, since a proxy that inspects TLS
  presents its own CA. Ignored with `--insecure`. Requires
  `runspec-webops-core >= 0.3.0`.

### Added — `proxy-settings --url` (effective per-URL proxy)

- `proxy-settings` gained an optional `--url` / `-u` argument. Alongside the
  static config it already reports (WinHTTP / WinINET / PAC / env), it now also
  resolves the **effective proxy that specific URL would use** — the real
  decision the web runnables make, with WPAD/PAC evaluated per-URL and bypass
  rules applied — via the shared `runspec-webops-core` resolver. Answers "which
  proxy does *this* URL actually go through?" for debugging PAC setups.

## [0.6.0] — 2026-08-26

### Added — `--proxy` on the web/TLS runnables (auto-detect by default)

- `cert-check`, `cert-chain`, `tls-info`, `http-check`, `http-headers`,
  `redirect-trace`, and `api-spec` gained an optional `--proxy` / `-x` argument
  and now **auto-detect a proxy by default** (via `runspec-webops-core` 0.2.0),
  so they work against internal addresses that are only reachable through a
  corporate proxy — the same way Edge/Chrome do.
- With no `--proxy`, the proxy is discovered from the proxy environment variables
  and, on Windows, the **system settings** — WPAD auto-detect and PAC scripts are
  evaluated per-URL via WinHTTP, then the per-user manual proxy, then the
  machine-wide `netsh winhttp` default. `--proxy host:port` (or
  `http://host:port`) overrides; `--proxy direct` forces a direct connection.
- TLS runnables tunnel via HTTP `CONNECT`, so the inspected certificate is the
  target host's, not the proxy's. `dns-lookup` is unchanged (a DNS lookup cannot
  be proxied). Bumps the `runspec-webops-core` dependency to `>=0.2.0`.

## [0.5.0] — 2026-08-21

### Removed — Microsoft 365 / Graph runnables (moved to `runspec-o365`)

- The 13 Microsoft 365 / Graph runnables — `graph-login`, `graph-whoami`,
  `outlook-unread`, `outlook-search`, `outlook-folders`, `calendar-upcoming`,
  `calendar-today`, `next-meeting`, `onedrive-recent`, `onedrive-search`,
  `onedrive-list`, `teams-list-chats`, `teams-read-chat` — **moved out of this
  package into the new standalone [`runspec-o365`](../runspec-o365)** package.
  The `[graph]` extra (and its `msal`/`httpx` deps), the `runspec_windows.graph`
  subpackage, and the `graph_get` / `get_token` public API are gone from here.
- **Why:** `runspec-console` depended on `runspec-windows[graph]`, so the Office
  365 runnables were always installed with the console and cluttered the agent's
  toolset even where Microsoft Graph is blocked. They're now opt-in: `pip install
  runspec-o365`.
- **Migration:** `pip install runspec-o365`; the MSAL token cache moved to
  `%APPDATA%/runspec-o365/`, so run `graph-login` once to re-authenticate. The
  `RUNSPEC_GRAPH_*` env vars are unchanged. Consumers importing
  `from runspec_windows import graph_get, get_token` switch to
  `from runspec_o365 import graph_get, get_token`.


## [0.4.0] — 2026-08-01

Make the console agent a Windows corporate-desktop expert: filesystem access
plus proxy / VPN / DNS / route / policy diagnostics, and a one-shot bundle to
draft a Desktop-Services ticket from.

### Added — filesystem (folded from runspec-fs)

- The **`fs`** runnable is now folded into the Windows pack (backed by the
  dependency-free `runspec-fs-core`): one verb per Filesystem MCP tool, reads
  `autonomous` and mutations `confirm`, flattened by `runspec serve` into leaf
  `fs_<verb>` tools. The inherited `--run-as` / `--become-method` args are
  POSIX-only and **no-op on Windows** (the verb runs in-process as the
  logged-in user); they are kept so the `fs` tool surface matches runspec-linux
  across a mixed fleet.

### Added — corporate networking diagnostics (all read-only)

- **`proxy-settings`** — the effective HTTP proxy across WinHTTP (system),
  WinINET (per-user), PAC / `AutoConfigURL`, and proxy env vars.
- **`vpn-status`** — configured VPN connections and their status.
- **`dns-config`** — configured DNS servers per interface.
- **`test-connection`** — TCP / ping / route / source-interface probe
  (`Test-NetConnection`).
- **`trace-route`** (`tracert`), **`route-table`** (`route print`).
- **`gpresult`** — applied Group Policy Objects (computer + user), evidence of
  locked-down policy.
- **`list-certs`** — machine root-CA store (corporate trust check).
- **`collect-diagnostics`** — runs all of the above into **one** structured
  report (each probe isolated so a blocked one doesn't sink the bundle) for the
  agent to turn into a Windows Desktop-Services ticket email. Content-only — no
  new Microsoft Graph write scopes; nothing is sent.

### Changed

- Requires `runspec >= 0.44.0`; adds a `runspec-fs-core >= 0.2.0` dependency.

## [0.3.0] — 2026-06-22

### Added
- A **`which`** runnable — locate a command on the PATH and return its full path,
  like `which`/`where` (`autonomy = "autonomous"`, `output = "json"`). Mirrors the
  `runspec-linux` `which` runnable so the console offers it on both fleets, e.g.
  to find an interpreter/binary path to feed another runnable. It's a pure PATH
  lookup (not Windows-gated) and is **PATHEXT-aware** on Windows, so `which python`
  resolves `python.exe`. Positional `name` accepts several space-separated names;
  `--all`/`-a` returns every match; `--path` overrides the search PATH. Prints
  `{path, all_found, results: [{name, found, path, paths}]}` and exits non-zero
  when any name was not found.

## [0.2.0] — 2026-06-22

### Added
- 8 cross-platform **web / TLS / API** runnables — thin wrappers over the new
  `runspec-webops-core` dependency: `cert-check`, `cert-chain`, `tls-info`,
  `http-check`, `http-headers`, `redirect-trace`, `dns-lookup`, and `api-spec`
  (an OpenAPI/Swagger reader that condenses an API's shape so an agent can write
  or update the runnables that call it). All are read-only network probes
  (`autonomy = "autonomous"`, `output = "json"`) built on the Python stdlib
  (`ssl` / `socket` / `http.client`), so they behave identically here and in
  `runspec-linux` (the same wrappers ship in both) and are **not** gated to
  Windows. A Python port of the `runspec-webops` Node package plus four extras
  (`http-headers`, `redirect-trace`, `dns-lookup`, `api-spec`), so a fleet that
  can't run Node — and the runspec-console, which depends on `runspec-windows` —
  still gets them. See the `runspec-linux` 0.5.0 changelog for the per-runnable
  detail.

## [0.1.2] — 2026-06-04

### Changed
- Switch `[config.logging]` to `store = "per-run"` so every runnable writes one
  log file per invocation (`{runnable}.{utc-ts}.{run_id}.log`) instead of a
  single shared rotating `{runnable}.log`. These runnables are meant to be
  installed into shared venvs and run by multiple users (and in parallel);
  per-invocation files are multi-writer safe — no in-process rotation and no
  contention over one file. Use `runspec logs` to view, compact, and prune. The
  `rotate`/`keep` keys (inert under per-run) are dropped. Bumps the `runspec`
  dependency floor to `>=0.27.0`, where `store = "per-run"` was introduced.

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
