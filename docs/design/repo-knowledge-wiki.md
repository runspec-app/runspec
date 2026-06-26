# Repo-anchored knowledge ("the AI wiki") — design north star

**Status:** design / discussion. No code yet. Captures the direction so we can build
it in small, portable chunks rather than over-tuning per-codebase runnables.

## Why this, and why now the reframe

`runspec-logops` (`summarize-log` / `map-trace`) is useful, but refining it further
has **local, non-portable** returns: every heuristic is shaped by one codebase's log
format and stack dialect, and the next team's stack needs different ones. That's a
treadmill. We're declaring logops "good enough" (it does the portable 80% — bounded
digests, JSON-lines parsing, best-effort code-mapping) and **stopping per-codebase
refinement.**

The durable, portable layer isn't smarter parsing — it's **accumulated, shared,
freshness-aware knowledge anchored to a versioned checkout**: an "AI wiki" that lives
next to the code, that a `git pull` keeps current, and that lets the next agent see at
a glance *what's already known, what's stale, and what's new* — so it re-reads only
what it must. This is portable because it rides git, not any language.

## The load-bearing insight

**Don't build a "database of files changed since last read" — git already is one.**

A note stamped with the commit SHA it was written against is *fresh* iff its files
haven't changed since. `git diff <stamp>..HEAD -- <paths>` (and `git log --stat`) tells
you exactly what is stale and what is new. "What can I rely on / what must I re-verify
/ what's uncovered" becomes a git query, not a bespoke index. This collapses most of
what otherwise looks like a massive task.

## Where it sits in runspec-console

The console is the cockpit: humans + their agents, a fleet of hosts, and a central
checkout. It already has every primitive this needs — dulwich git (Config Sync), SSH
filtered-fetch, the memory store, git-synced runbooks. The wiki is the **missing middle
layer**:

| Layer | What it holds | State today |
|---|---|---|
| Memory | cross-run facts, keyed by an `app` tag | exists (local TOML) |
| Runbooks | reusable runbooks / procedures | exists (git-synced) |
| **Knowledge (this)** | **repo-anchored discoveries with provenance + freshness** — "what we learned about module X, validated at commit abc123" | **missing** |

Longer term, memory and runbooks fold into this anchored model (a discovery *is* a memory
with a code location and a freshness stamp).

## Storage: a local-only knowledge ref on a durable central checkout (leading option)

Keep knowledge **in git** (versioned, diffable, staleness-checkable against the same
object store) but on a dedicated ref that **never enters origin's branches or PRs**, so
the product repo stays pristine. Two concrete forms:

- **`git notes`** under a custom ref (`refs/notes/runspec-knowledge`): attaches metadata
  to the exact commit it describes; invisible to normal `log`/branches; local by
  default; optionally fetch/pushable to a side ref. Staleness is intrinsic. Caveat:
  per-commit model; multi-writer merges need a notes merge strategy.
- **An orphan branch** (e.g. `runspec-knowledge`) holding `.notes/*.md` with small
  frontmatter. More familiar; merges like code; never merged to main.

A note (whichever carrier) is just:

```yaml
paths: [src/orders/pricing.py]
commit: abc123          # the SHA this was validated against
confidence: high
summary: >
  Pricing applies the discount before tax; the null-discount NPE path is guarded
  in apply_rules(). Safe to skip re-reading unless pricing.py changes.
```

**Resolving "never pushed" vs "collective":** if the group shares **one durable central
checkout** (a persistent host, or the console box — *not* an ephemeral clone), then
"local to that checkout" *is* "shared by the group" — no origin pollution, no second
repo, console as the access point. For distributed teams, opt-in replicate the ref to a
side namespace on a remote (still never a branch/PR).

**Open questions (to discuss):**
- Durability: a never-pushed ref isn't backed up and dies with the clone → "central"
  must mean "durable"; side-ref replication is the backup story.
- `git notes` vs orphan branch — intrinsic per-commit anchoring vs familiar
  file-per-area + easy merges.
- Multi-writer concurrency when several agents (and humans) update knowledge at once.
- In-repo `.runspec/notes/` (simplest, but touches the product repo) remains a fallback
  for teams that *want* notes in their history.

## Chunked roadmap (smallest first)

1. **Staleness primitive** — `knowledge-status --repo`: read the notes, shell `git` to
   classify each **fresh / stale**, and list **changed-but-uncovered** files since the
   last sync. Output is the agent's "rely on / re-verify / look at" map. Reuses
   logops-core's repo-walking; adds a thin git wrapper. *This is the load-bearing chunk
   and is useful alone.*
2. **`note-save`** — write/update a note stamped at HEAD.
3. **Console "Knowledge" tab** — browse the wiki, see staleness, jump to code.
4. **Fold in memory/runbooks** — unify under the anchored model.
5. **Collective / multi-writer** — the shared central checkout + ref replication; notes
   merge across a team of humans+agents.

## The agent loop this enables

`git pull` → `knowledge-status` → trust the fresh notes (skip re-reading that code) →
investigate only stale/uncovered areas → `note-save` stamped at HEAD. The next agent
inherits all of it. Cheap, incremental, codebase-agnostic.

## Explicitly out of scope (for now)

- Further per-codebase tuning of `summarize-log` / `map-trace`.
- A semantic "is this note still *correct*" check beyond git change-detection — changed
  files are flagged for review; the agent decides. Best-effort, like the rest.
