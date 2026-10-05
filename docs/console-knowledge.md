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
| **Capture** | Mines run history over the range picked next to the button (7, 30 or 90 days, or all history; 30 by default): the arguments a runnable is usually run with, which hosts it runs on, and what usually runs after what. Also reads your config: which hosts are in each group and profile, what each trigger and schedule runs and where, what each runbook is for and which runnables it uses, and the saved file filters. No AI. Also re-runs on its own, over the same range, when the tab opens and the last capture is over an hour old. | The wiki, marked *observed* |
| **The agent** | `propose_fact`, whenever it learns something reusable | The inbox |
| **Claude Code** (via the [console MCP server](console-mcp-server.md)) | the same `propose_fact` tool | The inbox |
| **Suggest from chats** | The utility model reads your saved chats over the same range and suggests facts. Long history is read in batches of about 24,000 characters, one model call each, up to 10 per click; the next click carries on. Chats already read are remembered, so a re-run only reads new chats and new turns. Hidden when AI is off. | The inbox |
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

On an entity's page, usage from Capture shows as one table (**Runs here** on a
host, **Used on** on a runnable) instead of one card per fact. A runnable with
past runs that isn't installed on any connected host now is drawn faded and
marked *not installed*; tick **Hide not installed** to take those off the graph.
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
| `retire_wiki_fact` | retire a wrong or stale fact | confirm |

The default system prompt tells the agent to check `search_wiki` before asking
you where something lives or what a term means, and to `propose_fact` what it
learns. None of the wiki itself is put in the prompt; the agent looks things up
when it needs them. A custom `[llm] system` replaces that line, so add it to
yours (or use `system_append`) if you want the same behaviour.

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
| `index.sqlite` | search/graph index, rebuilt from the files whenever they change; safe to delete |
| `state.toml` | this console's bookkeeping: the capture range, which chats were already read, and the last Memory Sync push and pull. Never shared; delete it to re-read every chat |

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
