# White-label runnable packages

The [white-label walkthrough](white-label.md) brands the **console** by wrapping
it in your own package. The runnable packs — `runspec-linux`, `runspec-windows`
and friends — are different: you usually don't want to *wrap* them, you want the
**same package under your own name** on your private index (Nexus, Artifactory,
devpi), so hosts install `mycorp-linux` from infrastructure you control and
every new upstream feature arrives without anyone hand-copying runnables.

`runspec rebrand` does exactly that: it takes a published wheel and writes a
copy whose **distribution name** is yours. Nothing else changes.

| Changed | Unchanged |
|---|---|
| Distribution name (`runspec-linux` → `mycorp-linux`) | Every `.py` file, byte for byte |
| `*.dist-info` / `*.data` directory names | `runspec.toml` — runnables, prompts, autonomy |
| `METADATA` `Name` (optionally `Version`) | Entry points / command names (`system-info`, `cron`, …) |
| `Requires-Dist` entries you ask it to re-point | Import package (`runspec_linux`) |
| `RECORD` (regenerated, hashes valid) | The `runspec` dependency and every version specifier |

Because the copy still requires `runspec` and still ships `runspec.toml`,
`runspec local`, `runspec serve`, the console's discovery and the MCP gateway
see exactly the same runnables as with the upstream package.

!!! tip "Why not a wrapper package?"
    A wrapper (depend on `runspec-linux-core`, write your own `runspec.toml` and
    thin modules) is the right tool when you want to **change** behaviour —
    bake in defaults, trim runnables, add corporate ones. For an unchanged,
    renamed copy it's pure maintenance: every upstream runnable has to be
    re-copied by hand. A rebrand is mechanical, so it can run unattended.

---

## 1. Rebrand one release

`rebrand` is part of the core `runspec` library (0.59.0+), so it reaches a
locked-down machine the same way runspec itself does — `pip install runspec`
from your Nexus PyPI proxy. There is no separate script to download from
GitHub, email or retype. It needs nothing beyond the standard library and pip.

```bash
# download runspec-linux 0.14.0 from the index pip is configured for, rename it
runspec rebrand runspec-linux==0.14.0 --name mycorp-linux --out dist/

# or rebrand a wheel you already have
runspec rebrand runspec_windows-0.9.0-py3-none-any.whl --name mycorp-windows --out dist/
```

```
✓ runspec-linux 0.14.0 → mycorp-linux 0.14.0
  wrote dist/mycorp_linux-0.14.0-py3-none-any.whl
  runnables: runspec_linux/runspec.toml
```

Then upload as usual: `twine upload --repository-url https://nexus.mycorp.example/repository/pypi-internal/ dist/*`.

**Keep the upstream version** (the default). `mycorp-linux 0.14.0` *is*
`runspec-linux 0.14.0`, so changelogs, bug reports and `runspec-linux-core`
compatibility all line up. `--version` exists for the rare re-publish
(`0.14.0.post1`), not for an independent version line.

## 2. The core packages — rename them or not

`runspec-linux` depends on `runspec-linux-core`, `runspec-webops-core` and
`runspec-fs-core`; `runspec-windows` on the last two. The copy keeps those
dependencies as-is, which is right when your Nexus **group** repository proxies
PyPI: pip resolves `mycorp-linux` from your hosted repo and the cores (and
`runspec` itself) through the proxy.

If your hosts must install *only* internally-named packages, rebrand the cores
too and re-point the dependency with `--rename-dep`:

```bash
runspec rebrand runspec-linux-core==0.7.0   --name mycorp-linux-core   --out dist/
runspec rebrand runspec-webops-core==0.3.0  --name mycorp-webops-core  --out dist/
runspec rebrand runspec-fs-core==0.3.0      --name mycorp-fs-core      --out dist/

runspec rebrand runspec-linux==0.14.0 --name mycorp-linux --out dist/ \
    --rename-dep runspec-linux-core=mycorp-linux-core \
    --rename-dep runspec-webops-core=mycorp-webops-core \
    --rename-dep runspec-fs-core=mycorp-fs-core
```

The version specifier is kept (`runspec-linux-core>=0.7.0` becomes
`mycorp-linux-core>=0.7.0`), so the rebranded cores must keep upstream versions
too. A `--rename-dep` the package doesn't declare is reported and ignored, so
one shared set of flags works for both `runspec-linux` and `runspec-windows`.
`runspec` itself is normally left alone — it's the library everything targets.

## 3. Keep it current automatically

Since a rebrand is deterministic, a scheduled CI job can mirror every new
upstream release. This one is plain shell (shown as GitLab CI; it drops into
Jenkins or GitHub Actions unchanged): for each pair it finds the latest upstream
wheel, skips it if your hosted repo already has that version, otherwise
rebrands and uploads. Re-running it is harmless, so a daily schedule is enough.

```yaml
# .gitlab-ci.yml
mirror-runspec:
  image: python:3.12              # or your registry's mirror of it
  rules:
    - if: $CI_PIPELINE_SOURCE == "schedule"   # the daily run
    - if: $CI_PIPELINE_SOURCE == "web"        # "Run pipeline" by hand
  variables:
    # Everything installs/downloads through Nexus: the group repo fronts both
    # the PyPI proxy (runspec, runspec-linux, …) and your hosted packages.
    PIP_INDEX_URL: https://nexus.mycorp.example/repository/pypi-group/simple
    # The hosted repo alone — to check what you've already published.
    HOSTED_INDEX: https://nexus.mycorp.example/repository/pypi-internal/simple
    TWINE_REPOSITORY_URL: https://nexus.mycorp.example/repository/pypi-internal/
    # TWINE_USERNAME / TWINE_PASSWORD: protected + masked CI/CD variables.
  script:
    - pip install twine "runspec>=0.59.0"
    - |
      set -euo pipefail
      for pair in runspec-linux:mycorp-linux runspec-windows:mycorp-windows; do
        up=${pair%%:*}; ours=${pair##*:}
        rm -rf up probe && mkdir up probe
        pip download --no-deps --only-binary=:all: -d up "$up"
        whl=$(ls up/*.whl)
        ver=$(basename "$whl" | cut -d- -f2)
        if pip download --no-deps --index-url "$HOSTED_INDEX" -d probe "$ours==$ver" >/dev/null 2>&1; then
          echo "$ours $ver already published"; continue
        fi
        runspec rebrand "$whl" --name "$ours" --out dist/
      done
      if ls dist/*.whl >/dev/null 2>&1; then
        twine upload dist/*
      else
        echo "nothing new"
      fi
  artifacts:
    paths: [dist/]
    expire_in: 1 week
    when: always
```

Set it up under **Build → Pipeline schedules**: target the default branch and
pick a daily time. Protected variables are only exposed to pipelines on
protected refs, so the schedule's target branch must be **protected** (the
default branch usually is) or `twine` gets no credentials. If the hosted repo
needs auth to read, put credentials in `HOSTED_INDEX`
(`https://$USER:$PASS@…`) or a `.netrc`.

Add `--rename-dep` flags (and the core packages to the loop, **before** the
packages that depend on them) if you rebrand the cores. Pin `pair` versions
instead of taking the latest if you want to promote releases deliberately.

---

## White-label consoles

A [white-label console](white-label.md) can carry your rebranded Windows pack
instead of the upstream one, and keep it updated from its own Marketplace.

**Depend on the copy, not the `[windows]` extra.** `runspec-console` ships the
Windows runnables as an optional extra, `runspec-console[windows]` (0.292.0+),
so a wrapper that leaves it out gets no upstream copy. `[all]` doesn't include
it either. Depend on your copy instead, with a lower bound only so the
Marketplace can move it forward independently of the console:

```toml
[project]
name = "mycorp-console"
dependencies = [
    "runspec-console[all]>=0.292.0",
    "mycorp-windows>=0.10.0 ; sys_platform == 'win32'",
]
```

(Or leave it out and let operators install it from the Marketplace.)

Don't rebrand `runspec-console` itself: the console looks itself up by
distribution name (discovery self-exclusion, config-seed detection, the update
check), so brand it with a wrapper.

**Point the Marketplace at your index only.** Ship this in the console's
[config seed](white-label.md) so every install offers your copies and never
the public packages:

```toml
[marketplace]
auto_pip = false          # don't derive repos from pip config (pypi.org by default)

[[marketplace.repos]]
name     = "nexus"
url      = "https://nexus.mycorp.example/repository/pypi-group/simple"
scan     = false          # read exactly the packages below
packages = ["mycorp-windows", "mycorp-linux"]
pip_arg  = "index-url"    # installs resolve through this index
```

Use the **group** URL, not the hosted repo alone: installing `mycorp-windows`
also pulls `runspec`, `runspec-webops-core` and `runspec-fs-core` (or your
rebranded cores), which the group serves through its PyPI proxy. The installed
and update chips match by distribution name, so `mycorp-windows` shows as
installed on every venv that has it and offers each new version your mirror
job publishes. If you keep `auto_pip` on, add
`hide = ["runspec-linux", "runspec-windows"]` so operators aren't offered both
names.

---

## Things to know

**Install one or the other, never both, in a venv.** The copy and the
upstream package own the same files (`runspec_linux/…`) and the same commands.
pip will install both without complaint, but uninstalling either removes the
shared files, and two versions overwrite each other. Choose one name per venv.

**What stays upstream-branded.** The long description (README), summary and
project URLs are carried over verbatim — they still say *runspec-linux* and
link to the runspec docs, which is accurate: it's the same code.
