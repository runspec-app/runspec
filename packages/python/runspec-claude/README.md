# runspec-claude

Drives the **Claude Code CLI** (`claude`) headlessly as a
[runspec](https://pypi.org/project/runspec/) runnable — so the runspec-console
agent (itself Claude) can delegate a bounded sub-task and resume the same worker
on a later turn. "Claude driving Claude." `pip install` it into a venv and it's
discoverable by `runspec local`, `runspec serve` (MCP), and runspec-console.

One runnable — **`claude-run`** — with four verbs. Each shells out to
`claude -p … --output-format json` and returns
`{result, session_id, num_turns, total_cost_usd, is_error, raw}`. `runspec serve`
flattens it into one leaf MCP tool per verb (`claude-run_ask`, `_continue`,
`_resume`, `_sessions`).

> Requires the `claude` CLI on the host. The entry point is `claude-run`, **not**
> `claude`, so it never shadows the real CLI. Zero extra runtime dependencies.

## Install

```
pip install runspec-claude
```

## Verbs

| Verb | Autonomy | What it does |
|---|---|---|
| `ask` | confirm | A fresh headless turn (`claude -p`). |
| `continue` | confirm | Continues the most-recent session (`claude --continue`). |
| `resume` | confirm | Resumes `--session <id>` (`claude --resume`). |
| `sessions` | autonomous | Lists `~/.claude/projects/<encoded-cwd>/*.jsonl` (CLAUDE_CONFIG_DIR-aware, best-effort titles). |

**Safe-by-default, operator-governed.** The delegate's power
(`--permission-mode` / `--model` / `--dir` / `--allowed-tools` /
`--append-system-prompt` / `--bare`) lives in inherited **global** args —
operator/run-form controlled, not set by the autonomous agent (whose tools take
only a prompt + `session`/`limit`). Defaults are `--permission-mode default` +
`--max-turns 12`; opt up explicitly:

```
claude-run ask "Summarise the failing tests in this repo"
claude-run --permission-mode acceptEdits --dir /srv/app ask "Fix the lint errors"
claude-run resume --session 4f3c… "Now add a test for that fix"
claude-run sessions --limit 10
```

## Public Python API

```python
from runspec_claude import run_claude, list_sessions, ClaudeError

result = run_claude(prompt="Summarise the diff", permission_mode="default", max_turns=12)
print(result["session_id"], result["result"])
```

`run_claude` / `list_sessions` / `encode_project_dir` are exported for
corporate-wrapper packages; failures raise `ClaudeError` (or
`ClaudeNotFoundError` when the `claude` CLI is missing).

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```
