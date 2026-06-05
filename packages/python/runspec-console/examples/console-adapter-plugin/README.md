# Example: a custom LLM provider plugin for runspec-console

A copy-paste starting point for an **out-of-tree** LLM adapter. Use this when
your model access is an internal/proprietary gateway whose adapter code must
live in a **private** repo, not in open-source runspec-console.

**Building this with an AI agent (e.g. Claude via GitHub Copilot)?**
`AGENTS.md` is a self-contained brief — paste it into
`.github/copilot-instructions.md` in your repo so Copilot loads it, and use
`.github/prompts/create-console-plugin.prompt.md` as the task prompt. Validate
the result with the conformance helpers (see *Validate* below).

runspec-console resolves a `[llm] provider` name from three sources, so you
don't fork or patch the host package:

| Mechanism | When to use | How |
|---|---|---|
| **Entry point** (recommended) | You ship a wheel to an internal index | Declare the `runspec_console.adapters` group in `pyproject.toml`. `pip install`, then `provider = "mycorp"`. |
| **`register_adapter()`** | You self-register on import | Call it from a module listed under `[plugins] modules` in the config. |
| **Dotted path** | Quick spike, no packaging | `provider = "console_adapter_plugin.adapter:MyCorpAdapter"`. |

## The contract

Implement `runspec_console.ModelAdapter`. It's dependency-free, so your plugin
depends on `runspec-console` only for the base classes — none of the host's
optional SDKs leak in. Your adapter factory (an adapter class works directly)
receives the kwargs runspec-console threads from `[llm]`: `api_key`, `model`,
`system`, `base_url`, `api_key_command`, `api_key_ttl_ms`, plus any custom keys
you read yourself.

Required methods: `chat()`, `stream_chat()`, `make_tool_turn()`.
`stream_with_tools()` has a correct non-streaming default. `chat()` must set
`stop_reason="tool_use"` whenever it returns tool calls — that's how the agent
loop knows to run them.

If your gateway is "LangServe-ish," subclass `LangServeAdapter` and override
only `_build_payload` / `_parse_output` (see `adapter.py`) to inherit auth,
token rotation, and the streaming/tool plumbing.

## Validate

Before wiring the plugin into the console, run the conformance helpers against a
fake HTTP client (no live gateway needed):

```python
from runspec_console.adapters.testing import (
    assert_adapter_contract, assert_chat_response, assert_tool_turn,
)
```

`tests/test_adapter_conformance.py` is a complete example. `pytest` green means
the adapter satisfies the contract (correct types, the `stop_reason="tool_use"`
invariant, a well-formed `make_tool_turn`).

## Build & install

```bash
# rename the package, fill in adapter.py, then:
python -m build           # produces dist/*.whl
pip install dist/*.whl    # into the same venv as runspec-console
```

Then in runspec-console: **Settings → LLM**, pick your provider (installed
plugins appear in the dropdown automatically), or hand-edit
`%APPDATA%\runspec-console\runspec_config.toml`:

```toml
[llm]
provider = "mycorp"
base_url = "https://gateway.corp/my-chain"
api_key_command = "your-token-vendor --print"   # rotating bearer token
api_key_ttl_ms  = 300000
```

> This example is **not** built, installed, or tested as part of runspec-console
> — it's a template to copy into your own repo.
