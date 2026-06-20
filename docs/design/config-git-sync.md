# Config Sync — pull console config from a git repo, by tag

> **Status: shipped** — `runspec-console` 0.73.0; **non-destructive merge +
> `preserve` in 0.74.0** (§5); **TLS CA bundle / proxy / verify-toggle in 0.75.0**
> (§4). A new **Settings → Config Sync** tab that pulls the
> console's config files from an external git repository at a tag, authenticating
> with a saved credential. Pairs with the Credentials feature (Settings →
> Credentials) for auth and the existing config I/O in `config.py`.
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
| 1 | Auth | A saved **credential** (Credentials tab). HTTPS uses its username + keyring token; SSH repos use an **`ssh-key` credential's deploy key** (`private_key` path + optional keyring passphrase) — one identity per repo — falling back to the console's SSH key (`[ssh] identityFile`) when no key-bearing credential is selected. See §SSH-key credentials. |
| 2 | Git engine | **dulwich** (pure-Python), behind a swappable boundary in `gitsync.py`. No system `git` needed on fresh/Windows installs. Optional `git-sync` extra. |
| 3 | Transports | HTTPS (token passed in-process — never on argv/log) **and** SSH (over the already-bundled paramiko via a small vendor, so no system `ssh` either). |
| 4 | Versioning | Pull by **ref** — a tag (recommended) or a branch. The UI offers a tag dropdown (`ls-remote`) with a free-text fallback. |
| 5 | Merge (0.74.0) | **Non-destructive merge, with backup** (was replace). Collections merge by identity; mappings deep-overlay; a `preserve` list keeps machine-local keys local. Each existing file is still backed up to `<name>.bak-<ts>` first. See §6. |
| 6 | Validation | Every fetched `.toml` is parsed **before** anything is written. A single broken file aborts the whole pull — never clobber good config with a broken pull. |
| 7 | Credentials file | **Merged by id** (a special case of the collection merge) — so the credential used to authenticate the pull (and any other local-only entry) survives. Secrets are never in the file; an id with a local keyring secret keeps it. |
| 8 | Scope | `config.toml`, `runspec_hosts.toml`, `runspec_triggers.toml`, `runspec_filters.toml`, `runspec_credentials.toml`. The list is **data-driven** (`gitsync.KNOWN_FILES` + the configured `files`), so new files sync with no code change. Bare filenames only (no path traversal out of the app dir). |
| 9 | No restart | After a successful pull the running app hot-reloads hosts / filters / credentials / SSH / triggers. |

### SSH-key credentials (0.98.0)

SSH repos historically reused the one console-wide SSH identity
(`[ssh] identityFile`), so there was no way to give the config repo its **own**
deploy key. The Credentials tab now has a `kind = "ssh-key"` credential that
references a key pair **by file path** — `private_key` (passed to paramiko as the
identity file) and an optional `public_key` (informational). The key material
never leaves disk; only an optional **key passphrase** is stored in the OS
keychain under the credential id (the same channel as any other secret).

`_sync_auth` resolves an SSH URL like this:

1. If the selected credential carries a `private_key`, use it (and, for an
   `ssh-key` credential, its keyring passphrase).
2. Otherwise fall back to `[ssh] identityFile`.
3. The username defaults to `git` (override per credential for non-GitHub hosts).

`GitAuth` gained a `passphrase` field. dulwich's `SSHVendor.run_command`
signature has no passphrase parameter, so the passphrase is **bound into the
vendor factory** (`_install_paramiko_vendor(passphrase)`) rather than threaded
through the call; paramiko then decrypts `key_filename` with it. Key paths are
`~`-expanded before reaching paramiko. Multiple `ssh-key` credentials can coexist
— one deploy key per repo — which the single `identityFile` couldn't express.

### Shared base config — ordered layers (0.99.0)

TOML has no native include/inheritance, so a shared base is built on the merge
engine. `[config_sync] layers` is an ordered list of repo subdirectories overlaid
**low→high** — e.g. a shared `_base`, then `regions/emea`, then `teams/emea-pay`
on top — so teams share common hosts/filters/skills/defaults and overlay their own
flavour instead of each folder duplicating the base. With `layers` empty, the
single `subpath` is used (unchanged behaviour).

A pull fetches the ref **once** (`gitsync.fetch_layers` reads every layer out of
the same in-memory commit tree — no extra round-trips) and, per file, folds the
layers into one effective table (`configmerge.fold_layers`): mapping files
(`config.toml`, the triggers tables) `deep_overlay` in order; array-of-tables
files (hosts/filters/credentials/skills) `merge_collection` by identity so a
base layer's entries survive a later layer that only adds/updates a few. That
effective table then goes through the **same** non-destructive merge onto local —
so the `preserve` list and the operator's local-only entries still win over the
entire stack. Validation aborts the whole pull if **any** layer's TOML is broken.

Net precedence: **local (bottom) ← _base ← region ← team (top)**, with `preserve`
pinned on top of everything.

### Runbook docs — a deterministic wiki (0.99.0)

Skills are written for the agent; `runspec_console/docsgen.py` renders them as a
human-followable **markdown wiki** (`render_wiki(skills, hosts) -> {path: md}`)
so during an outage — when the console may be down — a person can SSH in and run
the same runnables the agent would. `bridge.generate_docs(dest_dir)` writes a
`README.md` (index grouped by tag + a host list + an SSH preamble),
`skills/<id>.md` per skill (when-to-use, steps, a params table from
`[skill.args]`, and cross-links to the `host__runnable` tokens and host names its
steps mention), and `hosts.md`.

Output is **deterministic** — everything sorted, no timestamps — so the same
skills produce byte-identical files; re-running (or a daily refresh) only changes
what the skills changed, which keeps the commit-back diff clean. The output dir is
`[docs] dir` (default `app_dir()/docs`); point it at a local clone of the config
repo's team folder and commit the docs back so the team shares them. Config Sync
is **pull-only with no working tree**, so the console never pushes — the operator
commits from their own clone. (The wiki is distributed via the git repo itself,
not synced into the app dir.)

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
subpath       = ""                # optional dir within the repo (single layer)
layers        = ["_base", "teams/emea"]  # ordered overlay dirs (low→high)
ca_bundle     = ""                # PEM for an internal CA / TLS proxy (HTTPS)
files         = ["config.toml", "runspec_hosts.toml", "runspec_triggers.toml",
                 "runspec_filters.toml", "runspec_credentials.toml"]
preserve      = ["config.toml:mailbox.folder", "runspec_hosts.toml:*.room_user"]
last_ref      = "v1.4.0"          # stamped by a successful pull
last_commit   = "a1b2c3d…"
last_synced   = "2026-06-16T04:30:00Z"
```

**Chicken-and-egg / preserve-across-pull.** A pull rewrites `config.toml`, which
would wipe these very settings. After merging `config.toml` the pull re-injects
the **local** `[config_sync]` via `config.write_sync()` (local
url/ref/credential_id/subpath/files/preserve win over anything in the repo's
file) and stamps `last_*`. So the operator's repo pointer always survives a pull,
and a fresh install's typed settings persist through the first pull. The secret is
never in this section — it lives in the keyring under `credential_id`.

**TLS (private hosts / corporate proxies, 0.75.0).** dulwich verifies HTTPS
against certifi — **not** the OS cert store or `git config http.sslCAInfo` — so a
private GitLab with an internal CA, or a TLS-inspecting proxy that re-signs HTTPS,
fails with `SSL CERTIFICATE_VERIFY_FAILED`. `ca_bundle` points verification at a
PEM file (with `SSL_CERT_FILE` / `GIT_SSL_CAINFO` / `REQUESTS_CA_BUNDLE` as env
fallbacks); the pull also routes through the console's `[ssh] proxy` (or
`https_proxy`). Verification stays on — `gitsync._https_pool_manager` builds a
urllib3 `PoolManager`/`ProxyManager` with `ca_certs` set and passes it to dulwich
via `pool_manager=` (a custom manager bypasses dulwich's own CA/proxy handling, so
it carries both). Returns `None` when nothing is configured, letting dulwich use
its certifi default. A `tls_verify = false` escape hatch (Settings → "Verify TLS"
off) builds the manager with `cert_reqs="CERT_NONE"` — insecure, for a trusted
network where no CA bundle is available (mirrors git's `http.sslVerify false`);
prefer the CA bundle.

**dulwich ignores `~/.gitconfig`.** None of the operator's system-git settings
(`http.proxy`, `http.sslVerify`, `http.sslBackend = schannel`, `credential.helper`)
apply to the pull — dulwich reads none of them, and it uses certifi, not the
Windows cert store that `schannel` would. That's why these are surfaced as Config
Sync settings (CA bundle, verify toggle, `[ssh] proxy` reuse) and a Credentials-tab
entry rather than inherited from git.

## 5. Merge semantics (non-destructive, 0.74.0)

A pull **tops up** local config; it never deletes the operator's own entries. The
strategy is inferred per file by its known structure (in the pure `configmerge`
module), with a `preserve` list that pins specific keys to the local value.

**Collection files** — merge by each entry's natural identity:

| File | Identity key |
|---|---|
| `runspec_hosts.toml` | host `name` |
| `runspec_triggers.toml` | trigger name (`[triggers.<name>]`) |
| `runspec_filters.toml` | filter `name` |
| `runspec_credentials.toml` | credential `id` |

Repo entries **add** (new key) or **update** (matching key); entries whose key
isn't in the repo are **kept**. On a matched entry, a per-entry `preserve` field
restores the local value.

**Mapping files** — `config.toml` deep-overlays: repo values win on conflicts,
**local-only keys survive** (e.g. an `[llm] api_key` the repo doesn't carry), and
`preserve` paths keep the local value.

**The triggers `[rota]` is never synced.** Working hours are per-person, so a pull
keeps the local `[rota]` untouched and ignores any `[rota]` in the repo file —
operators manage their own in Settings → Triggers. (Only the `[triggers.<name>]`
tables merge.)

**Preserve paths** — `"<file>:<dotted.path>"`. For a mapping file the path is the
nested key (`config.toml:mailbox.folder`); for a collection file the first segment
is the entry's key — or `*` for every entry — and the rest is the field
(`runspec_hosts.toml:*.room_user`). The user's list is layered on top of
`configmerge.DEFAULT_PRESERVE` (`mailbox.folder`, every host's `room_user`), so the
common machine-local keys are protected with no configuration. A preserved key the
repo tries to introduce but that's absent locally is dropped (the repo can't set
it). (The triggers `[rota]` needs no preserve entry — it's never synced at all.)

## 6. Reused vs. new

**Reused:** `config.py` atomic write + `_CONFIG_LOCK` (the new `read_sync` /
`write_sync` mirror `read_mailbox` / `write_mailbox`); `credentials.get_secret` +
`Bridge._credential_entry` for auth; `Bridge._reload_hosts` / `_reload_filters` /
`_reload_credentials` / `_after_config_change` / `restart_triggers` for the
hot-reload; `load_hosts`/`save_hosts`, `load_filters`/`save_filters`,
`load_credentials`/`save_credentials`, `read_triggers`/`read_rota` for the merge;
paramiko (already a core dep) for SSH.

**New:** `runspec_console/gitsync.py` (the dulwich layer + paramiko SSH vendor);
`runspec_console/configmerge.py` (deep overlay, merge-by-key, preserve paths);
`config.read_sync` / `write_sync` / `write_triggers_file`; bridge methods
`get_sync_config` / `save_sync_config` / `list_sync_tags` / `sync_pull` (+ helpers
`_sync_auth`, `_atomic_write_bytes`, `_merge_synced_file`, `_merge_synced_triggers`);
the `ConfigSyncTab` in `SettingsDrawer.tsx`; the `git-sync` extra.

## 7. Safety

- The token rides in-process to dulwich's HTTP client — never on a command line
  or in a log. SSH uses paramiko with the key file.
- **Validate-before-write** + **backup-before-write** make a failed pull
  non-destructive.
- The **non-destructive merge** (and credential merge-by-id) means a pull can
  never delete the operator's own entries — including the auth credential.
- A **filename allowlist** (bare names only) blocks writing outside the app dir.

## 8. Slices

| # | Slice | Status |
|---|-------|--------|
| C1 | `gitsync.py` — dulwich fetch/list + paramiko SSH vendor | ✅ 0.73.0 |
| C2 | `config.read_sync` / `write_sync` (`[config_sync]`) | ✅ 0.73.0 |
| C3 | Bridge `get/save_sync_config`, `list_sync_tags`, `sync_pull` + hot-reload | ✅ 0.73.0 |
| C4 | `ConfigSyncTab` (Settings) + bridge interface | ✅ 0.73.0 |
| C5 | Tests (`test_gitsync.py`, `test_sync_pull.py`) | ✅ 0.73.0 |
| C6 | Non-destructive merge + `preserve` (`configmerge`, `test_configmerge.py`) | ✅ 0.74.0 |
| C7 | TLS CA bundle + proxy for private hosts (`_https_pool_manager`) | ✅ 0.75.0 |

## 9. Testing

- `tests/test_configmerge.py` unit-tests the pure merge helpers: deep overlay
  (repo wins, local-only survives), mapping preserve (local wins / repo can't
  introduce), collection merge (add/update/keep-local-only) and per-entry preserve
  (`*` and named).
- `tests/test_gitsync.py` builds a throwaway local repo with two tags and checks
  tag listing, per-tag file bytes, missing-file skip, and unknown-ref error
  (skipped when dulwich or git is absent).
- `tests/test_sync_pull.py` mocks `gitsync.fetch_files` and checks the
  orchestration: `[config_sync]` preserved + `last_*` stamped across the
  `config.toml` merge; `config.toml` deep-overlay keeps local-only + default
  preserve; hosts merge keeps local-only + preserves `room_user`; filters /
  triggers merge by name; invalid TOML aborts the whole pull; backups created;
  credentials merged by id (local-only survives); hot-reload + trigger restart
  fired; missing URL errors.

Manual end-to-end: `git init` a temp repo with the config TOMLs, `git tag v1`,
set `[config_sync]` to its `file://`/path URL + `v1`, and run `sync_pull()` (or
the UI **Pull now**) — files land in the app dir, backups appear, hosts/triggers
reload live.
