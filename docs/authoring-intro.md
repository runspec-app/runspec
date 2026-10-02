# Adding your own runnables

The console runs any installed **runnable** — a script or tool whose
interface is declared in a [`runspec.toml`](format.md). Install a package
that ships one into the console's environment (or a host's venv) and it
appears in Forms and in the agent's toolset, typed arguments, validation,
autonomy gate and all.

The published packs (`runspec-linux`, `runspec-windows`, `runspec-jsm`,
`runspec-snow`, `runspec-logops`, …) get you a long way, but the real value
is wrapping **your own** scripts and internal APIs. Two paths:

---

## Path A — draft it with the console agent

The agent can write the code for a new runnable; you do the install. The
honest division of labour:

1. **Give the agent somewhere to write.** Add your dev repository under
   **Settings → Repos** (the agent's read/edit allowlist — edits are local
   working-tree only, never commits).
2. **Ask for the runnable:**

    > In the tools repo, draft a runspec runnable called `rotate-report`
    > that archives yesterday's report file and regenerates it. Write the
    > `runspec.toml`, the Python module using `runspec.parse()`, and the
    > `pyproject.toml` entry point. Make the destructive step
    > `autonomy = "confirm"`.

    The agent writes the files with its `repo_write` tool (each write
    confirm-gated, diffs visible).

3. **You install it** — the agent cannot pip-install into its own
   environment or restart the app:

    ```
    pip install -e C:\path\to\tools-repo
    ```

    …into the console's venv for a local runnable, or onto a host venv (the
    console's **Marketplace** tab installs published packages there — see
    [bootstrapping](console-hosts.md#bootstrapping-a-fresh-host)).

4. **Relaunch the console** (local installs) or let the next refresh pick it
   up (remote venvs). It's now in Forms and the agent's tools:

    > Describe the rotate-report runnable and do a dry run.

5. **Iterate in place** — the `-e` editable install means further agent edits
   to the code apply without reinstalling; only `runspec.toml` interface
   changes need a refresh to re-read.

## Path B — scaffold it yourself

`runspec init` scaffolds a complete working project — `runspec.toml`, code
stub, `pyproject.toml` with the entry point wired:

```
pip install runspec
mkdir mytool && cd mytool
runspec init --name mytool --write-project
pip install -e .
mytool --help
```

The [quickstart](quickstart.md) walks this end-to-end in five minutes.

---

## What makes a good console runnable

- **Declare autonomy honestly** — `autonomous` for read-only checks,
  `confirm` (the default) for anything that changes state, `manual` for
  things an agent should never touch. This is what the console's safety
  gates enforce.
- **Emit JSON** (`output = "json"`, ideally a top-level array of flat
  objects) — the console renders it as tables, and the agent parses it
  reliably.
- **Add descriptions** to the runnable and each argument — they become the
  agent's tool documentation and the Forms tab's labels. The agent picks
  tools by these descriptions, so write them for a reader who can't see the
  code.
- **Read secrets from the environment** using names a
  [credential](console-forms.md#credentials) can derive (`MYTOOL_TOKEN`, …) —
  never take a password as a plain argument.

From here, the authoring reference covers everything: the
[authoring guide](authoring.md) end-to-end, the complete
[format reference](format.md), [logging](logging.md),
[testing](testing.md), and [typed arguments](typed-args.md).
