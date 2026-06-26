# Git-repo agent tools (local checkouts)

## Why

The console agent could already act on the fleet (run runnables), the mailbox
(Outlook COM), and its own memory — but it had **no view of source code**, so
code questions got generic answers. These tools give it on-disk checkouts of
sanctioned repos so suggestions are grounded in the *actual* code, and let it make
**small local edits** the operator reviews.

It deliberately reuses the **Config Sync** git machinery (`gitsync.py`): dulwich
(pure-Python, no system `git`), the paramiko SSH vendor, and the HTTPS CA-bundle /
proxy pool manager. So auth, internal CAs, and the corporate proxy behave exactly
as they do for Config Sync — and a username/group-gated environment needs no new
plumbing. The bridge builds the `GitAuth` (its existing `_sync_auth`, from a saved
Credential), and `repos.py` owns the git + filesystem mechanics.

## What

A **registry** + **seven tools**, offered only when the git engine (dulwich) is
available **and** at least one repo is registered.

### Registry — `runspec_repos.toml`
The controlled list of repos the agent may touch (it can never act on an arbitrary
URL). Array-of-tables, mirrors `runspec_groups.toml`; rides **Config Sync** (in
`gitsync.KNOWN_FILES` + `configmerge.COLLECTION_FILES`, merged by `name`):

```toml
[[repo]]
name          = "payments"
url           = "https://git.corp/team/payments.git"   # or ssh://…
ref           = "main"             # branch or tag to check out (default "main")
credential_id = "git-token"        # optional — a saved Credential for auth
description   = "Payments service"
```

Checkouts live at `{app_dir}/repos/{name}` (local working copies — **never**
synced; only the registry that lists them is).

### Tools
| Tool | Autonomy | Backed by (`repos.py`) |
|---|---|---|
| `repo_status` | autonomous (read) | `read_repos` + `is_checked_out` |
| `repo_list` | autonomous (read) | `list_dir` |
| `repo_grep` | autonomous (read) | `grep_repo` (regex, skips `.git`/binaries, bounded) |
| `repo_read` | autonomous (read) | `read_file` (optional line range, byte-capped) |
| `repo_diff` | autonomous (read) | `diff_repo` (working tree vs synced commit) |
| `repo_sync` | confirm | `sync_repo` (clone or fetch + re-checkout) |
| `repo_write` | confirm | `write_file` (local file write) |

Reads are autonomous (read-only over a local checkout). `repo_sync` (network +
disk, and it **discards** uncommitted local edits) and `repo_write` (writes a
file) are confirm-gated. The tools are also selectable in the per-trigger action
allowlist, and `_console_context` names the registered repos each turn.

## Deliberately out of scope: commit / push
**Edits are local only.** There is no commit or push tool. The agent reads code,
proposes edits into the working copy, and the operator reviews them with
`repo_diff` and commits/pushes themselves with their own tooling. This keeps the
console a *control plane*, not a code-publishing path: the blast radius of a wrong
edit is a local file the operator can throw away (a re-`repo_sync` restores it from
the remote). For full repo-grounded *authoring*, delegate to a purpose-built coding
agent (e.g. `runspec-claude`/`claude-run` or the Claude Agent SDK pointed at the
same proxy) rather than growing a code editor inside the console.

## Safety
- **Registry-bounded:** the agent can only name a repo present in
  `runspec_repos.toml` — not clone an arbitrary URL.
- **Path-fenced:** every file path is resolved through `_safe_path`, which rejects
  absolute paths and any `..` escape, so neither the model nor a repo file can read
  or write outside the checkout.
- **Bounded reads:** `repo_read` caps bytes, `repo_grep` caps file size + result
  count, `repo_diff` caps files + lines — a huge repo can't blow the model context.
- **Binary-aware:** binary files are skipped by grep and declined by read; diffs of
  binaries report "differs" rather than dumping bytes.

The pure helpers (`normalise_repos`, `_safe_path`, `_is_binary`, `_unified_diff`)
are unit-tested on any OS; the clone/read/edit/diff roundtrip is tested against a
real local dulwich repo (no network).

## Follow-ups
- A **Settings → Repos** tab (bridge `get_repos`/`save_repos`) so the registry is
  managed in the UI rather than by editing the TOML / Config Sync. (v1 is
  registry-file + Config Sync only — no new public bridge API surface.)
- Optional **delegate-to-Claude-Code** edit path via `runspec-claude` for repos
  where the operator wants full authoring rather than small reviewed edits.
