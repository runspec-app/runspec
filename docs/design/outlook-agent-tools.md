# Outlook Classic agent tools (desktop COM)

## Why

The chat agent could already *react* to Outlook (the `outlook` trigger source
fires `new_mail` / `reminder` events, and helpdesk replies use `save_reply_draft`
/ `send_draft`), but it had no way to *use* Outlook on demand: read the inbox,
search, check the calendar, or send a fresh mail. The Microsoft Graph runnables
in `runspec-windows` nominally cover reads, but they authenticate with the MSAL
**device-code** flow, which Conditional Access / locked-down corporate tenants
routinely block.

Outlook **Classic** (the desktop app) sidesteps that: it's already signed in with
the org's SSO session, and the console already drives it over COM (pywin32) for
monitoring, the rota calendar, and drafts. So the agent capability is built on
that proven COM layer — no new auth, no new package, works where Graph can't.

## What

Six built-in agent tools (in `bridge.py`, schemas from `_outlook_tools()`,
executed by `_run_outlook_tool()`), offered only when `sources.outlook.outlook_available()`
is true (Windows + pywin32):

| Tool | Autonomy | Backed by (`sources/outlook.py`) |
|---|---|---|
| `outlook_inbox` | autonomous (read) | `list_messages` |
| `outlook_search` | autonomous (read) | `search_messages` (DASL subject/body `LIKE`) |
| `outlook_calendar` | autonomous (read) | `upcoming_events` |
| `outlook_reminders` | autonomous (read) | `list_reminders` |
| `outlook_draft` | confirm | `save_reply_draft` / `save_new_draft` |
| `outlook_send` | confirm | `send_draft` / `send_new_mail` |

Reads are autonomous because they're side-effect-free local COM calls; **send is
confirm-gated** so the operator approves each irreversible send. `from_mailbox`
sets `SentOnBehalfOfName` (send-as / on-behalf, per the Exchange permission).

The compact row shapers (`_mail_summary`, `_event_row`, `_search_dasl`) are pure
and unit-tested with fakes on any OS; the COM calls are Windows-only and guarded.

## Awareness

`_console_context()` (the per-turn awareness block from the rota/triggers work)
now names the monitored mailbox/folder and the available `outlook_*` tools when
Outlook is reachable, so the agent proactively knows it can read/draft/send.
`console_status` inherits it. Nothing is emitted when no mailbox is configured,
preserving the prompt-cached fast path.

## Out of scope (planned follow-up)

This change is purely additive. Retiring the now-redundant Graph runnables is a
separate PR because it touches the published `runspec-windows` package:

- Remove the Graph **Outlook** (`outlook-unread/search/folders`) and **Calendar**
  (`calendar-upcoming/today`, `next-meeting`) runnables — COM replaces them.
- Decide the fate of **Teams** and **OneDrive** Graph runnables — there is no
  Outlook-COM equivalent, so they'd be dropped, not replaced.
- Only then drop the console's `runspec-windows[graph]` extra (the `[graph]`
  extra still backs Teams/OneDrive until those are resolved, so it stays for now).
