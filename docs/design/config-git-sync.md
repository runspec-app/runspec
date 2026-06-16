# Config Sync — pull console config from a git repo, by tag

> **Status: shipped** — `runspec-console` 0.73.0. A new **Settings → Config Sync**
> tab that pulls the console's config files from an external git repository at a
> tag, authenticating with a saved credential. Pairs with the Credentials feature
> (Settings → Credentials) for auth and the existing config I/O in `config.py`.
>
> **Scope:** versioned, repeatable provisioning of a console install from one
> source of truth — a fresh machine enters a repo URL + tag + credential, clicks
> **Pull**, and is fully configured.

---

## 1. The problem

A fresh `runspec-console` install starts empty. The operator hand-builds
`config.toml`, `runspec_hosts.toml`, `runspec_triggers.toml` (and the filters /
credentials metadata files) under `%APPDATA%/runspec-console/`. Across a fleet,
every new machine repeats that work and the configs drift apart over time.

Config Sync makes a git repository the source of truth: the console pulls its
config files from a repo **at a specific tag**, so a rollout is versioned (pin a
tag, roll forward by pulling a newer one, roll back by pulling an older one).
Authentication reuses the credentials the operator already manages in the
Credentials tab — no second secret store.

## 2. Decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Auth | A saved **credential** (Credentials tab). HTTPS uses its username + keyring token; SSH repos use the console's SSH key (`[ssh] identityFile`). |
| 2 | Git engine | **dulwich** (pure-Python), behind a swappable boundary in `gitsync.py`. No system `git` needed on fresh/Windows installs. Optional `git-sync` extra. |
| 3 | Transports | HTTPS (token passed in-process — never on argv/log) **and** SSH (over the already-bundled paramiko via a small vendor, so no system `ssh` either). |
| 4 | Versioning | Pull by **ref** — a tag (recommended) or a branch. The UI offers a tag dropdown (`ls-remote`) with a free-text fallback. |
| 5 | Overwrite | **Replace, with backup.** Each existing file is copied to `<name>.bak-<ts>` before being replaced. |
| 6 | Validation | Every fetched `.toml` is parsed **before** anything is written. A single broken file aborts the whole pull — never clobber good config with a broken pull. |
| 7 | Credentials file | **Merged by id**, not replaced — so the credential used to authenticate the pull (and any other local-only entry) survives. Secrets are never in the file; an id with a local keyring secret keeps it. |
| 8 | Scope | `config.toml`, `runspec_hosts.toml`, `runspec_triggers.toml`, `runspec_filters.toml`, `runspec_credentials.toml`. The list is **data-driven** (`gitsync.KNOWN_FILES` + the configured `files`), so new files sync with no code change. Bare filenames only (no path traversal out of the app dir). |
| 9 | No restart | After a successful pull the running app hot-reloads hosts / filters / credentials / SSH / triggers. |

### Why dulwich (engine choice)

A fresh desktop/Windows install frequently has **no `git` binary** — requiring
one undercuts the whole "install pulls and is configured" goal. dulwich is
pure-Python, ships as an optional extra, and is always present when the feature
is. It does HTTPS with in-process username/password (the token never touches a
command line or log) and SSH via a paramiko-backed vendor (paramiko is already a
core dependency), so both transports work with no external tooling. The
`gitsync.py` boundary keeps a future system-`git` backend a drop-in if a
corporate-proxy edge case ever needs `~/.gitconfig` (dulwich honors
`HTTPS_PROXY`/`HTTP_PROXY` but not gitconfig).

## 3. Flow

```
Settings → Config Sync                 bridge.py                    gitsync.py (dulwich)
─────────────────────                  ─────────                    ────────────────────
URL + tag + credential  ── save ──▶  save_sync_config  ──▶  [config_sync] in config.toml
"refresh tags"          ── list ──▶  list_sync_tags    ──▶  list_tags()  (ls-remote)
"Pull now" (confirm)    ── pull ──▶  sync_pull:
                                       1. resolve credential → GitAuth
                                       2.                         fetch_files(url, ref, files, auth)
                                                                    fetch ref → peel to commit
                                                                    read each file's blob from the tree
                                       3. validate every .toml (abort on parse error)
                                       4. per file: back up → write   (credentials.toml: merge by id)
                                       5. re-inject local [config_sync] + stamp last_*
                                       6. reload hosts/filters/creds/ssh + restart triggers
                                     ◀── { ok, ref, commit, written, skipped, backups }
```

No working tree is checked out: `fetch_files` fetches the ref, peels an annotated
tag down to its commit, and reads the wanted files straight out of the commit's
tree.

## 4. Config shape — `[config_sync]` in `config.toml`

```toml
[config_sync]
url           = "https://github.com/acme/runspec-config.git"
ref           = "v1.4.0"          # tag (preferred) or branch
credential_id = "config-repo"     # a Credentials-tab entry
subpath       = ""                # optional dir within the repo
files         = ["config.toml", "runspec_hosts.toml", "runspec_triggers.toml",
                 "runspec_filters.toml", "runspec_credentials.toml"]
last_ref      = "v1.4.0"          # stamped by a successful pull
last_commit   = "a1b2c3d…"
last_synced   = "2026-06-16T04:30:00Z"
```

**Chicken-and-egg / preserve-across-pull.** A pull overwrites `config.toml`,
which would wipe these very settings. The pull writes the repo's `config.toml`,
then re-injects the **local** `[config_sync]` via `config.write_sync()` (local
url/ref/credential_id/subpath/files win over anything in the repo's file) and
stamps `last_*`. So the operator's repo pointer always survives a pull, and a
fresh install's typed settings persist through the first pull. The secret is never
in this section — it lives in the keyring under `credential_id`.

## 5. Reused vs. new

**Reused:** `config.py` atomic write + `_CONFIG_LOCK` (the new `read_sync` /
`write_sync` mirror `read_mailbox` / `write_mailbox`); `credentials.get_secret` +
`Bridge._credential_entry` for auth; `Bridge._reload_hosts` / `_reload_filters` /
`_reload_credentials` / `_after_config_change` / `restart_triggers` for the
hot-reload; `credentials.save_credentials` for the merge; paramiko (already a
core dep) for SSH.

**New:** `runspec_console/gitsync.py` (the dulwich layer + paramiko SSH vendor);
`config.read_sync` / `write_sync`; bridge methods `get_sync_config` /
`save_sync_config` / `list_sync_tags` / `sync_pull` (+ helpers `_sync_auth`,
`_atomic_write_bytes`, `_merge_synced_credentials`); the `ConfigSyncTab` in
`SettingsDrawer.tsx`; the `git-sync` extra.

## 6. Safety

- The token rides in-process to dulwich's HTTP client — never on a command line
  or in a log. SSH uses paramiko with the key file.
- **Validate-before-replace** + **backup-before-write** make a failed pull
  non-destructive.
- **Credentials merge-by-id** means a pull can never lock the operator out of the
  repo by deleting the auth credential.
- A **filename allowlist** (bare names only) blocks writing outside the app dir.

## 7. Slices

| # | Slice | Status |
|---|-------|--------|
| C1 | `gitsync.py` — dulwich fetch/list + paramiko SSH vendor | ✅ 0.73.0 |
| C2 | `config.read_sync` / `write_sync` (`[config_sync]`) | ✅ 0.73.0 |
| C3 | Bridge `get/save_sync_config`, `list_sync_tags`, `sync_pull` + hot-reload | ✅ 0.73.0 |
| C4 | `ConfigSyncTab` (Settings) + bridge interface | ✅ 0.73.0 |
| C5 | Tests (`test_gitsync.py`, `test_sync_pull.py`) | ✅ 0.73.0 |

## 8. Testing

- `tests/test_gitsync.py` builds a throwaway local repo with two tags and checks
  tag listing, per-tag file bytes, missing-file skip, and unknown-ref error
  (skipped when dulwich or git is absent).
- `tests/test_sync_pull.py` mocks `gitsync.fetch_files` and checks the
  orchestration: `[config_sync]` preserved + `last_*` stamped across a
  `config.toml` overwrite; invalid TOML aborts the whole pull; backups created;
  credentials merged by id (local-only survives); hot-reload + trigger restart
  fired; missing URL errors.

Manual end-to-end: `git init` a temp repo with the config TOMLs, `git tag v1`,
set `[config_sync]` to its `file://`/path URL + `v1`, and run `sync_pull()` (or
the UI **Pull now**) — files land in the app dir, backups appear, hosts/triggers
reload live.
