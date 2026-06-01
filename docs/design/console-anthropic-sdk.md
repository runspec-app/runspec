# runspec-console — LLM Adapter Improvements

## Context

Two small features are needed in the `anthropic` LLM provider to support custom
base URLs (e.g. corporate AI proxies) and dynamic API key refresh via an external
helper command.

---

## 1 — `base_url` support in `AnthropicAdapter`

**File:** `runspec_console/adapters/anthropic.py`

`AnthropicAdapter.__init__` should accept an optional `base_url: str | None = None`
and pass it to `anthropic.AsyncAnthropic(...)`.

`bridge.py` already reads `base_url` from `config.toml → [llm]` and passes it
through `load_adapter(**kwargs)` — it silently gets dropped today because the
adapter doesn't declare it.

**Change:** add `base_url` parameter and forward it to the client constructor:

```python
def __init__(
    self,
    model: str = DEFAULT_MODEL,
    system: str = DEFAULT_SYSTEM,
    api_key: str | None = None,
    base_url: str | None = None,
) -> None:
    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    self.client = anthropic.AsyncAnthropic(**kwargs)
    self.model = model
    self.system = system
```

---

## 2 — `api_key_command` with TTL caching in `AnthropicAdapter`

**File:** `runspec_console/adapters/anthropic.py`

Add two optional constructor parameters:

- `api_key_command: str | None = None` — a shell command whose **stdout** (stripped)
  is used as the `api_key`. Intended for token-vending helpers, credential managers,
  etc. Mirrors the `apiKeyHelper` pattern in Claude Code.
- `api_key_ttl_ms: int = 0` — how long (milliseconds) to cache the result before
  re-running the command. `0` means re-run on every request.

When `api_key_command` is set, `api_key` (direct string) is ignored.

The adapter stores `_cached_key: str | None` and `_key_fetched_at: float` (epoch
seconds). Before each API call it checks whether the cache has expired and, if so,
re-runs the command via `subprocess.run(..., capture_output=True, text=True, shell=True)`
and builds a fresh `AsyncAnthropic` client with the new key.

Add a private helper method `_refresh_key_if_needed(self) -> None` and call it at
the top of `chat`, `stream_chat`, and `stream_with_tools`.

```python
import subprocess
import time

class AnthropicAdapter(ModelAdapter):
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        system: str = DEFAULT_SYSTEM,
        api_key: str | None = None,
        base_url: str | None = None,
        api_key_command: str | None = None,
        api_key_ttl_ms: int = 0,
    ) -> None:
        self._api_key_command = api_key_command
        self._api_key_ttl_s = api_key_ttl_ms / 1000.0
        self._cached_key: str | None = None
        self._key_fetched_at: float = 0.0
        self._base_url = base_url
        self.model = model
        self.system = system

        if api_key_command:
            self.client = None  # built lazily on first refresh
        else:
            kwargs: dict = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self.client = anthropic.AsyncAnthropic(**kwargs)

    def _refresh_key_if_needed(self) -> None:
        if not self._api_key_command:
            return
        now = time.monotonic()
        if (
            self._cached_key is not None
            and self._api_key_ttl_s > 0
            and (now - self._key_fetched_at) < self._api_key_ttl_s
        ):
            return  # still valid
        r = subprocess.run(
            self._api_key_command,
            capture_output=True, text=True, shell=True
        )
        key = r.stdout.strip()
        if not key:
            raise RuntimeError(
                f"api_key_command returned no output (exit {r.returncode}): "
                f"{r.stderr.strip()}"
            )
        self._cached_key = key
        self._key_fetched_at = now
        build_kwargs: dict = {"api_key": key}
        if self._base_url:
            build_kwargs["base_url"] = self._base_url
        self.client = anthropic.AsyncAnthropic(**build_kwargs)
```

Call `self._refresh_key_if_needed()` as the first line of `chat`,
`stream_chat`, and `stream_with_tools`.

---

## 3 — Pass new config keys through `bridge.py`

**File:** `runspec_console/bridge.py`

In `_get_adapter()`, the block that builds `kwargs` already handles `api_key`,
`model`, `base_url`, and `aws_region`. Add the same pattern for the two new keys:

```python
if llm_cfg.get("api_key_command"):
    kwargs["api_key_command"] = llm_cfg["api_key_command"]
if llm_cfg.get("api_key_ttl_ms") is not None:
    kwargs["api_key_ttl_ms"] = int(llm_cfg["api_key_ttl_ms"])
```

**Also:** when `api_key_command` is present in the config, do **not** cache
`self._adapter`. Return a fresh adapter each call so the TTL logic inside the
adapter controls caching rather than the bridge-level cache:

```python
adapter = load_adapter(provider, **kwargs)
if not llm_cfg.get("api_key_command"):
    self._adapter = adapter   # cache only for static-key configs
return adapter
```

---

## 4 — Backwards compatibility

`api_key` (direct string in config) must continue to work exactly as today when
`api_key_command` is not set. No existing behaviour changes.

---

## Config examples

### Direct API key (existing behaviour, unchanged)

```toml
[llm]
provider = "anthropic"
model    = "claude-sonnet-4-6"
api_key  = "sk-ant-..."
```

### Dynamic key via helper command + custom base URL (new)

```toml
[llm]
provider        = "anthropic"
model           = "claude-sonnet-4-6"
base_url        = "https://my-ai-proxy.example.com"
api_key_command = "my-token-helper"
api_key_ttl_ms  = 3600000
```

---

## Tests to add / update

- `AnthropicAdapter` constructed with `base_url` passes it to `AsyncAnthropic`
- `AnthropicAdapter` constructed with `api_key_command` calls the command and uses
  stdout as the key
- Key is refreshed after TTL expires but not before
- `api_key_command` returning empty stdout raises `RuntimeError`
- `bridge._get_adapter()` does not cache when `api_key_command` is set
- Existing `api_key` path is unaffected

---

## Resolution

Implemented as specified. The plan's file paths and symbols matched the codebase
(`runspec_console/adapters/anthropic.py`, `bridge.py::_get_adapter`,
`adapters/base.py::load_adapter`).

- **`adapters/anthropic.py`** — `AnthropicAdapter.__init__` gains `base_url`,
  `api_key_command`, and `api_key_ttl_ms`. A `_client_kwargs()` helper forwards
  `base_url` to `anthropic.AsyncAnthropic`. `_refresh_key_if_needed()` re-runs the
  command (TTL-gated via `time.monotonic`) and rebuilds the client; it's called at
  the top of `chat`, `stream_chat`, and `stream_with_tools`. Empty stdout raises
  `RuntimeError`. The client is built lazily when a command is configured.
- **`bridge.py`** — `_get_adapter()` threads `api_key_command` / `api_key_ttl_ms`
  through `load_adapter`, and skips the bridge-level adapter cache when a command
  is set so the adapter's own TTL governs refresh.
- **Tests** — `tests/test_anthropic_adapter.py` (10 cases: base_url forwarding,
  lazy build, command stdout → key, TTL cache/expiry/zero, empty-stdout error,
  `chat()` triggers refresh) and `tests/test_bridge_adapter.py` (3 cases: static
  key cached, command not cached + kwargs threaded, no-provider returns None).
  These mock `anthropic.AsyncAnthropic` and import only the adapter, so they run
  on Linux without the package's Windows-only deps.

**Notes for the maintainer**
- `api_key_command` runs via `subprocess.run(..., shell=True)`, mirroring Claude
  Code's `apiKeyHelper`. The command comes from local `config.toml` (user-trusted).
- The refresh is a synchronous `subprocess.run` inside the async methods (per the
  plan). For a local token helper this is a brief, infrequent call; if a slow helper
  ever blocks the event loop noticeably, move it to a thread executor.
- runspec-console has no CI quality gate (no job in `ci.yml`; `console-release.yml`
  only builds + smoke-tests the wheel), so lint/format/types/tests were run locally.
- Opportunity (not in scope): `chat`/`stream_*` re-send the fixed `system` prompt
  each call — adding `cache_control` to it would cut input cost/latency. Tracked
  separately if wanted.

