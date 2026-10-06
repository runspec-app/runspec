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
`profile`, `runnable`, `file`, `term` (team jargon), `app`, `service`, `team`,
`trigger`, `schedule`, `runbook` and `topic`. A fact's **kind** is one of `location`, `glossary`, `pairing`, `host`,
`effect` (knock-on effects), `arg-habit`, `procedure` or `note`.

Each entity gets a page listing every fact about it, the entities it links to, and
who contributed. The same fact proposed twice, or by two people, is recognised and
counted as a confirmation instead of a duplicate.

Facts are refused when they look like they contain a password, key or token. The
wiki is meant to be shared.

## Where facts come from

| Source | How | Goes to |
|---|---|---|
| **Capture** | Mines run history over the range picked next to the button (7, 30 or 90 days, or all history; 30 by default): the arguments a runnable is usually run with (never a username, email or run-as account, or a value that is your own account, since those differ per person, and never an argument left at the runnable's own default) and what usually runs after what. Also reads your config: what each trigger and schedule runs and where, what each runbook is for and which runnables it uses, and the saved file filters. Which hosts a runnable runs on, and which hosts are in each group and profile, are left out: History, the sidebar and the agent's `search_history` / `console_status` already show them. No AI. Also re-runs on its own, in the background, over the same range, when the tab opens and the last capture is over an hour old; the tab opens straight away and refreshes when it lands. | The wiki, marked *observed* |
| **Built in** | Ships with the console: what runspec is, the `runspec.toml` format, how to write a runnable (the `add-runnable` skill's steps) or install one from the Marketplace, what each console feature does, and every built-in agent tool. | The wiki, marked source *seed* |
| **The agent** | `propose_fact`, whenever it learns something reusable | The inbox |
| **Claude Code** (via the [console MCP server](console-mcp-server.md)) | the same `propose_fact` tool | The inbox |
| **Suggest from chats** | The utility model reads your saved chats over the same range and suggests facts. It sees your messages, the agent's replies, each tool call and the start of what it returned (300 characters, with anything that looks like a secret redacted), so it can spot one tool's output feeding the next. Long history is read in batches of about 24,000 characters, one model call each, up to 10 per click; the next click carries on. Chats already read are remembered, so a re-run only reads new chats and new turns. Hidden when AI is off. | The inbox |
| **Add fact** | You write one in the tab | The wiki |

Facts are linked to the fleet's real entities. When a suggestion, or a fact the
agent proposes, names one of your hosts, groups, profiles, runnables, triggers,
schedules or runbooks, it is attached to that existing node rather than a
lookalike. `term:Linux Servers` becomes `group:Linux Servers`, and the hosts
the text names are linked too. Capture links earlier facts the same way, so a
fact written before a group existed joins it once it does. Very common words
such as `local` are only linked when a fact names them as a subject, not when
they appear in a sentence.

Nothing proposed joins the wiki until you approve it in the **Inbox** tab, where
you can approve, edit and approve, or reject each fact.

A runnable with past runs that isn't installed on any connected host now is
drawn faded and marked *not installed*; tick **Hide not installed** to take
those off the graph.
The graph has zoom in, zoom out and fit-to-view buttons in its corner, and the
scroll wheel zooms about the pointer. Click empty space on the graph (or the
clear button under the zoom buttons) to clear the selection.

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
| `retire_wiki_fact` | retire a wrong or stale fact (also drops its pin) | confirm |
| `pin_wiki_fact` | pin a fact into every chat's context, or unpin it | autonomous |

The default system prompt tells the agent to check `search_wiki` before asking
you where something lives or what a term means, and to `propose_fact` what it
learns. The only part of the wiki in the prompt is your **pinned** facts; the
agent looks everything else up when it needs it. A custom `[llm] system`
replaces that line, so add it to yours (or use `system_append`) if you want the
same behaviour.

Because the built-in facts cover runspec and the console, "how do I add a
runnable?" or "what can the Schedules tab do?" is answered from the wiki, by the
console's own agent or by Claude Code through the MCP server (with
`expose_builtins` on).

## Pinned facts

Pin up to six facts that matter on almost every turn (the pushpin on a fact, or
the agent's `pin_wiki_fact`); they are folded into every chat's system prompt
under *Pinned facts (team wiki)*. With nothing pinned the prompt is unchanged.
The side panel lists the pins when no entity is selected, and the context gauge
counts them. Pins belong to this console: they live in `state.toml`, Memory Sync
never shares them, and you can pin a fact pulled from the team. A pin follows a
fact through an edit or a review, and goes away when the fact is retired or
rejected.

## Facts about a runnable

Don't spend a pin on advice for one tool. A fact about `runnable:<name>` is
shown to the agent whenever it searches for that runnable (up to three facts on
its `search_runnables` row) or describes it (up to eight in `describe_runnable`),
approved facts first, then observed ones. The agent gets the advice at the
moment it picks the tool, and facts about other runnables stay out of its
context.

Advice that holds wherever the runnable is used, such as which args keep
Confluence macro formatting or which verb is the right entry point, belongs in
the arg and runnable `description` fields of its `runspec.toml` instead. That
text is part of the tool's schema, so every agent sees it, including Claude Code
through the MCP server. Use a wiki fact for what is specific to your team.

## Moving over from the old agent memory

The wiki replaced the agent's older memory (`remember`, `search_memory`,
`list_memories`, `forget_memory` over `runspec_memory.toml`) in console 0.332.0.
The first time the wiki is opened after the upgrade, each remembered fact moves
into the **inbox** as a pending `note` from source `memory`: about `app:<app>`
(unless it was the default `general`), its first three tags as `topic:` entities,
and any host, group or runnable the text names. Pinned memories stay pinned (the
six most recalled), so the prompt carries the same facts as before. They move to
the inbox rather than straight into the wiki because the old memory was never
shared and approved facts are what Memory Sync pushes: you decide which become
team knowledge. Until you approve one, `search_wiki` doesn't return it. A fact
that looks like it contains a secret is not moved. The old file is renamed
`runspec_memory.toml.migrated` and kept as a backup.

| Old tool | Now |
|---|---|
| `remember` | `propose_fact` (then approve it in the inbox) |
| `remember` with `pin` | `propose_fact`, then `pin_wiki_fact` |
| `search_memory`, `list_memories` | `search_wiki`, `wiki_page` |
| `forget_memory` | `retire_wiki_fact` |

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
| `observed.toml` | usage patterns and config facts from the last capture (rewritten each capture) |
| `team/*.toml` | facts pulled from the team by Memory Sync, one file per contributor (replaced on each pull) |
| `runspec_console/knowledge_seed.toml` (in the package) | the built-in facts; read-only. Retiring or editing one writes your own copy that overrides it, and Memory Sync never pushes them |
| `index.sqlite` | search/graph index, rebuilt from the files whenever they change; safe to delete |
| `state.toml` | this console's bookkeeping: the capture range, which chats were already read, the pinned facts, and the last Memory Sync push and pull. Never shared; deleting it re-reads every chat and clears the pins |

The TOML files are the source of truth, written in a stable order so they diff
cleanly in git.

## Sharing with your team (Memory Sync)

Memory Sync shares the wiki through a git repo of its own. It is a sibling of
Config Sync with a separate repo and credential, so a wiki token never reaches
your config, and Config Sync stays pull-only.

**Set up** in Settings → Memory Sync:

| Field | Meaning |
|---|---|
| Wiki repository | HTTPS or SSH URL of an empty repo kept for the wiki |
| Credential | a Credentials entry that can push to it (a token, or an ssh-key credential over SSH) |
| Team branch | what a pull reads; default `main` |
| Your name in the wiki | your *lane*; blank uses your login name. Kept on this machine across a Config Sync pull |
| Commit email | the author email on your pushes; use the one on your git account. Blank uses `<lane>@<hostname>`. Kept on this machine |
| CA bundle, Verify TLS | as for Config Sync, for an internal CA or a TLS-inspecting proxy |

**Test connection** checks the repo is reachable and whether the team branch
and your branch exist yet. Save, Test, Push and Pull each leave their result on
screen (under the buttons in Settings, beside them on the Knowledge tab) until
the next action, so a credential or server error is visible.

If GitLab refuses a push with `Author '…' is not a member of team` (or another
author or committer check), the repo has a push rule that only accepts commits
from known accounts. Set **Commit email** to the email on your GitLab account
and push again. The settings are stored as `[memory_sync]` in
`config.toml`; the secret stays in the keychain.

**Push** (Knowledge tab) shares your approved facts and nothing else:

- It writes one file, `facts/<lane>.toml`, on your own branch `contrib/<lane>`.
  Nothing else in the repo changes and it never force-pushes. Your branch starts
  from the team branch the first time.
- Only *approved* facts go: never the inbox, observed usage, retired facts, or
  `evidence` ids (they name local runs). A fact whose text now looks like a
  secret is held back.
- A dot on **Push** means you have approved facts that aren't pushed yet.

Facts reach the team when someone merges `contrib/<lane>` into the team branch
on the repo, so every fact is reviewed twice: in your inbox, then in the merge.
Each contributor only ever changes their own file, so merging any number of
`contrib/*` branches never conflicts. Protect the team branch on the repo so
merges go through review.

**Pull** reads every `facts/*.toml` on the team branch except your own (those
facts are already here) into `team/`, replacing what was there. A file that
isn't valid TOML aborts the pull with nothing written; a file whose name isn't a
valid lane is skipped. Team facts show in the graph, pages and search like your
own, and confirmations from the team add to your count.

Layout of the wiki repo:

```text
facts/
  alice.toml     # merged from contrib/alice
  jason.toml     # merged from contrib/jason
```
