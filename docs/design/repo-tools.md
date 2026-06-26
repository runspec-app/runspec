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

## Registry UI (Settings → Repos)

The registry is managed from a **Settings → Repos** tab (`SettingsDrawer.tsx`
`ReposTab`), so operators don't hand-edit `runspec_repos.toml`. It's framed as the
agent's allowlist ("repositories the agent may read and locally edit"): add / edit /
delete entries (name, URL, branch/tag, a credential picked from the Credentials tab,
description), each row showing **checked-out / synced-commit** status with a **Sync
now** button. That button calls the bridge `sync_repo(name)` — an operator-initiated
clone/fetch, so the click *is* the authorization and it runs without the autonomy
gate the agent's `repo_sync` tool carries. Backed by `get_repos` / `save_repos` /
`sync_repo` (`bridge_fleet.py`); the registry still rides Config Sync, so the UI just
edits the local layer. Deleting an entry revokes the agent's access but leaves the
on-disk checkout in place.

## Follow-ups
- Optional **delegate-to-Claude-Code** edit path via `runspec-claude` for repos
  where the operator wants full authoring rather than small reviewed edits.
