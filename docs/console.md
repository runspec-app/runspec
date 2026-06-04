# Desktop Console

`runspec-console` is a desktop application for running, scheduling, and
monitoring your runspec runnables from a graphical UI. It's built on
[pywebview](https://pywebview.flowrl.com/) with a Vite/React frontend,
discovers every runnable installed in its environment, and can reach runnables
on remote hosts over SSH.

!!! info "Supersedes runspec-chat"
    runspec-console replaces the older Chainlit-based
    [runspec-chat](runspec-chat.md) UI. Prefer the console for new setups.

---

## Install

```bash
pip install runspec-console
runspec-console
```

On **Windows**, installing the console automatically pulls in
[`runspec-windows`](https://pypi.org/project/runspec-windows/) (with its
`[graph]` extra), so its Windows system-administration and Microsoft 365
runnables appear in the UI with no extra step. The dependency is gated to
`sys_platform == "win32"`, so it's skipped on macOS/Linux.

To add more runnables, `pip install` the package that provides them into the
same environment (e.g. `runspec-linux`) — the console picks them up on the next
launch.

### Where runnables come from

The console lists whatever `runspec local` can discover: any installed package
that ships a `runspec.toml` and entry points. There's no registry and no
filesystem scanning — `pip install` (or `pip install -e .`) is how a runnable
becomes visible.

---

## SSH connections & tuning

The console refreshes every host's connectivity and runnable inventory on a
background timer. To avoid hammering bastions/jump hosts with a burst of fresh
SSH handshakes (which trips `sshd MaxStartups` and surfaces as *"Error reading
SSH protocol banner"*), it keeps **one reused, keepalive'd SSH connection per
host** — shared by the connectivity probe, discovery, history, logs, and
invocations. A warm connection serves new commands over fresh channels with no
new handshake; a host going offline is detected on the next cycle and retried
with exponential backoff.

All knobs live in `%APPDATA%\runspec-console\config.toml` and have safe
defaults — you only need them for unusual topologies (very strict bastions,
large fleets, high-latency proxies):

```toml
[ssh]
# Connection reuse (issue #104). Set false for legacy connect-per-call.
pool         = true
keepalive    = 30     # seconds; transport keepalive (0 disables)
max_sessions = 8      # max concurrent channels per connection (< sshd MaxSessions)
idle_ttl     = 300    # seconds before an unused connection is closed

# Connect-phase timeouts (raise these behind slow proxies / VPNs).
connect_timeout = 10
banner_timeout  = 30
auth_timeout    = 30

[refresh]
interval       = 30   # seconds between refresh cycles
max_concurrent = 5    # max simultaneous NEW handshakes across the whole fleet
jitter         = 3    # +/- seconds of per-host start jitter
backoff_base   = 5    # seconds; per-host exponential backoff after a failure
backoff_max    = 300  # seconds cap
```

!!! tip "Behind a corporate proxy / middlebox"
    If a connection error mentions *"the remote sent non-SSH data on connect"*,
    a proxy or TLS middlebox is intercepting the SSH port. Point the console at
    an HTTP CONNECT proxy with `[ssh] proxy = "http://proxy.corp:8080"`, or set
    `use_ssh_config = true` to honour a `ProxyCommand` from `~/.ssh/config`.

---

## Microsoft 365 runnables (Outlook · Teams · Calendar · OneDrive)

These ship in `runspec-windows` and authenticate as the signed-in user via the
Microsoft Graph **device-code** flow (no client secret). One-time setup:

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

    ```ini
    client_id = "00000000-0000-0000-0000-000000000000"
    tenant    = "yourcompany.onmicrosoft.com"
    ```

6. Run `graph-login` once (it prints a URL + code to authenticate); the token
   caches and refreshes silently thereafter.

!!! tip "Trying it at home"
    For a **personal** Microsoft account, register the app for "personal
    Microsoft accounts" and set `tenant = "consumers"` (or `"common"`) instead
    of a tenant domain.

!!! warning "Admin consent in a managed tenant"
    `Chat.Read` (Teams) is typically **admin-consent-required**, so a tenant
    admin may need to grant consent on the app registration. The
    mail/calendar/files scopes are usually user-consentable.

The full app-registration walkthrough lives in the
[runspec-windows README](https://pypi.org/project/runspec-windows/).

---

## AI assistant (optional)

The console can drive runnables through an LLM. Install the provider extra you
want:

```bash
pip install "runspec-console[anthropic]"   # Claude
pip install "runspec-console[openai]"      # OpenAI
pip install "runspec-console[bedrock]"     # Claude via AWS Bedrock
```

---

## Output rendering

When a runnable prints JSON, the console renders it instead of dumping raw text:

- a **JSON array of objects** → a table
- a **dict that wraps array(s)-of-objects** → titled section tables
  (e.g. `{System:[…], Application:[…]}`, or scalar metadata plus one array of
  rows)
- a **plain dict** → a key/value view
- nested objects/arrays inside a row → an **expandable** sub-table or JSON

Each block has a table/raw toggle and a "copy as table" action. For the
cleanest tables, a runnable should emit a **top-level array of flat row
objects**; the console still renders nested shapes gracefully when it doesn't.

---

## Usage

```bash
runspec-console              # production (bundled UI)
runspec-console --dev        # point pywebview at a running Vite dev server
runspec-console --devtools   # enable the Chromium inspector in a prod build
```
