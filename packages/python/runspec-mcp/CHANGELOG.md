# Changelog

All notable changes to `runspec-mcp` are documented here.

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
