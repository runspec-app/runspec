# Denylist

The denylist is a fast, uniform way to **disable runnables** installed in a
Python virtual environment — a single one, a set, or all of them — scoped by
username and/or hostname, and combinable. It is a security control: a kill
switch you can flip without uninstalling anything or editing each runnable's
`runspec.toml`.

Because every runnable depends on `runspec` and funnels through
`runspec.parse()`, the denylist is enforced in one place and applies uniformly
to **every** invocation path: a direct CLI run, an installed entry point, and an
agent call via `runspec serve`. A disabled runnable returns the same refusal
everywhere.

## How it works

On every `parse()`, runspec reads `runspec_denylist.toml` from the root of the
virtual environment it is installed in (`{sys.prefix}` — the same location as
`.runspec_env`). If a rule matches the runnable being run **and** the current
effective user **and** the hostname, the run is refused:

```
✗  Refusing to run 'deploy': disabled by the runspec denylist.
   Reason: change freeze
   Denylist: /opt/venv/runspec_denylist.toml
   An administrator can re-enable it with `runspec deny clear` (or by editing that file).
```

When no file is present (the default), nothing is disabled.

## Managing it with `runspec deny`

`runspec deny` edits the denylist for the venv it runs in. It is part of the
core `runspec` binary — **not** a runnable — so it can never disable itself: the
re-enable path always works, even on a host where everything else is disabled.

```bash
runspec deny add --runnable deploy                    # disable one runnable (all users/hosts)
runspec deny add -r deploy -u svc-batch -H 'prod-*'   # disable deploy for svc-batch on prod hosts
runspec deny all --reason "change freeze"             # disable every runnable in this venv
runspec deny list                                     # show the current mode + rules
runspec deny mode warn                                # warn instead of refusing (enforce|warn|off)
runspec deny clear                                    # remove the denylist — re-enable everything
```

`--runnable`, `--user`, and `--host` accept shell-style globs (`*`) and may be
repeated. An omitted dimension means "all". `runspec deny list --json` emits the
denylist as structured JSON (`{path, present, mode, disabled, reason, rules}`) —
what the console's status table reads.

## File format

The file is a separate format from `runspec.toml`:

```toml
[denylist]
mode = "enforce"          # "enforce" (default) | "warn" | "off"
# disabled = true         # fast path: deny everything

[[denylist.rule]]
runnables = ["deploy"]    # omitted ⇒ ["*"] (all runnables)
users = ["svc-batch"]     # omitted ⇒ ["*"] (all users)
hosts = ["prod-*"]        # omitted ⇒ ["*"] (all hosts)
reason = "change freeze"  # surfaced in the refusal message
```

A run is denied when **any** rule matches. Multiple rules OR together; within a
rule, the runnable name, username, and hostname dimensions AND together.

- **`mode`** is authoritative and lives in this file — there is no per-runnable
  opt-out, so a runnable author cannot exempt themselves from an operator's
  denylist. `warn` logs to stderr and continues; `off` disables enforcement
  without deleting the rules.
- The **effective** user (post-`sudo`/`run_as` identity the runnable actually
  runs as) is matched, alongside `socket.gethostname()`.
- `--help` still works for a disabled runnable, and introspection
  (`runspec local`, `runspec stubs`) is never blocked.

Because the file carries hostname rules, one canonical denylist can be
distributed across many hosts and still target only the right ones (e.g. disable
`deploy` on `prod-*` while leaving `dev-*` alone).

## Why it cannot lock you out

The `runspec` binary itself is exempt from the denylist, so `runspec deny clear`,
`runspec serve`, and `runspec local` keep working even under a deny-everything
rule. To re-enable on a remote host, run `runspec deny clear` over SSH (or delete
`{sys.prefix}/runspec_denylist.toml`).

## From the desktop console

The [desktop console](console-quickstart.md) drives the same `runspec deny` across your
fleet over the SSH connections it already holds — one call per venv on each host
— in two ways:

- **Settings → Denylist** — a tab with a host picker (empty ⇒ all hosts),
  **Disable all** / **Re-enable all (clear)** buttons (danger-confirmed), an
  *add specific rule* form (runnable / user / host globs + reason), and an
  enforcement-mode toggle (`enforce`/`warn`/`off`). The aggregated result is
  shown inline.
- **The agent tool `manage_denylist`** — ask the agent something like *"disable
  all runnables on prod-\* until further notice"* or *"re-enable everything on
  prod-2"* and approve the confirm prompt.

- `targets` selects which fleet hosts to act on; omit it to hit **all** hosts.
- `add` takes the same `runnable` / `user` / `host` globs and `reason` as the
  CLI; `mode` takes `enforce|warn|off`; `clear` re-enables everything.
- The tool is **confirm-gated** (like file transfers), so a fleet-wide disable
  is one approval away — and one approval back.

Because the tool shells out to the core `runspec` binary (which is exempt from
its own denylist), it disables the *runnables* on those hosts, never the console
itself, and `clear` can always re-enable them. Remote hosts need
`runspec >= 0.43.0` for the tool to take effect.

## Testing

Point the denylist at an arbitrary path with the `RUNSPEC_DENYLIST_FILE`
environment variable (mirrors `RUNSPEC_ENV_FILE`):

```bash
RUNSPEC_DENYLIST_FILE=/tmp/deny.toml runspec deny all
RUNSPEC_DENYLIST_FILE=/tmp/deny.toml deploy   # refused
```

---

**Next:** [Console quickstart](console-quickstart.md) — the runspec-console desktop app.
