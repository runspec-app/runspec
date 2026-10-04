# Knowledge wiki

The **Knowledge** tab is the console's team knowledge wiki: what you and the agent
have learned about your fleet, kept as short facts about named things, drawn as a
graph, and readable by the agent and by an MCP client such as Claude Code.

Where [agent memory](console-agent.md) holds a handful of machine-local notes, the
wiki is structured: every fact is *about* one or more entities, and the entities
link together through the facts they share.

## What a fact looks like

```text
kind   location
about  host:web-01, app:orders, file:/var/log/orders/app.log
text   orders logs are in /var/log/orders/app.log on web-01
```

Entities are written `type:name`. The types the tab colours are `host`, `group`,
`profile`, `runnable`, `file`, `term` (team jargon), `app`, `service`, `team` and
`topic`. A fact's **kind** is one of `location`, `glossary`, `pairing`, `host`,
`effect` (knock-on effects), `arg-habit`, `procedure` or `note`.

Each entity gets a page listing every fact about it, the entities it links to, and
who contributed. The same fact proposed twice, or by two people, is recognised and
counted as a confirmation instead of a duplicate.

Facts are refused when they look like they contain a password, key or token. The
wiki is meant to be shared.

## Where facts come from

| Source | How | Goes to |
|---|---|---|
| **Capture usage** | Mines run history over the range picked next to the button (7, 30 or 90 days, or all history; 30 by default): the arguments a runnable is usually run with, which hosts it runs on, and what usually runs after what. No AI. Also re-runs on its own, over the same range, when the tab opens and the last capture is over an hour old. | The wiki, marked *observed* |
| **The agent** | `propose_fact`, whenever it learns something reusable | The inbox |
| **Claude Code** (via the [console MCP server](console-mcp-server.md)) | the same `propose_fact` tool | The inbox |
| **Suggest from chats** | The utility model reads your saved chats over the same range and suggests facts. Long history is read in batches of about 24,000 characters, one model call each, up to 10 per click; the next click carries on. Chats already read are remembered, so a re-run only reads new chats and new turns. Hidden when AI is off. | The inbox |
| **Add fact** | You write one in the tab | The wiki |

Nothing proposed joins the wiki until you approve it in the **Inbox** tab, where
you can approve, edit and approve, or reject each fact.

## Agent and MCP tools

| Tool | Does | Autonomy |
|---|---|---|
| `search_wiki` | ranked search (kind and entity filters) | autonomous |
| `wiki_page` | one entity's page | autonomous |
| `wiki_graph` | an entity's neighbours, with weights | autonomous |
| `propose_fact` | adds a fact to the inbox | autonomous |
| `list_wiki_inbox` | pending facts with source and author | autonomous |
| `review_wiki_fact` | approve or reject (optionally editing) | confirm |
| `edit_wiki_fact` | correct an approved fact | confirm |
| `retire_wiki_fact` | retire a wrong or stale fact | confirm |

The agent is told to check `search_wiki` before asking you where something lives
or what a term means. None of the wiki is put in the system prompt; the agent
looks things up when it needs them.

With `[mcp_server] expose_builtins = true`, the same tools are published to an MCP
client. A confirm-gated call from Claude Code (review, edit, retire) waits for you
in the console's confirmation card, so a client can curate but cannot approve
facts into the wiki without you. Facts it proposes are marked with source `mcp`.
The tools don't need the console's own model, so they keep working with
`[console] ai = false`.

## Storage

Everything lives in `{app_dir}/knowledge/`:

| File | Holds |
|---|---|
| `facts.toml` | facts approved on this console (your contributions) |
| `inbox.toml` | proposed facts awaiting review |
| `observed.toml` | usage patterns from the last capture (rewritten each capture) |
| `team/*.toml` | facts pulled from the team (Memory Sync, coming next) |
| `index.sqlite` | search/graph index, rebuilt from the files whenever they change; safe to delete |
| `state.toml` | this console's bookkeeping: the capture range and which chats were already read. Never shared; delete it to re-read every chat |

The TOML files are the source of truth, written in a stable order so they diff
cleanly in git. **Memory Sync**, the follow-up to this feature, shares
`facts.toml` through a separate git repo with its own credential, so Config Sync
stays pull-only.
