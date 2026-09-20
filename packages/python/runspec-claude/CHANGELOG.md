# runspec-claude Changelog

## [0.1.1] — 2026-09-20

### Changed

- **Packaging metadata for the runspec-console Marketplace.** Add a README
  (rendered in the Marketplace card's Details view) and publish `keywords`
  and Documentation / Source / Changelog / Issues project URLs, so the
  package's Marketplace card shows a description, tags and links. No
  runnable, argument, or behaviour changes.

## [0.1.0] — 2026-06-22

Initial release.

One runnable, `claude-run`, that drives the **Claude Code CLI** (`claude`)
headlessly so you can use Claude — and let the runspec-console agent (itself
Claude) delegate to Claude — as a runspec tool. Four verbs (subcommands) mirror
the way you talk to Claude in a chat app:

- **`claude-run ask "<prompt>"`** — start a fresh headless turn / session.
- **`claude-run continue "<prompt>"`** — continue the most recent session in this
  directory (`claude --continue`).
- **`claude-run resume --session <id> "<prompt>"`** — continue a specific prior
  session by id (`claude --resume`).
- **`claude-run sessions`** — list recent sessions you can resume (read-only).

Each verb shells out to `claude -p … --output-format json` and returns the
structured result: `result` text, `session_id`, `num_turns`, `total_cost_usd`,
`is_error`, and the full `raw` payload. The returned `session_id` lets a caller
(or the console agent) resume the same delegated worker on a later turn — the
"drive yourself across turns" loop with one tool.

**MCP surface.** `runspec serve` flattens the runnable into one leaf tool per
verb — `claude-run_ask`, `claude-run_continue`, `claude-run_resume`,
`claude-run_sessions` — so the console agent sees four clearly-named tools while
the package stays a single installable entry point. Each verb tool also carries
the inherited global args (requires `runspec >= 0.44.0`), so an operator or the
agent can set `permission-mode` / `model` / etc. per call — still behind the
`confirm` gate.

**Safe-by-default, operator-governed.** Every action verb is
`autonomy = "confirm"` (a human approves each delegation); `sessions` is
`autonomous`. The headless run defaults to `--permission-mode default` (it can
read and plan but blocks on edits/commands) with `--max-turns 12`. The delegated
run's power — `permission-mode`, `model`, working directory, `allowed-tools`,
`append-system-prompt`, `bare` — is set through global args (operator-controlled,
inherited by every verb), not by the autonomous agent: the agent's tools take a
prompt (and `session`/`limit`); a human or the run form chooses how much the
nested agent may do. Opt up explicitly, e.g.
`claude-run --permission-mode acceptEdits ask "…"`.

**Session listing** reads Claude Code's transcript store
(`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`, `CLAUDE_CONFIG_DIR`
honoured). The per-line schema is undocumented, so titles are best-effort
(sniffed from the first user message) and the lister degrades gracefully — an
absent directory yields an empty list.

**Entry point** is deliberately named `claude-run`, not `claude`, so installing
this package never shadows the real Claude Code CLI binary.

**Public Python API** for wrapper packages that want to bake in corporate
defaults (a fixed model, allowlist, or working directory): `run_claude`,
`list_sessions`, `encode_project_dir`, `ClaudeError`, `ClaudeNotFoundError`.
Every function takes plain parameters and raises typed errors — the same
core/wrapper split used by `runspec-jsm` / `runspec-logops`.

**Requirements.** The `claude` CLI must be installed on the host where the
runnable runs (it is invoked over `subprocess`). Zero extra runtime
dependencies beyond `runspec`.
