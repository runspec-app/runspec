# runspec-o365

Microsoft 365 (Outlook · Teams · Calendar · OneDrive) runnables for
[runspec](https://pypi.org/project/runspec/). `pip install` it into a venv and
the runnables are discoverable by `runspec local`, `runspec serve` (MCP), and
runspec-console.

These runnables used to live in
[`runspec-windows`](../runspec-windows) (behind its `[graph]` extra). They were
split into this standalone package so a console / fleet where Microsoft Graph is
unusable — blocked by tenant policy — doesn't carry unusable tools. runspec-console no
longer depends on them; install this package explicitly to opt in.

They are **pure HTTP** via Microsoft Graph (no pywin32, no client secret), so
they run on any OS — they only need a token.

All runnables emit JSON on stdout. Read-only runnables are `autonomy =
"autonomous"`; `graph-login` is `autonomy = "confirm"`.

## Install

```
pip install runspec-o365
```

## Runnables

| Group | Runnables |
|---|---|
| Account | `graph-login`, `graph-whoami` |
| Outlook | `outlook-unread`, `outlook-search`, `outlook-folders` |
| Calendar | `calendar-upcoming`, `calendar-today`, `next-meeting` |
| OneDrive | `onedrive-recent`, `onedrive-search`, `onedrive-list` |
| Teams | `teams-list-chats`, `teams-read-chat` |

## Microsoft 365 — one-time setup

The Graph runnables authenticate as the signed-in user via the OAuth
**device-code** flow (no client secret, no admin app password). You need a
public-client **app registration** in your Entra ID (Azure AD) tenant:

1. **Entra admin center → App registrations → New registration.** Give it a
   name; under *Supported account types* pick whatever matches your tenant.
2. **Authentication → Advanced settings → Allow public client flows → Yes.**
   (Device-code flow requires this.)
3. **API permissions → Add → Microsoft Graph → Delegated**, add
   `User.Read`, `Mail.Read`, `Chat.Read`, `Calendars.Read`, `Files.Read.All`,
   then *Grant admin consent* if your tenant requires it.
4. Copy the **Application (client) ID**.

Then point the package at it (the client id is **not** a secret):

```
set RUNSPEC_GRAPH_CLIENT_ID=<application-client-id>
set RUNSPEC_GRAPH_TENANT=<tenant-id-or-domain>   # optional; default "organizations"
```

…or create `%APPDATA%\runspec-o365\graph.toml`:

```toml
client_id = "00000000-0000-0000-0000-000000000000"
tenant    = "contoso.onmicrosoft.com"
```

Sign in once (prints a code + URL to visit; the token is cached under
`%APPDATA%\runspec-o365\msal_cache.bin`):

```
graph-login
graph-whoami
outlook-unread --count 10
outlook-search --query "invoice"
teams-list-chats
teams-read-chat --chat-id 19:abc...@thread.v2
calendar-upcoming --days 7
calendar-today
next-meeting
onedrive-recent
onedrive-search --query "budget"
onedrive-list --path "/Documents"
```

Tokens are cached and refreshed silently; rerun `graph-login` if the cache is
cleared or consent changes.

> **Migrating from `runspec-windows[graph]`?** The token cache moved from
> `%APPDATA%\runspec-windows\` to `%APPDATA%\runspec-o365\`, so run `graph-login`
> once after installing. The `RUNSPEC_GRAPH_*` env vars are unchanged.

## Public Python API

This package exports the authenticated Graph client so you can build your own
wrapper runnables:

```python
from runspec_o365 import graph_get, get_token

me = graph_get("/me")
events = graph_get("/me/events", params={"$top": 5})
```

## Development

```
python -m venv .venv && . .venv/bin/activate   # or .venv\Scripts\activate
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy runspec_o365/
pytest
```

The Graph formatters and auth/client helpers are unit-tested on any platform
(no live tenant needed).
