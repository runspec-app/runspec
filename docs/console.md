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

## Event triggers (Windows)

Triggers let the console **react to things that happen on the machine** and run
runspec runnables — a new email, a meeting reminder, a Teams/Slack notification.
The **Triggers** tab shows your configured triggers and a kanban board where each
matched event flows across columns: **Triggered → Triage → Awaiting human →
Running → Done / Failed**.

A trigger binds an **event source** + **event type** (optionally narrowed by a
regex `match`) to an **action**. The action is either a deterministic runnable
call (`mode = "rule"`) or a headless agent turn (`mode = "agent"`) that follows
the trigger's `rules` with the full runnable toolset. Either way, every runnable a
trigger drives goes through the **same autonomy gate as chat** — confirm-by-default,
so a card parks in *Awaiting human* with inline Approve / Deny.

> Triggers are a **Windows** feature today and only fire while the console is
> running (they never replay a backlog — see *forward-only* below). An LLM
> provider must be configured (above) for `mode = "agent"` triggers.

### Event sources

| Source (`source =`) | Events (`event =`) | Notes |
|---|---|---|
| `outlook`       | `new_mail`, `reminder` | Uses the **signed-in desktop Outlook** client over COM — no extra dependency, and it rides your existing SSO session. |
| `notifications` | `toast`                | Every system toast (Teams, Slack, anything) via one listener. Needs the `notifications` extra **and** a one-time Windows consent grant. |

```bash
pip install "runspec-console[notifications]"   # enables the toast source
```

The Triggers tab shows each source's availability and the reason any is off
(not on Windows, `winsdk` not installed, consent not granted, …).

### Configuring triggers

Triggers live in `runspec_triggers.toml` in the app config directory
(`%APPDATA%\runspec-console\`). Each `[[triggers]]` table is one trigger:

```toml
[[triggers]]
name   = "vip-outage-triage"
source = "outlook"
event  = "new_mail"
mode   = "agent"               # "agent" (LLM-in-the-loop) | "rule" (direct runnable)
autonomy = "confirm"           # gate level (default: confirm)
post_to_chat = false           # also mirror this run into the Console tab

# Cheap regex pre-filter on the event payload — all patterns must match. Keeps
# the LLM out of irrelevant events (and tames a chatty `reminder` source).
[triggers.match]
from    = ".*@bigcustomer\\.com"
subject = "(?i)outage|down|urgent"

# agent mode: the rules the model follows for this trigger.
rules = """
If this reports an outage, identify the affected host, run the diagnostics
runnable against it, and draft a short reply summarising findings. Never
restart a service or send mail without confirmation.
"""

# Optional: let the agent save a suggested reply to Outlook Drafts (never sends).
[triggers.reply]
draft = true
style = "Concise, professional. Acknowledge and state the next step."
```

For a deterministic, no-LLM trigger, use `mode = "rule"` and map event-payload
fields onto the runnable's args with `{{ field }}`:

```toml
[[triggers]]
name   = "log-incoming-ticket"
source = "outlook"
event  = "new_mail"
mode   = "rule"
run    = "local__record-ticket"     # host__runnable
[triggers.map]
summary = "{{ subject }}"
sender  = "{{ from }}"
```

Event payload fields you can `match` on / map from:

- `outlook` `new_mail`: `from`, `to`, `subject`, `body`, `received`, `categories`, `entry_id`
- `outlook` `reminder`: `subject`, `start`, `location`, `is_meeting`, `organizer`
- `notifications` `toast`: `app`, `title`, `body`, `text`

### draft_reply — suggested replies

With `[triggers.reply] draft = true` on an `agent`-mode `new_mail` trigger, the
model can call a built-in `draft_reply` tool that **saves a reply to your Outlook
Drafts folder** (the agent's text above the quoted original). The console
**never sends mail** — you review the draft in Outlook and send it yourself, and
each draft save is confirm-gated. The tool is only offered when both the trigger
enables it and the event is a mail with something to reply to.

### Safe by default

- **Confirm-by-default.** Trigger-driven runnables use the same autonomy gate as
  chat; a `manual` runnable is still never run by the agent. Approve / Deny is
  surfaced on the card (the *Awaiting human* column).
- **Forward-only.** Sources only react to events that arrive **while the console
  is running** — your inbox / existing toasts are snapshotted at startup and
  never replayed, so reopening the console doesn't re-handle old messages.
- **Kill switch.** Toggle any trigger off in the Triggers tab (it stops
  immediately); delete it from `runspec_triggers.toml` to remove it.

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
