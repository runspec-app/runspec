# Keyboard shortcuts

The console has app-wide keyboard shortcuts and a **command palette**. Press
**Ctrl+/** (or **?** when you aren't typing in a field) to see them all, or click
the **?** button in the top-right corner.

On macOS, **Cmd** works wherever **Ctrl** is listed.

## Command palette

Press **Ctrl+K** and start typing. One searchable list covers:

- **Actions**: approve or deny a waiting confirmation, stop the running turn,
  new chat, refresh hosts, Settings, file transfer, theme, docs.
- **Go to**: every tab.
- **Runnables**: opens the runnable's form in the Forms tab (the copy on the
  selected host when it has one).
- **Runbooks** and **saved runs**: opens their run dialog.
- **Hosts** and **host profiles**: selects the host or switches the profile.

Arrow keys move, **Enter** runs the highlighted item, **Esc** closes.

## Shortcuts

| Keys | Action |
|---|---|
| Ctrl+K | Command palette |
| Ctrl+/ or ? | Keyboard shortcuts |
| Ctrl+, | Open Settings |
| Alt+R | Refresh hosts |
| Ctrl+1 … Ctrl+9 | Switch to the Nth tab (in the order shown) |
| Ctrl+L | Focus the command bar |
| / | Focus the command bar and open the runnable menu |
| Alt+A | Approve the pending confirmation |
| Alt+D | Deny the pending confirmation |
| Alt+N | New chat |
| Ctrl+Tab / Ctrl+Shift+Tab | Next / previous chat session |
| Esc (in the command bar) | Stop the running turn |
| Ctrl+M (in the command bar) | Start / stop dictation |

Single keys (**?** and **/**) only work when focus isn't in a text field, so
they never interfere with typing. No shortcut fires while a dialog or drawer is
open.

With AI off ([`[console] ai = false`](console-ai-off.md)) the chat-only
shortcuts (New chat, Stop, dictation) are hidden. Approve / Deny stay, because
confirmations still come from the MCP server and triggers.

## Approving from the keyboard

**Alt+A** and **Alt+D** answer the oldest confirmation that is waiting, but never
one you can't see:

- From another tab, the first press brings up the Console with the card
  flashing. Press again to answer.
- If the card's buttons are scrolled out of view, the first press scrolls them
  into view and flashes the card. Press again to answer.

The key presses the card's own button, so edited arguments and the audit record
are exactly the same as a mouse click.

## Keys the console leaves alone

These keep their usual browser meaning and are never used for shortcuts:

- **Ctrl+R / F5**: reload the window. This is the way to recover if the UI ever
  gets into a bad state. A reload closes open chat sessions; saved conversations
  can be reopened from **History**.
- **Ctrl+F**: find text anywhere in the current view.
- **Ctrl+P**: print.
- Copy, paste, cut, select all, undo and redo.
