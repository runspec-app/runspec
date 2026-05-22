# Changelog

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
