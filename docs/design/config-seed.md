# Config Seed — ship a baseline config inside the deployment package

> **Status: shipped** — `runspec-console` 0.152.0. A white-label / corporate
> distribution that already wraps the console (its own LLM adapter + branding)
> **bundles a baseline config as package data** and applies it on bring-up, so a
> fresh install is configured **with no external provider** — no git host, no
> private PyPI index reachable at run time. Built entirely on the existing
> Config Sync merge engine (`configmerge`) — the seed is just a new *source*
> feeding the same pipeline. Pairs with Config Sync (`docs/design/config-git-sync.md`)
> and the adapter/branding model (`branding.py`). **Decision locked: re-assert on
> upgrade** (§8).
>
> **Scope:** a self-contained, versioned config floor that works in a disaster-
> recovery / air-gapped scenario where the git repo or internal index is
> unreachable. Config Sync stays the optional **live** overlay on top.

---

## 1. The problem

Config Sync (0.73.0) made a git repo the source of truth for a console's config:
a fresh machine enters a repo URL + tag + credential, clicks **Pull**, and is
configured. That works well, but it makes the **git provider a hard runtime
dependency** of provisioning. Two scenarios break it:

* **Disaster recovery / air-gapped.** The git host (or the network path to it) is
  down exactly when an operator is rebuilding a console. The internal PyPI index
  the white-label package is installed from is a second such dependency.
* **Self-contained installs.** Some deployments prefer a single artifact: a wheel
  you can copy to a host and `pip install ./brand.whl`, with **no live index and
  no git pull** required to reach a working, on-brand console.

The deployment package is already the thing being installed. If it also **carries
the baseline config**, provisioning needs no second provider: the wheel *is* the
source of truth for the floor. The trade-off — accepted deliberately — is that a
change to the baseline is a **new package version to install**, which is the right
cadence for slow-moving, blessed config (LLM provider, branding, fleet defaults).

This is **not** a replacement for Config Sync. It's a floor *underneath* it:

```
local (operator edits)  ←  package seed  ←  [optional git-sync layers]  ←  preserve
```

An install degrades gracefully from "fully live" (seed + git pull) to
"baseline only" (seed, no provider reachable) with **no different code path** —
both top up local config through the same engine.

## 2. Decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Where the config lives | **Package data** inside the deployment distribution (`<pkg>/config_seed/*.toml`), read via `importlib.resources`. No filesystem layout assumptions; works from a wheel. |
| 2 | Discovery | A dedicated **entry-point group `runspec_console.config_seed`**, declared by the brand package right alongside its LLM-adapter entry point. Decoupled (the console never names the brand) and supports a seed-only package. See §5. |
| 3 | Merge engine | **Reuse `configmerge` unchanged.** The seed is a set of parsed layers fed to the same `fold_layers` + per-file merge that a git pull uses. No new merge code. See §8. |
| 4 | Apply path | **Reuse the post-fetch half of `sync_pull`.** Extract it into `_apply_config_layers(...)` (validate → backup → fold → merge → hot-reload) and call it from both git pull and seed apply. See §9. |
| 5 | When it applies | **On bring-up, when the seed *version* changed** (re-assert-on-upgrade). Idempotent: same version → no-op, no backups. See §6, §8. |
| 6 | Versioning | The stamp is `{distribution: version}` read from `importlib.metadata` — automatic, no hand-maintained number. A package upgrade rolls the baseline forward. |
| 7 | Stamp storage | A **machine-local** `runspec_seed_state.toml` — **not** in `gitsync.KNOWN_FILES`, **never synced** (mirrors `runspec_repo_paths.toml` and the active-profile marker). |
| 8 | Secrets | **Never seeded** — exactly as Config Sync already requires. The package carries metadata/structure; secret bootstrap (keyring, copy-ssh-id) stays separate. See §10. |
| 9 | Files | The same `gitsync.KNOWN_FILES` allowlist (bare names only, no path traversal). A seed may carry any subset. |

## 3. What gets seeded

The mechanism is generic — **any `config.toml` key deep-overlays for free** — so the
inventory below is "what a brand package would put in its `config_seed/`," not a
fixed schema. Each row notes the secret rule and whether rendering is net-new
work.

| Section | Example keys | Secret (never seeded) | Notes |
|---|---|---|---|
| `[llm]` | `provider`, `model`, `endpoint` | `api_key` (keyring / local-only) | The adapter package's core purpose; the seed names *its own* provider, so a fresh install boots on-brand. |
| `[console]` branding | `docs_url` (exists); **`theme.accent_color`**, **`theme.watermark_text`**, **`theme.watermark_logo`** (net-new) | — | `docs_url` already honoured; window title auto-derives in `branding.py`. **Theme colour + watermark are delivered by the seed but require frontend rendering** — out of scope for the seed feature itself (see §12). |
| `[config_sync]` | `url`, `ref`, `credential_id`, `subpath`/`layers`, `preserve` | the credential secret (keyring) | **Bootstraps live updates:** the package ships *where to pull from*, so a reachable install can immediately top up over git. See §7 for the `last_*` interaction. |
| `[ssh]` | `proxy`, `identityFile`, connection tuning | private key material (on disk / keyring) | Fleet-wide SSH defaults. |
| `[console]` notifications | `confirm_notifications`, `room_notifications` | — | Existing booleans. |
| `[console.sounds]` | `enabled`, `volume`, per-category tokens | `pack` + `file:` tokens (machine-local filesystem paths) | Existing table (`DEFAULT_SOUND_MAP`). A seed should stick to `synth:`/`system:`/`off` tokens — `file:` paths and the `pack` folder are machine-local. |
| collection files | `runspec_hosts.toml`, `runspec_groups.toml`, `runspec_credentials.toml` (metadata), `runspec_runbooks.toml`, … | credential secrets (keyring) | Seeded by identity-merge, same as a git pull; an operator's own entries survive. |

**`[config_sync]` as bootstrap is the elegant bit.** The seed carries the git
pointer; the install knows where to find *more* config the moment a network path
exists. A package upgrade that bumps the seed's `ref` also rolls the whole
fleet's git pin forward (re-assert, §8) — central control of the live layer via
a package release.

## 4. Net precedence

Seed and git-sync both *top up* local through the same engine, so they compose
without special-casing:

```
seed (lowest)  →  git layers (_base ← region ← team)  →  local edits  ←  preserve (pinned on top)
```

Concretely: seed applies on bring-up → local now holds the baseline → an operator
git pull (if reachable) tops up on top → the operator's local-only entries and
`preserve` paths win over both. For shared entries, **git updates what the seed
set, and a package upgrade re-asserts the seed** (the tension §8 resolves).

A future nicety (out of scope for v1): fold the seed in as the *bottom layer of
the pull fold itself*. The standalone bring-up apply already delivers the DR
guarantee on its own, so v1 keeps them as two independent top-ups.

## 5. Discovery & versioning

A brand package declares **one** extra entry point next to its adapter. The
entry-point *value* is the package that holds the `config_seed/` data dir (no
`:attr` — it's a module, not a callable). PEP 621 form:

```toml
[project.entry-points."runspec_console.adapters"]
mycorp = "mycorp_console.adapter:Provider"

[project.entry-points."runspec_console.config_seed"]
mycorp = "mycorp_console"     # a package containing a config_seed/ data dir
```

**Poetry** (what an adapter package commonly uses) — entry points are
`[tool.poetry.plugins."<group>"]`, and the bundled TOMLs must be included in the
wheel. Files **inside** the package directory are picked up by default; the
explicit `include` makes it robust against a `.gitignore` that ignores generated
config or a non-default `packages`/`exclude` setup:

```toml
[tool.poetry.plugins."runspec_console.adapters"]
mycorp = "mycorp_console.adapter:Provider"

[tool.poetry.plugins."runspec_console.config_seed"]
mycorp = "mycorp_console"     # a package containing a config_seed/ data dir

# Ensure the data dir ships in the wheel (sdist + wheel).
[tool.poetry]
include = [{ path = "mycorp_console/config_seed/*.toml", format = ["sdist", "wheel"] }]
```

So the layout is:

```
mycorp_console/
  __init__.py
  adapter.py
  config_seed/
    config.toml            # [llm], [console], [config_sync], [ssh] …
    runspec_hosts.toml     # optional — any KNOWN_FILES subset
```

A new `configseed.py` (sibling to `gitsync.py`) resolves contributors:

* For each entry point in group `runspec_console.config_seed`, read
  `importlib.resources.files(<value>) / "config_seed"` and collect every
  `*.toml` whose name is in `gitsync.KNOWN_FILES` → `{name: bytes}`.
* The **stamp** is `{ep.dist.name: ep.dist.version}` for every contributing
  distribution — read straight from installed metadata, so it tracks the package
  version with nothing to hand-maintain.
* Multiple contributors (e.g. a brand package + a region add-on) fold **low→high
  by sorted distribution name** — a bounded, documented order, no priority DSL.
  One contributor is the common case.

This mirrors how `branding.py` already derives identity from a distribution's
metadata, and keeps "one brand package = adapter + branding + seed" a single
coherent unit.

## 6. Flow

```
bring-up (app.py / Bridge)            configseed.py                 _apply_config_layers (shared)
──────────────────────────            ─────────────                 ────────────────────────────
seed_apply(force=False) ──▶  discover():
                               entry points → {name: bytes}, stamp={dist: ver}
                             read_seed_state() → stored stamp
                             if stamp == stored and not force: return {ok, skipped:"unchanged"}
                             else  ──▶  _apply_config_layers({name:[bytes]}, label="seed"):
                                          1. validate every .toml (abort on parse error)
                                          2. per file: back up → fold_layers → _merge_synced_file
                                          3. prune backups
                                          4. hot-reload hosts/filters/creds/ssh + restart triggers
                             write_seed_state(stamp)              ◀── {written, skipped, backups}
                           ◀── {ok, applied, written, backups}
```

`seed_apply` runs early in Bridge bring-up — after the config dir exists, before
hosts are probed — so the merge's own hot-reload leaves the app consistent with
no restart. The **Re-apply seed** button (Settings) calls `seed_apply(force=True)`.

## 7. Config shape

The bundled seed is just ordinary config files under `config_seed/`:

```toml
# mycorp_console/config_seed/config.toml  (shipped in the wheel)
[llm]
provider = "mycorp"
model    = "claude-opus-4-8"

[console]
docs_url = "https://docs.mycorp.example/console"

[console.theme]          # net-new keys — carried now, rendered later (§12)
accent_color   = "#b3001b"
watermark_text = "MyCorp — Internal"

[config_sync]            # bootstrap the live layer
url           = "https://git.mycorp.example/ops/runspec-config.git"
ref           = "v1.4.0"
credential_id = "config-repo"

[ssh]
proxy = "socks5h://127.0.0.1:1080"
```

The machine-local stamp (never synced, never seeded):

```toml
# runspec_seed_state.toml  (in the app dir, machine-local)
[seed]
applied = { "mycorp-console" = "1.4.0" }
last_applied = "2026-06-29T09:15:00Z"
```

**`[config_sync].last_*` interaction.** A seed that carries `[config_sync]` must
**not** clobber the machine-local run-state the git pull stamps
(`last_ref` / `last_commit` / `last_synced`). The seed apply therefore treats that
triplet like `preserve` — restoring the local values after merging `config.toml`
(the same re-injection pattern `sync_pull` uses for the local `[config_sync]`
pointer). The seed carries only `url` / `ref` / `credential_id` / `subpath` /
`layers` / `preserve`, never the `last_*` stamps.

## 8. Merge semantics — seed-once vs. re-assert-on-upgrade

This is **the** design decision, because `merge_collection` / `deep_overlay`
*update* matching entries by identity. Two models:

| Model | On upgrade | Operator override | Drift |
|---|---|---|---|
| **Seed-once** (first install only) | nothing re-applies | free in-place edits | a baseline fix can't be pushed via a version bump — drift returns |
| **Re-assert** (recommended) | the changed seed re-folds onto local | override by **adding your own entries** or `preserve`, not by editing a seed-owned entry in place | a version bump genuinely rolls config forward across the fleet |

**Decision: re-assert (locked).** It's what makes "config change = new package
version" actually mean something, and it's the **same discipline Config Sync
already trains operators on** (a pull updates matching entries; local-only
entries and `preserve` paths survive). The escape hatches are identical:

* **Local-only entries survive** — a host the seed never shipped is never touched.
* **`preserve` pins machine-local keys** — `configmerge.DEFAULT_PRESERVE`
  (`mailbox.folder`, every host's `room_user`) plus the seed's own preserve list
  and the `[config_sync].last_*` triplet (§7).
* **In the air-gapped/DR case there is no git layer at all**, so the seed is
  simply the only baseline and the "re-assert over a git/local edit" tension
  doesn't arise.

The cost to state plainly: under re-assert, an operator who edits a *seed-shipped*
entry (e.g. flips the accent colour back to blue, or edits a seeded host) will see
the seed value **re-asserted on the next package upgrade** unless it's `preserve`d.
That's the point of a centrally-controlled baseline — but it must be documented so
it isn't a surprise.

The stamp guard (§6) is what makes this cheap: between upgrades the seed is a pure
no-op (stamp unchanged → return early, zero file writes, zero backups), so there's
no per-boot churn.

**Making the tension visible.** So a re-assert doesn't silently revert an
operator's edit, `seed_status` also returns an **`overrides`** list — the seeded
leaf keys whose *current local* value differs from the seed's, each flagged
`preserved` when a `preserve` rule already pins it local. The Settings → Config
Sync → Config seed panel shows these ahead of the next upgrade / **Re-apply**: a
warning listing the unprotected local edits a re-assert would overwrite (with the
`local → seed` values and the hint to add the key to *Preserve*), and a separate,
non-alarming list of the ones a preserve rule already keeps. The diff is computed
by the pure `configmerge.seed_overrides(name, effective, local, preserve)` against
each bundled file folded to its effective table (the custom-merged
`runspec_triggers.toml` is skipped); it's read-only and best-effort — never
blocks status.

## 9. Reused vs. new

**Reused:**

* `configmerge` **entirely** — `fold_layers`, `merge_collection`, `merge_mapping`,
  `deep_overlay`, `parse_preserve`, `DEFAULT_PRESERVE`, `COLLECTION_FILES`.
* `Bridge._merge_synced_file` / `_merge_synced_triggers` — the per-file
  "effective table → local" routine, unchanged.
* The hot-reload chain — `_reload_hosts` / `_reload_filters` /
  `_reload_credentials` / `_after_config_change` / `restart_triggers`.
* `_atomic_write_bytes`, `_prune_backups`, the TOML-validate-before-write gate,
  the `gitsync.KNOWN_FILES` allowlist.
* `branding.py`'s metadata-resolution pattern (`importlib.metadata`) for the
  discovery + stamp.

**New (small):**

* `runspec_console/configseed.py` — entry-point discovery + stamp
  (`discover() -> ({name: bytes}, stamp)`), pure/IO-thin.
* `Bridge._apply_config_layers(fetched_layers, *, preserve_extra, backups_keep,
  label)` — the **extracted** post-fetch half of `sync_pull` (S1); `sync_pull`
  keeps fetch + `last_*` stamping and calls it.
* `Bridge.seed_apply(force=False)` + `config.read_seed_state` /
  `write_seed_state` (machine-local `runspec_seed_state.toml`).
* A bring-up call to `seed_apply()` (app.py / Bridge `__init__`).
* A read-only Config Sync status line + **Re-apply seed** button (small UI).

## 10. Safety

* **Secrets never ship in the package.** The seed carries non-secret config only
  (`[llm] api_key`, the `config_sync` credential's secret, SSH key material are
  all keyring/disk-local and bootstrapped separately) — the same contract Config
  Sync already enforces.
* **Validate-before-write + backup-before-write** are reused, so a malformed
  bundled file aborts the apply without clobbering good local config.
* **Filename allowlist** (`KNOWN_FILES`, bare names) blocks writing outside the
  app dir, even though the bytes come from a trusted package.
* **Idempotent + stamped** — a same-version boot writes nothing, so the feature
  can't churn config or accumulate backups on every launch.
* The stamp file is **machine-local and never synced**, so it can't leak into a
  shared repo and mis-report another machine's seed state.

## 11. Slices

| # | Slice | What | Status |
|---|-------|------|--------|
| S1 | Extract `_apply_config_layers` | Pure refactor of `sync_pull`'s post-fetch half; behaviour unchanged (covered by `test_sync_pull.py`). | ✅ |
| S2 | `configseed.py` | Entry-point discovery → `{name: bytes}` + `{dist: version}` stamp; fold multiple contributors by sorted dist name. | ✅ |
| S3 | Apply-at-bring-up + stamp | `runspec_seed_state.toml` (machine-local), `seed_apply(force)`, the version guard, the `[config_sync].last_*` preserve, the bring-up call. | ✅ |
| S4 | UI status (small) | Config Sync tab: "Bundled: `<dist> v<ver>`" + last-applied + **Re-apply seed** (`seed_status`/`seed_apply`). | ✅ |
| S5 | Tests | `test_configseed.py`, `test_seed_apply.py` (see §13). | ✅ |
| S6 | Docs + version | This doc → "shipped"; CLAUDE.md status note; console version bump + `## [x]` CHANGELOG entry. | ✅ |

## 11a. Seeding an `api_key_command` (venv-relative execution)

A brand package usually fetches the API key from a helper it ships **into the
same venv** rather than seeding a static `[llm] api_key` (which is a secret and
never seeded, §10). The catch: the venv can be created anywhere, so a seeded
`api_key_command` that hardcodes a path into the venv's `Scripts/` (Windows) or
`bin/` (POSIX) breaks on a machine whose venv lives elsewhere.

`api_key_command` is therefore executed with the **current venv made
discoverable** (`adapters/base.run_api_key_command` / `api_key_command_env`),
so a seeded command can be **venv-relative** and resolve wherever the venv was
created:

* **`PATH`** — this venv's script dir is prepended. Ship your key helper as a
  console-script entry point in the same wheel and seed the bare name:

  ```toml
  # config_seed/config.toml
  [llm]
  api_key_command = "get-key"        # a console-script installed into this venv
  ```

* **`RUNSPEC_PYTHON`** — the venv interpreter (`sys.executable`), for a module
  helper: `api_key_command = '"$RUNSPEC_PYTHON" -m mycorp_console.getkey'`
  (`%RUNSPEC_PYTHON%` under `cmd`).
* **`RUNSPEC_VENV`** — the venv root (`sys.prefix`), to locate other bundled
  files relative to the install.

Because a venv-relative command is correct on every machine, re-assert-on-upgrade
(§8) is a feature here, not a hazard — there's no per-machine local override to
protect. If you *do* keep a machine-specific command, pin it with
`preserve = ["config.toml:llm.api_key_command"]` so the seed can't re-assert over
it. (The **secret** itself — a service-account password, an `api_key` — is never
seeded; only its *fetch command* is.)

## 12. Out of scope (follow-ups)

* **Theme rendering.** ✅ **Shipped (0.153.0)** — the seed *delivers*
  `[console.theme]` values, and the console now renders them (accent colour →
  `colorPrimary` + app icon, watermark, sidebar brand name). See
  `docs/design/console-theme.md`.
* **Seed-as-bottom-fold-layer of a git pull** (§4) — v1 keeps seed and pull as two
  independent top-ups.
* **Priority DSL for multiple contributors** — v1 folds by sorted dist name only.

## 13. Testing

* `test_configseed.py` — discovery against a **fake entry point** (a temp
  distribution exposing a `config_seed/` dir): collects only `KNOWN_FILES`,
  builds the `{dist: version}` stamp, and folds two contributors low→high by
  sorted name.
* `test_seed_apply.py` — orchestration, mirroring `test_sync_pull.py`'s mocking
  style:
  * first boot merges + stamps; a **second boot with the same stamp is a no-op**
    (no writes, no backups);
  * a bumped stamp re-applies (re-assert);
  * the **air-gapped path** (no `[config_sync]`) still produces a working baseline;
  * a seed entry sharing a host `name` with a local edit is **re-asserted unless
    `preserve`d**;
  * `[config_sync].last_*` survives a seed that carries `[config_sync]`;
  * a malformed bundled TOML aborts the whole apply (nothing written).
* `scripts/check_docs.py` — `config_seed` and `seed` added to
  `NON_RUNSPEC_SECTIONS` so this doc's `config.toml` / stamp examples are exempt
  from runspec-schema validation.
