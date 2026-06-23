# Credentials: name-derived environment variables (0.114.0)

> Status: shipped. Supersedes the form-side arg-fill behaviour from 0.100.0 /
> 0.101.1 / 0.102.1 (those still describe the **agent** path).

## Why

The old model copied a saved credential's values straight into a runnable's
arguments on **both** the form and the agent path — bound by `username_arg`
(→ argv) and `secret_arg` (→ the `RUNSPEC_*_ARG_*` env channel), auto-applied by
host/runnable scope globs. For a human filling a form this was surprising: a
required field could be silently satisfied from secure storage, and the only
runnables it fit were ones whose author had declared matching args. It did
nothing for the common case of a runnable that reads bare environment variables
(e.g. `runspec-jsm` reading `JSM_USER`/`JSM_PASSWORD`).

We split the two paths:

- **Forms (humans)** never copy a credential into args. Credentials are passed
  only as **plain, name-derived environment variables**. A required arg — secret
  or not — must be typed by the operator. This keeps "the ability to require
  human input" and pushes credential reuse onto an explicit, visible channel.
- **Agents** keep the arg-binding fill (so a required arg, including a
  `password`, can be satisfied from a credential) **and** additionally get the
  same name-derived env vars.

## The convention

A credential is injected as env vars whose names derive from its **label** and
**kind** by a strict convention:

| label | kind | env vars |
|---|---|---|
| `Jira` | `userpass` | `JIRA_USERNAME`, `JIRA_PASSWORD` |
| `Jira Dev` | `userpass` | `JIRA_DEV_USERNAME`, `JIRA_DEV_PASSWORD` |
| `DB` | `password` | `DB_PASSWORD` |
| `Deploy` | `token` | `DEPLOY_TOKEN` |
| `Config repo` | `ssh-key` | *(none — key-file based)* |

Prefix derivation (`credentials.env_prefix`): uppercase the label, replace every
run of non-alphanumeric characters with a single `_`, strip leading/trailing
`_`. An empty prefix (e.g. label `!!!`) emits nothing.

The operator **names a credential to match what the runnable reads** from the
environment. The console can't introspect a runnable's env reads (they're plain
`os.environ` lookups), so the run form and the Settings editor both **preview**
the derived names to make the contract obvious.

## Code map

- `credentials.env_prefix` / `env_field_map` / `env_vars_for` / `env_var_names`
  — pure derivation (no keyring access; the secret is passed in).
- `bridge._credential_env(host, runnable, ids)` — resolves the applied
  credentials (explicit `ids` from the form's multi-select, or the in-scope set
  when `ids is None` for the agent), reads secrets from the keyring, returns
  `(env, applied_ids)`.
- `executor.run_local` / `run_remote` — take `cred_env` and merge it into the
  child environment alongside the `RUNSPEC_*_ARG_*` secret channel, including
  through `env(1)` so it survives `sudo`/`su` on the `run_as` path.
- Frontend: `FormsView.credEnvNames` mirrors the derivation for previews; the run
  dialog's multi-select; per-runnable prefill of the chosen credential **ids**
  (localStorage, never secrets); `SettingsDrawer` live preview while editing.

## sudo / run_as

Most `run_as` runnables take no credential at all — they assume passwordless
sudo. Credentials that *are* attached ride `env(1)` through the escalation, the
same as the secret channel. The one sudo runnable with a secret arg,
`enable-passwordless-sudo`, is a one-time bootstrap where typing the password in
the form is appropriate.

## Known gap

The strict convention emits `JSM_USERNAME`/`JSM_PASSWORD`/`JSM_TOKEN`, but the
shipped `runspec-jsm` reads `JSM_USER`/`JSM_PAT` and `runspec-snow` reads
`SNOW_USER`/`SNOW_TOKEN`. A conventionally-named credential won't fully wire
those packs until their env reads accept the conventional names too — tracked as
a follow-up, out of scope here.
