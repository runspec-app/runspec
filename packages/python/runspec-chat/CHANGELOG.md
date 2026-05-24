# Changelog

## [0.4.0] — 2026-05-24

### Added

- **Chainlit flag pass-through** — the following Chainlit `run` options are now
  exposed as `runspec-chat` arguments:
  - `--host` (default `0.0.0.0`) — network interface to bind to (was hardcoded)
  - `-w` / `--watch` — reload on source file changes (dev workflow)
  - `--headless` — suppress auto-opening the browser on startup
  - `--root-path` — root path when serving behind a reverse proxy
  - `--ssl-cert` / `--ssl-key` — SSL certificate and key for HTTPS

---

## [0.3.0] — 2026-05-24

### Added

- **Tool categories** — each connection and slash-command description now shows
  its category. Add an optional `category` field to a host in `jump_hosts.toml`;
  omit it and the host name is used as the fallback. Local tools always show
  `[local]`. Connection messages include a `── {category} ──` header line.
  Slash-command autocomplete prefixes every description with `[category]`.
  Name your Chainlit connection identically to the host key in `jump_hosts.toml`
  so the category resolves automatically.

- **Session user identity** — the OS identity of the person who started
  runspec-chat is resolved at startup and injected into the system prompt so
  the LLM is aware of who it's assisting. The running-as label appears in the
  local startup message (`running as **Jason Finestone**`). Available in
  `user_session["user_identity"]` for future flow context and activity feed
  features. Resolution order: Windows AD display name via `pywin32`
  (`GetUserNameEx(NameDisplay)`); Linux/Mac GECOS field from `/etc/passwd`;
  OS username as final fallback. `pywin32` is now an automatic dependency on
  Windows installs.

---

## [0.2.0] — 2026-05-24

### Changed

- `hosts.toml` renamed to `jump_hosts.toml` (`~/.config/runspec-chat/jump_hosts.toml`).
  A deprecation warning is printed to stderr if the old filename is found and
  the new one is not, so existing configs keep working without any changes.
  Rename the file to suppress the warning.

---

## [0.1.0] — 2026-05-22

### First release

- Chainlit-based browser UI — runs locally, browser is just the UI
- Local `runspec serve` starts automatically on chat start; any runspec tool
  pip-installed into the same venv is discovered and available immediately
- Remote hosts connected via the plug icon (MCP stdio over SSH)
- Natural language → tool use via Anthropic adapter (default: `claude-haiku-4-5`)
- Slash commands — type `/` for autocomplete, call any tool directly without
  the LLM; autocomplete updates live as hosts connect and disconnect
- `--help` on any slash command shows the tool's declared arguments from its
  MCP schema
- `/setup-keys` — generates an ed25519 SSH key and copies it to all configured
  hosts using passwords entered in browser Settings
- Browser Settings (⚙) — masked fields for `ANTHROPIC_API_KEY` and SSH
  passwords; session-scoped, never written to disk
- `hosts.toml` — `[config].user` as default SSH username; per-host `user`
  override generates a dedicated `SSH_<HOST>_PASS` field in Settings
- `RUNSPEC_CHAT_MODEL` env var and `--model` arg to override the LLM model
- Windows support: `setup-keys` detects missing `sshpass`/`ssh-copy-id` and
  shows manual key-copy instructions instead

---
