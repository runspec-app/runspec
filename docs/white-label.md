# White-label walkthrough

This page builds, start to finish, a distributable console for your team:
one wheel — call it `mycorp-console` — that an operator installs from your
private index and launches into a console that is **branded as yours** and
**pre-configured**: your LLM gateway active, hosts, groups, profiles,
filters, and starter runbooks already in place, and your theme on the
window. Nothing on this page modifies runspec-console itself — it's all
standard packaging around it.

The moving parts:

| Mechanism | What it does |
|---|---|
| A custom **LLM adapter** entry point (`runspec_console.adapters`) | Makes your gateway a selectable provider — and, when active, the console brands its window title, docs link, and update check from *your* package's metadata |
| A **config seed** entry point (`runspec_console.config_seed`) | Ships baseline config files as package data, applied on first launch and re-applied on upgrade — works fully offline |
| **`[console.theme]`** (delivered by the seed) | Accent colour, brand name, icon, watermark, title-bar tint |
| **`[project.scripts]`** | A branded launcher command |

A [Config Sync](console-team.md) repo can layer on top of the seed for
day-to-day config changes; the seed is the offline floor underneath it
(seed < sync < the operator's local edits).

---

## 1. Project skeleton

Start from the example plugin in the runspec repo —
`packages/python/runspec-console/examples/console-adapter-plugin/` — and
rename. Target layout:

```
mycorp-console/
├── pyproject.toml
└── mycorp_console/
    ├── __init__.py
    ├── adapter.py             # your LLM provider
    ├── app.ico                # your icon (package data)
    └── config_seed/           # baseline config (package data)
        ├── config.toml
        ├── runspec_hosts.toml
        ├── runspec_groups.toml
        ├── runspec_profiles.toml
        ├── runspec_filters.toml
        └── runspec_runbooks.toml
```

The `pyproject.toml` wires everything:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "mycorp-console"
version = "1.0.0"
description = "MyCorp operations console"
requires-python = ">=3.11"
dependencies = [
    "runspec-console[credentials,git-sync,schedule,notifications]>=0.174.0",
    "httpx>=0.27",                     # your gateway client's deps
]

# Branding is derived from THIS package's metadata when your provider is active:
# window title from the distribution name, docs link from these URLs, and the
# update check tracks this package against your index.
[project.urls]
Documentation = "https://docs.mycorp.example/console"
Homepage = "https://mycorp.example"

# A branded launcher alongside `runspec-console` — same console underneath.
[project.scripts]
mycorp-console = "runspec_console.app:main"

# Your gateway becomes a selectable provider: `provider = "mycorp"`.
[project.entry-points."runspec_console.adapters"]
mycorp = "mycorp_console.adapter:MyCorpAdapter"

# Your bundled baseline config: a package with a config_seed/ data directory.
[project.entry-points."runspec_console.config_seed"]
mycorp = "mycorp_console"

[tool.hatch.build.targets.wheel]
packages = ["mycorp_console"]
```

Make sure the data files ship in the wheel (hatchling includes package
directories by default; verify `config_seed/` and `app.ico` appear in the
built wheel).

---

## 2. The adapter

The adapter is what makes the console *yours*: branding follows the **active
`[llm] provider`**, and an entry-point provider resolves to the distribution
that advertises it. Two routes:

- **Your gateway speaks a LangServe-ish `/invoke` dialect** (most corporate
  LLM gateways): subclass the bundled `LangServeAdapter` and override the
  request/response mapping — often just a few lines.
- **Anything else**: implement the `ModelAdapter` base class directly.

The example plugin's `adapter.py` shows both, and the console ships a
conformance test kit — wire it into your CI:

```python
from runspec_console.adapters.testing import (
    assert_adapter_contract, assert_chat_response, assert_tool_turn,
)
```

The full adapter API, streaming contract, and the `[llm]` keys your adapter
receives are documented in [Custom model adapters](console-adapters.md).

---

## 3. The config seed

Everything in `config_seed/` must be one of the known config files (the same
set [Config Sync](console-team.md#config-sync) manages). What to put in each:

**`config.toml`** — the heart of the seed:

```toml
[llm]
provider = "mycorp"                      # your adapter, active out of the box
base_url = "https://llm-gw.mycorp.example"
models   = ["mycorp-sonnet", "mycorp-haiku"]
model    = "mycorp-sonnet"
# No api_key here — secrets are never seeded. Use api_key_command, or have
# operators paste their key once in Settings → Model / API.
api_key_command = "mycorp-auth token --scope llm"
api_key_ttl_ms  = 300000

[console]
docs_url = "https://docs.mycorp.example/console"

[console.theme]
brand_name      = "MyCorp Console"
accent_color    = "#b3172a"
icon            = "pkg:mycorp_console:app.ico"   # your bundled icon
title_bar_color = "#7a0f1d"                      # Windows 11 caption tint
watermark_text  = "MyCorp internal"
watermark_style = "banner"                       # tiled | banner | center

# Bootstrap the live config layer: consoles pull day-to-day config from git.
[config_sync]
repo = "https://git.mycorp.example/ops/console-config.git"
tag  = "v1"
# Keys you expect each operator to own — a seed/pull re-assert never overwrites
# these; the local value always wins. "<file>:<dotted.path>", * = any entry.
preserve = [
  "runspec_credentials.toml:*.username",   # every credential's username stays local
  "config.toml:llm.api_key_command",       # a machine-specific key command wins
]

[ssh]
proxy = "http://proxy.mycorp.example:8080"       # your network's realities
```

The whole `preserve` list lives here, in `[config_sync]` — **it is not a field on
the entries it protects.** You never add anything to a `[[credential]]` or
`[[host]]` table; each preserve string names its target by path:
`"<file>:<entry>.<field>"`, where `<entry>` is a specific `id`/`name` or `*` for
**every entry in that file**. So one line —
`"runspec_credentials.toml:*.username"` — pins the username across *all* seeded
credentials; `"runspec_credentials.toml:jira-prod.username"` would pin just that
one. (For `config.toml`, which isn't a list of entries, the path is simply the
dotted key: `"config.toml:llm.api_key_command"`.)

**`api_key_command` resolves relative to the install.** A seeded key helper is
commonly a console-script you ship in the *same wheel*
(`[project.scripts] mycorp-auth = "mycorp_console.auth:main"`), but the venv can
be created anywhere — so `api_key_command` runs with **this venv's script
directory prepended to `PATH`**, and seeding the bare command
(`api_key_command = "mycorp-auth token --scope llm"`) resolves wherever the venv
lives (a hardcoded `Scripts/`/`bin/` path would not). Two env vars are also
exported for a module-style helper: **`RUNSPEC_PYTHON`** (this venv's
interpreter) and **`RUNSPEC_VENV`** (its root) —
`api_key_command = '"$RUNSPEC_PYTHON" -m mycorp_console.auth token'`
(`%RUNSPEC_PYTHON%` under `cmd`). The secret itself is never seeded — only the
command that fetches it. Because a venv-relative command is correct on every
machine, no operator ever needs to override it locally, so re-assert-on-upgrade
(below) never fights you over this key.

**`runspec_hosts.toml`** — the fleet, ready in the sidebar on first launch:

```toml
[[host]]
name          = "app-01"
ssh           = "svc-ops@app-01.mycorp.example"
runspec_paths = ["/opt/venvs/ops/bin/runspec"]
group         = "app"
profiles      = ["production"]
```

**`runspec_groups.toml` / `runspec_profiles.toml` / `runspec_filters.toml`**
— the group vocabulary, host working-sets, and output filters your runbooks
and docs assume.

**`runspec_runbooks.toml`** — starter procedures, so the agent has your
playbook from day one:

```toml
[[runbook]]
id = "morning-checks"
title = "Morning fleet checks"
symptom = "Start-of-day verification"
steps = """
1. Run health-check across the `app` group and summarise failures.
2. Check overnight backup logs on nas-01 and flag anything unusual.
"""
```

You can also seed `runspec_triggers.toml`, `runspec_repos.toml`,
`runspec_credentials.toml` (**metadata only** — secret values always come
from each operator's own keychain), and `runspec_self_service.toml`.

Seeded credentials pair especially well with the
[platform-credential naming convention](console-forms.md#one-credential-across-many-runnables-a-naming-convention):
seed entries labelled `Windows` and `Linux`, have every runnable read
`WINDOWS_USERNAME`/`WINDOWS_PASSWORD` and `LINUX_USERNAME`/`LINUX_PASSWORD`
via `env` fallbacks, and each operator supplies two secrets once to cover the
whole catalogue.

### How the seed is applied

On every startup the console compares the installed seed packages' versions
against a machine-local stamp: unchanged means no-op; new or upgraded means
the seed re-folds onto the local config through the same non-destructive
merge Config Sync uses — collections merge by `name`/`id`, `config.toml`
deep-overlays, and the operator's local edits and `preserve`-pinned keys win.
So shipping `mycorp-console 1.1.0` with an updated seed rolls the baseline
forward on the next launch, without stomping local changes.

The one thing to brief operators on: a re-assert **does** overlay the seed's
value on top of local for any *seed-shipped* key it declares — so an operator
who edited a seeded key in place (rather than adding their own entry or
`preserve`-ing it) sees it revert on the next upgrade. That's the point of a
package-controlled baseline, but it shouldn't surprise anyone. **Settings →
Config Sync → Config seed** makes it visible: alongside the seed's status and a
**Re-apply seed** button, it lists any **local values a re-assert would
overwrite** — with their `local → seed` values and a hint to add the key to
*Preserve* — and, separately, the ones a `preserve` rule already protects. So an
operator can spot and pin a machine-specific edit *before* an upgrade reverts it.
Design a key you expect operators to own (a machine-local path, a personal
account name) to be `preserve`d from the start — via the `[config_sync].preserve`
list in the seed's `config.toml` above — or seed it as a separate entry they can
override. A common one is a credential's `username`: seed the credential's id /
label / kind / arg-bindings so the entry exists, but `preserve`
`runspec_credentials.toml:*.username` so each operator's own account name (and
the keychain secret, which is never seeded regardless) survives every upgrade.

**Declare `preserve` in the seed's own `config.toml`** (as shown above) — the
seed apply reads the preserve list from *both* the seed it's applying and the
local config, so a seed-declared preserve protects the operator's local value
from the **very first** re-assert that ships it. (Preserve read only from local
config would be one upgrade behind: the upgrade that introduces the preserve
runs against the *old* local config, clears the value, then writes the preserve
— which looks exactly like preserve being ignored.) Two caveats still hold: a
preserve can only keep a value that is **already in the local file** at upgrade
time (it restores the operator's current value, it can't bring back one an
earlier upgrade already cleared — so if usernames were blanked before you added
the preserve, operators re-enter them once and they stick thereafter); and the
**secret** behind a credential is never seeded regardless, so an operator always
sets that once in **Settings → Credentials**.

---

## 4. Theme reference {#theme}

All keys live under `[console.theme]`, delivered by the seed (there's no
Settings editor — deliberate, so the brand is package-controlled). Invalid
values are dropped, never fatal; with none set the console is a stock
install, pixel-identical.

| Key | Accepts | Effect |
|---|---|---|
| `brand_name` | text | Sidebar title (unset ⇒ "runspec") |
| `accent_color` | `#rgb` / `#rrggbb` | UI primary colour **and** the generated app icon |
| `icon` | file path or `pkg:<module>:<relpath>` | Explicit `.ico`/`.png` app icon (bundle it as package data with the `pkg:` form) |
| `title_bar_color` | hex | Native Windows 11 caption tint — independent of `accent_color`, so a blue-accent UI can carry a red title bar; no-op elsewhere |
| `watermark_text` | text | Faint watermark behind content |
| `watermark_logo` | `http(s)`/`data:` image URL | Image watermark |
| `watermark_style` | `tiled` / `banner` / `center` | Repeated marks, one large upper-third mark, or one centred mark |

The window title, in-app documentation link, and update-check target come
from your **package metadata** (§1), not the theme.

---

## 5. Build, publish, install

```
python -m build
twine upload --repository-url https://pypi.mycorp.example/simple dist/*
```

Operator install — this is the entire rollout instruction:

```
pip install mycorp-console --index-url https://pypi.mycorp.example/simple
mycorp-console
```

The footer's update badge now tracks `mycorp-console` against your index, so
publishing `1.1.0` prompts every operator. For fleet-side venvs behind the
same private index, the `configure-pip` runnable in `runspec-linux` writes
the `pip.conf` — drivable by the agent during
[host bootstrap](console-hosts.md#bootstrapping-a-fresh-host).

## 6. Verify

Launch on a clean machine (or a fresh `%APPDATA%` by temporarily renaming
`runspec-console/`) and check:

- [ ] Window and sidebar show **MyCorp Console**, your icon, your accent —
  and the title bar tint on Windows 11.
- [ ] **Settings → Model / API** shows `mycorp` active; a chat turn answers
  through your gateway.
- [ ] The sidebar lists your seeded hosts and groups; the profile switcher
  shows your profiles.
- [ ] The agent knows the seeded runbooks: ask *"what runbooks do you
  have?"*.
- [ ] **Settings → Config Sync** shows the seed stamped with your package
  version, and the git layer configured.
- [ ] The in-app Documentation link opens your docs; the footer update check
  names your package.
