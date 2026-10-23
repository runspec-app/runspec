# Changelog

All notable changes to `runspec-mcp` are documented here.

## [0.2.3] — 2026-10-23

### Added

- **Runs from the gateway are labelled `mcp`.** Every run sends
  `RUNSPEC_LABEL=mcp`, so `runspec history` and the console's History tab show
  where it came from (needs runspec 0.76.0 on the host).

## [0.2.2] — 2026-10-07

### Fixed

- **An object passed for a JSON-string argument is sent as JSON.** The vendored
  executor sent a dict argument as Python's repr (single quotes, `True`), which
  a runnable's JSON parse rejects. Same fix as runspec-console 0.335.1.
- Memory Sync's `memory_sync.user` / `memory_sync.email` join the default
  Config Sync preserve list, matching runspec-console.

## [0.2.1] — 2026-10-01

### Fixed

- **A local run with a very long argument no longer fails as "Command not
  found" on Windows.** This is the same fix as runspec-console 0.311.1, applied
  to the vendored executor.
  - When a run's command line would go past the Windows limit, the longest
    values move onto the `RUNSPEC_<RUNNABLE>_ARG_<NAME>` environment variable.
  - A process that fails to start shows the real OS error.
  - `tests/test_parity.py` covers the new `fit_command_line` and `start_error`.

## [0.2.0] — 2026-10-01

### Added

- **Install the Claude Code plugin from runspec.app.** Two marketplaces are
  published with the docs, so the plugin installs with `/plugin` and stays
  installed. Before this, it was a `--plugin-dir` flag on every launch.
  - `https://runspec.app/marketplace.json` (marketplace `runspec`) serves the
    plugin as a zip and starts the gateway with
    `uvx --from runspec-mcp runspec-mcp serve`, so there's nothing to
    `pip install`.
  - `https://runspec.app/marketplace-pip.json` (marketplace `runspec-pip`)
    uses an installed runspec-mcp. Its `command` source runs the new
    `runspec-mcp claude-plugin`.
  - Neither fetches anything from GitHub, so a private repo is fine.
  - Install with `/plugin install runspec-mcp@runspec` or
    `runspec-mcp@runspec-pip`. Both keep the plugin name `runspec-mcp`, so tool
    names and the autonomy hook are the same either way.
- **`runspec-mcp claude-plugin`** writes a copy of the plugin into the app dir
  and prints its path. The copy points at this install's absolute
  `runspec-mcp` and at this install's Python for the autonomy hook. It's
  re-rendered only when the install moves, since Claude Code runs it once per
  session.
- **Install-time settings.** The plugin asks for the runspec-console config
  folder, or a `runspec_mcp.toml`, when you install it. These reach the gateway
  as `RUNSPEC_MCP_PLUGIN_CONFIG_DIR` / `RUNSPEC_MCP_PLUGIN_CONFIG`, which rank
  below the usual `RUNSPEC_MCP_CONFIG_DIR` / `RUNSPEC_MCP_CONFIG`, so a value
  set by hand still wins. An empty setting, or a placeholder the client left
  unexpanded, counts as unset.
- `scripts/build_claude_marketplace.py` builds the zip and both marketplace
  files into the docs site. The docs workflow runs it after `mkdocs build`.
  - It generates the zip and its SHA-256 together, so they always match.
  - The zip is byte-identical across rebuilds, so a redeploy with no plugin
    change publishes the same checksum.

### Changed

- **Plugin manifest.**
  - It no longer uses `${RUNSPEC_MCP_BIN:-runspec-mcp}`; a `${VAR:-default}`
    fallback isn't documented for plugin MCP servers. Each install route sets an
    explicit command.
  - The version now follows the package; it was stuck at 0.1.0.
  - The stale package-internal `marketplace.json` is removed.
- **`runspec-mcp setup claude`** now leads with the marketplace install, then
  the bare `claude mcp add` (no hook), then a one-session `--plugin-dir`
  command.
- Docs: `docs/mcp.md` → "Install the Claude Code plugin" covers all three
  options, the settings, and the notes (Python for the hook, updates, Claude
  Code only).

## [0.1.8] — 2026-10-01

### Fixed

- Gateway runs that carry variables (credentials) now work under a sudoers
  rule that refuses shells (`ALL, !/bin/sh, …`) or that allows only the venv's
  `bin/`. The gateway requires `runspec >= 0.70.0`, which runs the env prelude
  on the venv's own `bin/python` instead of `sh`.

## [0.1.7] — 2026-09-30

### Fixed

- **A host's own SSH key from a console directory is honoured.** The console
  writes a host's key as `identityFile`; the gateway only read `identity_file`,
  so with `--config-dir` a per-host key was ignored and the global `[ssh]` key
  (or the managed key) was used instead. `load_config` now accepts either
  spelling, in `runspec_mcp.toml` too (`config.host_identity`).

### Added

- **`$RUNSPEC_MCP_CONFIG_DIR`**, the environment form of `--config-dir`. The
  Claude Code plugin starts the gateway with environment variables but no
  arguments, so it can now reuse a console's configuration. An explicit
  `--config`/`--config-dir` still wins.
- **`runspec-mcp setup <client> --config-dir <dir>`** prints the client entry
  for a console directory (`RUNSPEC_MCP_CONFIG_DIR=<dir>`) instead of a
  `runspec_mcp.toml`, for `claude`, `vscode` and `json`.

## [0.1.6] — 2026-09-30

### Changed

- **Runnables run as their venv's owner by default** (runspec >= 0.68.0; SPEC →
  "Remote Execution → run_as"). `run_as` resolves in order: the runnable's
  own, then its package's `[config] run_as`, then `"@owner"`. `"@owner"` becomes
  the venv root's owner reported by `runspec local --format json` (`env_owner`).
  It never resolves to root, and nothing escalates when the owner is the account
  the gateway logs in as. A host still on an older core reports no owner, so its
  runnables don't escalate.

## [0.1.5] — 2026-09-30

Fixes for "the client never connects" and "the credential never arrives",
found by installing the gateway and driving it as an MCP client.

### Fixed

- **A credential with only an `id` produced no environment variables.** The env
  var prefix came from `label`, which defaulted to empty, and the documented
  example gives only `id`. So the secret never reached the runnable. `label` now
  defaults to the `id`.
- **The autonomy manifest was written inside the installed package.** The plugin
  pointed it at `$CLAUDE_PLUGIN_ROOT/autonomy.json`, which a system-wide install
  can't write, and then every call prompted. The gateway and the hook now both
  use `autonomy.json` in the user's config directory
  (`$RUNSPEC_PLUGIN_MANIFEST` still overrides it). The hook's not-found message
  no longer points at a script that doesn't exist.
- **The keychain error blamed a missing extra.** It said to install the
  `credentials` extra even when it was installed. It now names the real cause:
  on Linux, no Secret Service running and unlocked, the usual case over SSH or
  on a headless server. It also names the fixes.

### Added

- **`runspec-mcp setup claude|vscode|json`** prints the exact client config
  with absolute paths for the gateway binary and config. An MCP client starts
  the gateway with its own `PATH`, so a bare `runspec-mcp` in a venv silently
  fails to start; this was the most likely reason Claude Code never connected.
  - `setup claude --apply` runs `claude mcp add` for you.
  - `setup vscode` prints the `.vscode/mcp.json` entry for VS Code and GitHub
    Copilot.
- **`runspec-mcp check`** warns when `runspec-mcp` isn't on `PATH`, and when
  credentials are configured but no keychain is available.
- `--version` and the version the gateway reports to MCP clients come from the
  installed package. They were hard-coded to `0.1.0`.

## [0.1.4] — 2026-09-30

### Fixed

- **Security: secrets no longer go on a command line** (runspec >= 0.67.0).
  - The vendored executor is updated the same way as runspec-console 0.305.0.
    Remote and escalated runs now pass credential variables and the secret
    channel as the SPEC env frame on stdin, instead of `KEY=val` or
    `sudo … env KEY=val` in the command line. A command line is readable by
    every user on the host, and `sudo` logs it.
  - Adds `remote_exec_command()`, kept identical to the console's by
    `tests/test_parity.py`.

## [0.1.3]

Isolate an invalid tool schema instead of blanking the whole fleet list. The
Anthropic API rejects the entire `tools` array over one invalid `inputSchema`
(`400 tools.N.custom.input_schema: ... must match JSON Schema draft 2020-12`), so
a single malformed runnable on any host made Claude Code see no fleet tools at
all. The catalog now drops (and logs) just the offending tool via
`runspec.schemacheck.schema_issue`, keeping the rest of the fleet usable.
Requires `runspec >= 0.54.0` (which adds `runspec.schemacheck`).

## [0.1.2]

Sync the vendored `configmerge` copy with runspec-console, fixing the
`test_configmerge_parity` drift:

- Add `runspec_evals.toml` (`("suite", "name")`) to `COLLECTION_FILES` so the
  eval-suite registry (runspec-console 0.245.0) merges by identity like the other
  synced collection files instead of being deep-overlaid.

No API change; this only re-aligns the vendored pure helpers with their source
of truth.

## [0.1.1]

Sync the vendored `configmerge` copy with runspec-console, fixing the
`test_configmerge_parity` drift:

- Add `runspec_sounds.toml` (`("sound", "id")`) to `COLLECTION_FILES` so the
  named-sound registry (runspec-console 0.243.0) merges by identity like the
  other synced collection files instead of being deep-overlaid.
- Add `config.toml:mailbox.folders` to `DEFAULT_PRESERVE` so that machine-local
  key is pinned to the local value on a config pull, matching console.

No API change; this only re-aligns the vendored pure helpers with their source
of truth.

## [0.1.0]

Initial release. A local MCP **gateway** that connects Claude Code to local and
remote runspec runnables from one config file.

- Aggregating MCP stdio server (`runspec-mcp serve`) over local venvs and remote
  SSH venvs, discovering runnables via `runspec local --format json` and expanding
  them into leaf MCP tools identical to `runspec serve`.
- **Live discovery**: declares `tools.listChanged` and pushes
  `notifications/tools/list_changed` from a background discovery thread — the tool
  list stays current with no restart or manual refresh.
- **Credential injection** as name-derived environment variables (reusing the
  runspec-console convention; secrets in the OS keychain, shared service name),
  auto-matched to runnables by their declared arg `env` or by scope globs.
- One config file (`runspec_mcp.toml`), or reuse of an existing runspec-console
  config directory (`--config-dir`), or a bundled **config seed** package.
- **Managed SSH keypair**: the gateway generates and uses its own ed25519 key as
  the default SSH identity (no `~/.ssh` setup needed); `runspec-mcp copy-key`
  installs the public key on hosts over a one-time password login, and
  `runspec-mcp key` shows / rotates it. A per-host `identity_file` and the
  ssh-agent / default keys still work.
- Ships a Claude Code plugin (MCP server entry + autonomy PreToolUse hook, whose
  manifest the gateway keeps fresh + slash commands).
- Transport/credentials/config layer vendored from runspec-console, kept
  behaviour-identical by parity tests.
