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

Fourteen built-in agent tools (in `bridge.py`, schemas from `_outlook_tools()`,
executed by `_run_outlook_tool()`), offered only when `sources.outlook.outlook_available()`
is true (Windows + pywin32):

| Tool | Autonomy | Backed by (`sources/outlook.py`) |
|---|---|---|
| `outlook_inbox` | autonomous (read) | `list_messages` |
| `outlook_search` | autonomous (read) | `search_messages` (DASL subject/body `LIKE`) |
| `outlook_get_message` | autonomous (read) | `get_message` (full body + headers + attachment list) |
| `outlook_calendar` | autonomous (read) | `upcoming_events` |
| `outlook_reminders` | autonomous (read) | `list_reminders` |
| `outlook_resolve_recipient` | autonomous (read) | `resolve_recipient` (GAL/contacts → SMTP) |
| `outlook_draft` | confirm | `save_reply_draft` (incl. `reply_all`) / `save_new_draft` |
| `outlook_send` | confirm | `send_draft` / `send_new_mail` |
| `outlook_save_attachment` | confirm | `save_attachment` (writes a file to disk) |
| `outlook_create_event` | confirm | `create_event` (invite with attendees, else personal block) |
| `outlook_respond_invite` | confirm | `respond_invite` (accept / tentative / decline) |
| `outlook_mark_read` | confirm | `mark_message` (read/unread) |
| `outlook_categorize` | confirm | `categorize_message` (add or replace categories) |
| `outlook_move` | confirm | `move_message` (file into a folder) |

Reads are autonomous because they're side-effect-free local COM calls; the writes
are **confirm-gated** so the operator approves each one (an irreversible send, a
file written from email content, an invite/response that mails the organizer).
`from_mailbox` sets `SentOnBehalfOfName` (send-as / on-behalf, per the Exchange
permission).

`outlook_get_message` is the primitive the list/search rows promise: those rows
carry only a 280-char preview plus an `entry_id`, and this fetches the rest (full
body up to `_FULL_BODY_CAP`, cc, and the attachment list). `outlook_save_attachment`
then pulls a named/indexed attachment to disk (basename-sanitised so a crafted
filename can't escape the destination dir) — e.g. to feed a log file into the
`runspec-logops` runnables. `outlook_create_event` does **not** itself add a Teams
join link (the desktop Teams Meeting add-in is not scriptable over COM, and the
join link can only be minted by the Teams service); a link appears only if the org
enables online-meeting-by-default, or via a separate Graph `OnlineMeetings` scope.

`outlook_mark_read` / `outlook_categorize` / `outlook_move` are the "after
handling" half of the helpdesk loop — flag a message read, tag it, or file it once
it's been dealt with — and `outlook_draft` gained a `reply_all` mode (reply to the
sender *and* every other recipient).

The compact row shapers (`_mail_summary`, `_event_row`, `_search_dasl`,
`_full_message`, `_attachment_rows`, `_match_attachment_index`,
`_safe_attachment_name`, `_split_recipients`, `_build_event`, `_respond_code`,
`_merge_categories`) are pure and unit-tested with fakes on any OS; the COM calls
are Windows-only and guarded.

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
