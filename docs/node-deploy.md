# Deploying a Node runnable folder

A `runspec-node` runnable set deploys as a **self-contained folder** — no Python
on the box, no global install. The folder *is* the unit: its own `runspec.toml`,
runnable scripts, and `node_modules`, shaped into a venv-like layout by
[`runspec bin`](cli.md). A controller (the [Desktop Console](console.md)) drives
it over SSH exactly like a Python venv — it can't tell the difference.

!!! note "“Venv-shaped” means"
    A folder with `bin/runspec` + one `bin/<runnable>` per runnable + a `logs/`
    directory — mirroring a Python venv's `bin/` + `logs/`. The console derives
    the group name and log directory from the `bin/runspec` path the same way it
    does for Python, so both kinds of host are managed identically.

## Prerequisites

- **Remote:** Linux with **Node 18+**.
- **SSH access** from your console machine to the remote (the key the console
  already uses for its hosts).

## 1. Put the folder on the remote

### Option A — the published `runspec-webops` (fastest)

A ready-made set of HTTPS/TLS runnables (`cert-check`, `cert-chain`,
`http-check`, `tls-info`). The package source *is* the deployable folder:

```bash
cd /opt/runnables                  # wherever you keep tools
npm pack runspec-webops            # downloads runspec-webops-<ver>.tgz
tar xzf runspec-webops-*.tgz       # -> ./package/
mv package webops && cd webops

npm install                        # pulls runspec-node (+ deps)
npx runspec bin                    # writes bin/runspec, bin/<runnable>, logs/
```

### Option B — your own runnables

```bash
mkdir mytools && cd mytools
npm init -y
npm install runspec-node
npx runspec init --name greet      # scaffolds runspec.toml + a code stub
```

Declare your runnables in `runspec.toml` (turn on per-invocation logging so a
shared host stays multi-writer safe):

```toml
[config]
autonomy-default = "autonomous"

[config.logging]
store = "per-run"

[greet]
description = "Greet someone"

[greet.args]
name = {type = "str", description = "Who to greet", default = "world"}
```

Point each runnable at its script via `package.json` `bin` (or just name the file
`greet.js` next to `runspec.toml`), then generate the shims:

```bash
npx runspec bin
```

!!! tip "Re-run `runspec bin`"
    Run it again after `npm install`, or after adding/renaming a runnable — it
    (re)generates the `bin/` shims from `runspec.toml` + `package.json`.

Either option leaves a venv-shaped folder:

```
/opt/runnables/webops/
  bin/runspec   bin/cert-check  …      ← what the console invokes
  runspec.toml  package.json
  node_modules/                        ← incl. runspec-node
  logs/                                ← per-invocation logs land here
```

## 2. Smoke-test on the remote

```bash
./bin/runspec local                    # lists the runnables
./bin/cert-check example.com           # run one (or ./bin/greet --name Ada)
./bin/runspec logs status              # per-runnable file + disk inventory
./bin/runspec logs cert-check          # merged stream — includes the runnable's own output
```

These work from any directory: the shims resolve their own location, and
`runspec` finds `runspec.toml` relative to the binary (not the current
directory), which is what lets the console drive them over SSH.

## 3. Register the host with the console

Add the remote to the console's `runspec_hosts.toml`, pointing `runspec_paths`
at the folder's **`bin/runspec`** (you can also do this from the console's host
UI):

```ini
[[host]]
name          = "tools-1"
ssh           = "deploy@your-remote"
runspec_paths = ["/opt/runnables/webops/bin/runspec"]
group         = "Tools"
# identityFile = "/path/to/key"        # if not your default SSH key
```

The console then, over SSH:

- **discovers** runnables via `…/bin/runspec local --format json`,
- **invokes** them as `…/bin/<runnable>`,
- drives the **Logs tab** with `…/bin/runspec logs …`, and **History** reads
  `…/logs/` — including each runnable's captured `console.log` output.

The host shows up grouped as `webops` (the folder name), with its runnables
listed like any Python venv's.

## Managing logs

In `store = "per-run"` mode each invocation writes its own
`{runnable}.{ts}.{run_id}.log`, so retention is an operator action — the same
`runspec logs` verbs you used to smoke-test also compact and prune:

```bash
./bin/runspec logs compact --older-than 7d --gzip
./bin/runspec logs prune   --older-than 90d
```

Schedule those from cron or a systemd timer (see
[Logging → Per-invocation files](logging.md#per-invocation-files-shared-venvs)).

!!! note "Cross-platform"
    `runspec bin` also writes a Windows `.cmd` per runnable, so the *same*
    folder runs on Windows as well as Linux/macOS. On a Linux remote you only
    use the POSIX shims; the `.cmd` files are harmless.

## Gotchas

- **Generate `bin/` on the remote.** The shims are created by `runspec bin`
  *after* `npm install`, because they reference the installed `runspec-node`.
- **Relocatable, per-machine.** The shims resolve their own directory, so moving
  the folder is fine — but `node_modules` is platform-specific, so run
  `npm install` (and `runspec bin`) on the host where it will execute.
- **One folder = one toolset.** Keep separate toolsets in separate folders
  (separate `runspec_paths` entries / groups), the same way you'd use separate
  Python venvs rather than one shared environment.
