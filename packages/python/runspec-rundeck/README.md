# runspec-rundeck

Sync your venv's [runspec](https://runspec.app/) runnables to
[Rundeck](https://www.rundeck.com/) as jobs — a JVM-free replacement for
`rd jobs load` you can drop into a CI publish stage. When your internal runnables
are published and installed into a venv, `rundeck sync` makes every one of them
available on the Rundeck platform, with argument definitions always in sync.

```bash
pip install runspec-rundeck
```

## Verbs

| Command | Autonomy | What it does |
|---|---|---|
| `rundeck sync` | confirm | Discover every runnable in the venv, emit a Rundeck job per leaf command, and import them into a project |
| `rundeck push` | confirm | Import already-emitted `jobs/*.json` files (the `rd jobs load` replacement) |
| `rundeck list` | autonomous | List a project's jobs |

## How sync works

`sync` composes two stable runspec CLI surfaces — it does not reach into runspec
internals:

1. `runspec emit --rundeck --stdout --project <p>` renders one Rundeck job per
   leaf command (subcommands flatten like `runspec serve`), baking a stable
   `uuid5(project/group/job)` into each.
2. `runspec local --format json` supplies each runnable's `discoverable` flag, so
   hidden internal helpers are skipped (pass `--all` to include them).
3. The jobs are imported over the Rundeck REST API with
   `dupeOption=update&uuidOption=preserve`, so a matching job is **updated** and a
   missing one **created** — rename-safe, because identity is the UUID.

`--prune` then deletes jobs in the managed group whose UUID wasn't in the push,
so a runnable removed from the venv has its job cleaned up.

## Auth & environments

Auth is environment-variable first:

```bash
export RUNDECK_URL=https://rundeck.example.com
export RUNDECK_TOKEN=…            # a Rundeck API token
export RUNDECK_PROJECT=ops
rundeck sync
```

Target several instances (dev/uat/prod, or regional labels like `prod-eu`) with
`RUNDECK_<ENV>_*` prefixes selected by `--env`:

```bash
export RUNDECK_PROD_EU_URL=https://rundeck.eu.example.com
export RUNDECK_PROD_EU_TOKEN=…
rundeck sync --env prod-eu --project ops
```

A CI matrix runs one invocation per instance. UUIDs only need to be unique
*within* an instance, so the same emitted jobs push cleanly to every separate
instance.

`--token` (a password arg) supplies the token through the runspec-console secret
channel instead; `--url` / `--project` / `--api-version` / `--ca-bundle` /
`--proxy` / `--insecure` override the environment for private-CA / proxied
instances.

## Per-runnable Rundeck settings

Schedules, notifications, node filters, and the job group come from a
`[<runnable>.meta.rundeck]` table in the runnable's `runspec.toml` (project
defaults in `[config.meta.rundeck]`) — see the
[Rundeck jobs](https://runspec.app/rundeck/) docs. `sync` passes them straight
through the emitter.

Built on [`runspec-rundeck-core`](https://pypi.org/project/runspec-rundeck-core/).
