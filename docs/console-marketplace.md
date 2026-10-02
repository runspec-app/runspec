# Marketplace — install runnable packages

The **Marketplace** tab lists runspec runnable packages you can install into
the console's venvs and into the venvs of your connected fleet hosts — the
same packages you would otherwise `pip install` by hand (`runspec-linux`,
`runspec-windows`, `runspec-jira`, a private `acme-ops`, …), as cards with a
description, tags, links, and the per-venv install state.

Nothing is curated by hand. The catalogue is **generated from package
indexes**: the console reads the indexes its own venv's pip is configured
with (plus any you declare) and lists every distribution that depends on
`runspec`.

---

## What you see

Each card is one package:

- **name · latest version** and the repo(s) it came from;
- the package's **summary**, **keywords** as tags, and links to its
  **Docs / Home / Source / Changelog** (from the package's `Project-URL`
  metadata) — *Details* opens the long description (README) and the
  dependency list;
- **where it is installed**: one chip per venv — green when current, orange
  with `↑` when the index has a newer version, purple when the venv is ahead;
- **Install…** / **Update…** / **Install / reinstall…** and **Uninstall…**.

The toolbar filters by text, by repo, and by scope (**All / Installed /
Updates / Not installed**). **Refresh** re-reads the indexes and re-probes
every venv; the status line shows when each repo was last read and how many
venvs were reachable.

## Targets — which venvs

A package can be installed into:

- the console's **own venv** and every venv matched by
  [`[local] venv_globs`](console-hosts.md#local-venvs) (`local`);
- each **fleet host's** `runspec_paths` venvs, over the host's pooled SSH
  connection — the active profile's working set, like the sidebar.

Disconnected hosts are listed but cannot be picked. The install picker
shows each venv's Python version and current install, pre-selects the
sensible set (the outdated venvs for an update; nothing when the package is
already current everywhere), and lets you choose a specific **version**,
**force reinstall**, or **Also upgrade runspec**. That last one adds the
runspec library to the same pip run (`pip install -U <package> runspec`), so a
package that needs a newer runspec lands together with it.

### Run as — service-account venvs

A venv on a fleet host often belongs to a service account (created with
the console's **Provision venv…** and a run-as account). The picker's **Run as** column is
pre-filled with the venv's owner whenever it differs from your SSH login
user, and the install runs escalated — `sudo -n -u <owner>` (or `su`) —
the same way the console provisions venvs. Leave it blank to run as
the login user.

A shared venv also needs its **logs inbox** set up once, so the runs of the
packages you install reach its History whoever runs them. **Provision venv…**
does this; for an older venv use **Logs → Make logs shared**, or run
`sudo -u <owner> <venv>/bin/runspec logs share --inbox`. See
[Set up the logs inbox](console-hosts.md#set-up-the-logs-inbox-for-runs-by-other-accounts).

The command a target runs is the plain
`<venv>/bin/python -m pip install [-U] [--force-reinstall] <package>` (or
`pip uninstall -y`), so a host needs **no** `runspec-linux` to be managed
from the Marketplace. Only **named package specs** are ever passed — a URL,
path or option is refused — so the Marketplace can only install what the
configured indexes publish.

pip's output streams live per target; a finished install kicks a discovery
refresh so the new runnables appear in Forms and in the agent's toolset
without a restart.

### Several at once, and the Activity list

An install, update or removal runs in the background. Starting one opens its
live view, which you can close straight away and start the next; nothing
waits for the previous one to finish. Running operations show in a
**Running:** strip under the toolbar and as an *installing…* tag on their
package's card; click either to reopen the live view.

Two operations never run pip in the **same venv** at once (that can corrupt
it): the second one shows that venv as *queued* and starts when the first
finishes. Different venvs run in parallel.

**Activity** (toolbar, with a badge counting running operations) lists every
operation from this console session, newest first, with its outcome
(*ok*, *2 of 5 failed*, *cancelled*). Click one to see each venv's result and
pip output. The list keeps the 25 most recent finished operations and is
cleared when the console restarts.

---

## Where the packages come from

### Automatically — your pip configuration

With **Marketplace → Settings → "Also read the indexes this venv's pip is
configured with"** on (the default), the console reads pip's own
configuration for the venv it runs in — `pip.ini` / `pip.conf` at the
global, user and venv scopes, plus `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` —
and turns `index-url` and every `extra-index-url` into a repo. A console
whose venv is pointed at a private Nexus / Artifactory / devpi index gets a
marketplace for that index with no further configuration; pip's default
pypi.org counts when nothing sets an index. A `user:pass@` embedded in the
URL is used for reading and never shown.

These repos appear read-only on the Settings tab. To customise one (name,
credential, explicit packages…), *declare* it — a declared repo with the
same index URL takes over.

### Declared repos

`[[marketplace.repos]]` in `config.toml` (Marketplace → Settings → *Add
repo*) declares an index explicitly:

```toml
[[marketplace.repos]]
name       = "Nexus"
url        = "https://nexus.example.com/repository/pypi-all/simple/"
credential = "nexus-svc"        # a Credentials-tab id → HTTP Basic auth
packages   = ["acme-ops"]       # read even when not matching the patterns
tags       = ["internal"]

[[marketplace.repos]]
name = "PyPI"
url  = "https://pypi.org/simple/"
```

| Key | Default | Meaning |
|---|---|---|
| `name` | *(from the URL)* | Label on cards and in the repo filter |
| `url` | — | The simple-API root (`…/simple/`) or the index base URL — the same value as pip's `index-url` |
| `enabled` | `true` | Read this repo |
| `scan` | `true` | List the whole index and keep names matching `patterns` (see below) |
| `packages` | `[]` | Package names to read regardless of `scan` / `patterns` |
| `patterns` | `["runspec-*"]` | Name globs a scanned project must match (overrides the section default) |
| `credential` | — | A Credentials id (username + password, or a token) sent as HTTP Basic auth when reading and carried to pip on install — only the id is stored |
| `pip_arg` | `auto` | How the target's pip is pointed at the repo on install: `auto` (a pip-configured primary index → `--index-url`, an extra → `--extra-index-url`, pypi.org → nothing), `none` (rely on the target's own pip config), `index-url`, `extra-index-url` |
| `trusted_host` | — | Pass `--trusted-host` for this host (pip skips TLS verification for it) |
| `tags` | `[]` | Free-form labels |

Config lives in `config.toml`, so declared repos ride
[Config Sync](console-team.md) and package [config seeds](white-label.md) —
one versioned list works fleet-wide.

### Scanning vs naming — is pypi.org fast enough?

A **scan** lists the index's root page and keeps the names that match the
patterns (`runspec-*` by default). For a private index that is a few
kilobytes. pypi.org's root listing is ~45 MB (≈10 MB compressed, ~900 000
projects); the console streams it through a name filter without building the
full list, so a scan takes a few seconds and is done **once per cache period**
(`cache_ttl`, default 24 h) — opening the tab afterwards is instant. Set
`scan = false` and list `packages` explicitly if you would rather not.

Reading a candidate's metadata then costs one small request per package —
pypi.org's JSON API where available (the richest source: README, project
URLs, keywords), else the PEP 658 `.metadata` sidecar of the newest wheel,
else the wheel itself (bounded by `max_wheel_bytes`), which also proves a
bundled `runspec.toml`. Candidates are read in parallel.

A scanned name that turns out to be a **registered-but-empty** project — no
releases, or a name that 404s — is skipped **silently** (it's discovery noise
you can't install or fix, e.g. a squatted/typo `runspec-*` name someone
registered on PyPI with no uploads). Only a name you list **explicitly** in a
repo's `packages` shows up in the top "N packages skipped" note when it can't be
read, so a typo you typed is still flagged.

### What counts as a runnable package

A distribution is listed when its `Requires-Dist` names `runspec` outside
any `extra ==` marker — a runnable package always does, because its
`runspec.toml` is parsed by runspec at run time — **or** when its
`Keywords` include `runspec-runnable` (a corporate wrapper that pins runspec
through another package). It is never listed when it is in the `exclude`
list: the `runspec` library itself and the apps built on it
(`runspec-console`, `runspec-mcp`, `runspec-room`, `runspec-registry`) by
default. The `*-core` logic packages don't depend on runspec, so they drop
out on their own.

The console itself is deliberately not a marketplace entry: it is installed
per machine, not into a fleet venv.

### Hiding packages (adapter / white-label deployments)

`[marketplace] hide` is a list of **name globs** of runnable packages to drop
from the catalogue — a curation knob for a deployment that ships **internal
wrappers** and doesn't want the public package offered alongside them, where
showing both would lead to the wrong install:

```toml
[marketplace]
hide = ["runspec-jira", "runspec-confluence", "runspec-snow", "runspec-gitlab"]
```

A hidden package never appears — no card, no search hit, and it isn't counted
in a repo's total. Matching is case-insensitive on the normalised name and
glob-capable (`runspec-*`). It is **additive** (unlike `exclude`, which lists
the non-runnable library/apps): the defaults are untouched, so hiding a public
package can't accidentally un-hide the console or the other apps. The list
lives in `config.toml`, so it rides [Config Sync](console-team.md) and the
package [config seed](white-label.md) — your adapter package ships one
versioned list and every console honours it. Edit it in **Settings →
Marketplace → Hidden packages**, or seed it.

`hide` only affects what the Marketplace *offers*; a hidden package that is
already installed stays installed (manage it with your internal package's card
or with pip).

---

## The agent and the Marketplace

The chat agent can **discover** installable packages, but it cannot install
them — installing is always an operator action on this tab.

A read-only `search_marketplace` tool lets the agent search the same catalogue
you see here for runnable packages your indexes offer that aren't installed
yet. It's the complement to the agent's `search_runnables` (which only sees
what's already installed): when you ask *"is there a runnable for GitLab?"* and
nothing is installed, the agent can find `runspec-gitlab` in your index, tell
you what it adds, and point you here to install it — rather than stopping at "I
don't have a tool for that". Each result carries the package name, latest
version, summary, keywords, a *verified* flag (it ships a `runspec.toml`), the
repo(s) it came from, and doc/source links.

It is **discovery only, by design**: the agent recommends, you install. That
keeps the useful part — closing the "there's no tool for that" gap — safe and
universal, without letting the agent run `pip install` across your fleet's
venvs on its own.

The tool:

- **honours `[marketplace] hide`** — a curated-out public package is never
  recommended, so your adapter deployment's agent points at your internal
  wrapper, not the public one;
- is offered only when `[marketplace] enabled` (dropped entirely when the
  marketplace is disabled);
- reads the machine-local catalogue **cache** (no network unless it's cold),
  and marks a package *installed* only when a recent probe already knew — it
  never triggers a fresh SSH sweep.

There is no agent-driven install: to add a package the agent found, use the
card here (or `pip` on the target).

---

## Making your package look good on the card

The card is built entirely from the package's published metadata, so a
package that publishes good metadata gets a good card:

```toml
[project]
name = "acme-ops"
description = "Acme internal ops runnables — deploy, rotate secrets, drain a node"
readme = "README.md"                       # → the Details view
keywords = ["runspec", "acme", "ops", "deploy"]

[project.urls]
Documentation = "https://wiki.example.com/acme-ops"
Source        = "https://git.example.com/platform/acme-ops"
Changelog     = "https://git.example.com/platform/acme-ops/-/blob/main/CHANGELOG.md"
```

`description` is the card's one-liner; `readme` becomes the Details view;
`keywords` become tags; `Documentation` / `Homepage` / `Source` /
`Changelog` / `Issues` project URLs become the link row. A wheel that ships
a `runspec.toml` earns a **verified** tag when the metadata was read from
the wheel itself.

---

## Settings & knobs

The Marketplace tab's **Settings** button edits the `[marketplace]` section (declared repos,
the pip-config switch, pre-releases, the cache period, clear cache). The full
key list is in the [config reference](console-reference.md#marketplace).

The catalogue cache (`marketplace_cache.json`) is machine-local and never
synced; the venv probe (what each target has installed) is held in memory
until an install / uninstall, a host (re)connects, or you press Refresh.

Set `[marketplace] enabled = false` to hide the tab and refuse every
marketplace call — a locked-down deployment that installs packages some
other way.
