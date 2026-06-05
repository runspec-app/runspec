# runspec-console

A desktop console for [runspec](https://pypi.org/project/runspec/) — run,
schedule, and monitor runnables from a graphical UI. Built on
[pywebview](https://pywebview.flowrl.com/) with a Vite/React frontend, it
discovers every runnable installed in its environment (via `runspec local`) and
can also reach runnables on remote hosts over SSH.

## Install

```
pip install runspec-console
runspec-console
```

On **Windows**, installing the console automatically pulls in
[`runspec-windows`](https://pypi.org/project/runspec-windows/) (with its
`[graph]` extra), so its Windows system-administration and Microsoft 365
runnables appear in the UI with no extra step. The dependency is gated to
`sys_platform == "win32"`, so it's skipped on macOS/Linux.

To add more runnables, just `pip install` the package that provides them into
the same environment (e.g. `runspec-linux`) — the console picks them up on the
next launch.

## Where runnables come from

The console lists whatever `runspec local` can discover: any installed package
that ships a `runspec.toml` and entry points. There's no registry and no
filesystem scanning — `pip install` (or `pip install -e .`) is how a runnable
becomes visible.

## Microsoft 365 runnables (Outlook · Teams · Calendar · OneDrive)

These ship in `runspec-windows` and authenticate as the signed-in user via the
Microsoft Graph **device-code** flow (no client secret). The full setup lives in
the [runspec-windows README](https://pypi.org/project/runspec-windows/); the
short version:

1. **Register an app** in your Entra (Azure AD) tenant
   (*App registrations → New registration*).
2. **Authentication → "Allow public client flows" → Yes** — device-code login
   needs this.
3. **API permissions → Microsoft Graph → Delegated**: add `User.Read`,
   `Mail.Read`, `Chat.Read`, `Calendars.Read`, `Files.Read.All`, then grant
   consent.
4. Copy the **Application (client) ID** and **Directory (tenant) ID**.
5. Drop them in `%APPDATA%\runspec-windows\graph.toml` (the client id is not a
   secret):

   ```toml
   client_id = "00000000-0000-0000-0000-000000000000"
   tenant    = "yourcompany.onmicrosoft.com"
   ```

   For a **personal** Microsoft account (e.g. trying it at home), register the
   app for "personal Microsoft accounts" and set `tenant = "consumers"` (or
   `"common"`) instead of a tenant domain.
6. Run `graph-login` once (it prints a URL + code to authenticate); the token
   caches and refreshes silently thereafter.

> **In a managed tenant:** `Chat.Read` (Teams) is typically **admin-consent-required**, so a
> tenant admin may need to grant consent on the app registration. The
> mail/calendar/files scopes are usually user-consentable.

## AI assistant (optional)

The console can drive runnables through an LLM. Install the provider extra you
want:

```
pip install "runspec-console[anthropic]"   # Claude
pip install "runspec-console[openai]"      # OpenAI
pip install "runspec-console[bedrock]"     # Claude via AWS Bedrock
pip install "runspec-console[langserve]"   # corporate LLM gateway (LangServe /invoke)
```

### LangServe gateway (no Anthropic/OpenAI account)

If your only model access is a corporate **LLM gateway** that exposes a
LangServe `/invoke` route (commonly Bedrock/Claude underneath), pick the
`langserve` provider. Configure it in Settings → LLM, or hand-edit
`%APPDATA%\runspec-console\runspec_config.toml`:

```toml
[llm]
provider = "langserve"
base_url = "https://gateway.corp/my-chain"   # /invoke is appended automatically
model    = ""                                 # the published chain usually pins its model
# Static token:
api_key  = "..."
# …or a short-lived token re-fetched on a TTL (rotating corporate creds):
# api_key_command = "aws-vend-token --print"
# api_key_ttl_ms  = 300000
```

Tool calling is structured — runspec tools are sent to the gateway and the
model's tool calls are executed exactly as with the native providers. The two
parts that vary per-gateway are knobs under `[llm]`, adjustable once you've seen
the endpoint's `GET /input_schema`:

| key | default | meaning |
|---|---|---|
| `input_messages_key` | `"messages"` | key the chat history is sent under, inside `input` |
| `input_tools_key` | `"tools"` | key the tool schemas are sent under |
| `tools_in_config` | `false` | when `true`, tools move to `config.configurable.<key>` instead of `input` |
| `auth_header` | `"Authorization"` | header the token is sent in |
| `auth_scheme` | `"Bearer"` | scheme prefix; set to `""` to send the raw token |

### Custom providers (plugins)

When a model gateway is proprietary and its adapter must stay in a **private**
repo, ship it as a plugin instead of forking the console. A plugin implements
`runspec_console.ModelAdapter` (dependency-free, so no SDKs leak in) and is
resolved under a `[llm] provider` name three ways:

- **Entry point** (recommended) — your wheel advertises the
  `runspec_console.adapters` group; `pip install` it and the provider appears
  (selectable in Settings → LLM).
- **`register_adapter(name, factory)`** — call it from a module imported at
  startup via `[plugins] modules = ["your_pkg"]`.
- **Dotted path** — `provider = "your_pkg.module:AdapterClass"`, no packaging.

A "LangServe-ish but not vanilla" gateway can subclass `LangServeAdapter` and
override only `_build_payload` / `_parse_output`. See
[`examples/console-adapter-plugin/`](examples/console-adapter-plugin/) for a
ready-to-copy template — it includes an `AGENTS.md` brief and a Copilot prompt
file for building the adapter with an AI agent, and conformance helpers
(`runspec_console.adapters.testing`) to validate it without a live gateway.

## Usage

```
runspec-console              # production (bundled UI)
runspec-console --dev        # point pywebview at a running Vite dev server
runspec-console --devtools   # enable the Chromium inspector in a prod build
```

See the main [runspec docs](https://github.com/JasonFinestone/runspec) for the
runspec format, the SSH jump-host model, and logging.
