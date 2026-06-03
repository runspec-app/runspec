# runspec-windows

Windows system-administration and Microsoft 365 (Outlook + Teams) runnables for
[runspec](https://pypi.org/project/runspec/). The Windows counterpart to
[`runspec-linux`](../runspec-linux): `pip install` it into a venv and the
runnables are discoverable by `runspec local`, `runspec serve` (MCP), and
runspec-console.

All runnables emit JSON on stdout. Read-only runnables are `autonomy =
"autonomous"`; state-changing ones (`kill-process`, `restart-service`,
`stop-service`, `flush-dns`, `graph-login`) are `autonomy = "confirm"`.

## Install

```
pip install runspec-windows          # system utilities only
pip install "runspec-windows[graph]" # + Microsoft 365 (Outlook/Teams) runnables
```

## Runnables

| Group | Runnables |
|---|---|
| System | `system-info`, `disk-usage`, `check-memory`, `uptime` |
| Processes | `list-processes`, `process-info`, `kill-process` |
| Services | `list-services`, `check-service`, `restart-service`, `stop-service` |
| Network | `ping-host`, `check-port`, `show-connections`, `list-adapters`, `flush-dns` |
| Event log | `query-eventlog`, `recent-errors` |
| Scheduled tasks | `list-scheduled-tasks` |
| Installed software | `installed-software` |
| Sessions | `who`, `current-user` |
| Microsoft 365 | `graph-login`, `graph-whoami`, `outlook-unread`, `outlook-search`, `outlook-folders`, `teams-list-chats`, `teams-read-chat`, `calendar-upcoming`, `calendar-today`, `next-meeting`, `onedrive-recent`, `onedrive-search`, `onedrive-list` |

The system runnables are Windows-only; run one on macOS/Linux and it returns a
clean JSON error rather than a traceback. The Microsoft Graph runnables are pure
HTTP and run anywhere — they only need a token.

```
system-info
list-processes --filter chrome --limit 10
query-eventlog --log System --level error --count 20
installed-software --filter "Microsoft"
```

## Microsoft 365 (Outlook + Teams) — one-time setup

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

…or create `%APPDATA%\runspec-windows\graph.toml`:

```toml
client_id = "00000000-0000-0000-0000-000000000000"
tenant    = "contoso.onmicrosoft.com"
```

Sign in once (prints a code + URL to visit; the token is cached under
`%APPDATA%\runspec-windows\msal_cache.bin`):

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

## Public Python API

Mirroring `runspec-linux`'s `nc_send`, this package exports the authenticated
Graph client so you can build your own wrapper runnables:

```python
from runspec_windows import graph_get, get_token

me = graph_get("/me")
events = graph_get("/me/events", params={"$top": 5})
```

## Development

```
python -m venv .venv && . .venv/bin/activate   # or .venv\Scripts\activate
pip install -e ".[dev,graph]"
ruff check . && ruff format --check .
pytest
```

The OS-specific work sits behind thin helpers in `_platform.py`; the pure output
parsers (`tasklist`/`netstat`/`ipconfig`/`schtasks`/`query user`) and the Graph
formatters are unit-tested on any platform. Real end-to-end runs of the
Windows-only runnables happen on a Windows host / the `windows-latest` CI job.
