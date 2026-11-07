# Installed-code integrity

Every run checks that the runnable's installed code is still what was installed.
If someone edited a file inside the venv (a quick fix on the box that never made
it back to the repo), the run still goes ahead, but it is **recorded as
modified**: a warning on stderr, a `modified` badge on the run in the console's
History, and an audit event that also goes to the OS log.

Nothing to set up. It is on for every runnable from runspec 0.81.0, and for
Node runnables from runspec-node 0.47.0 ([below](#node-runnables)).

## What gets checked

When a package is installed, pip writes a `RECORD` file listing every file it
installed with its SHA-256 hash. On each run, `runspec.parse()` hashes those
files again and compares:

- the package that ships the runnable's `runspec.toml`, and
- each package it depends on directly, which for a typical runnable is its
  `-core` package and runspec itself.

That covers the Python code, the `runspec.toml` (so an edited `autonomy` or a
removed `password` type is caught too) and the console scripts. It adds about
10 ms to a run: `runspec-linux` and its dependencies are about 200 files.

It doesn't check:

- **Byte-code** (`__pycache__/*.pyc`). pip records it without a hash.
- **New files** dropped into a package folder. An edited file that imports one
  is still caught.
- Packages further down the dependency tree (`httpx`, `requests`, …).
  `runspec verify --all` checks those on demand.

## Development installs are left alone

How a package was installed is recorded next to it (`direct_url.json`, PEP 610),
and the check uses that:

| Installed with | Checked | Shown in History |
|---|---|---|
| `pip install mypkg` from PyPI or a private index | yes | nothing, unless modified |
| `pip install -e .` (editable) | **no** | nothing |
| `pip install .` from a folder | yes | `local build` |
| `pip install ./mypkg-1.0-py3-none-any.whl` | yes | `local build` |
| `pip install git+https://…` | yes | `local build` |

An editable install points at your working tree, so you can edit and run as
much as you like. A spec that isn't part of an installed package at all (a
script next to its `runspec.toml`, a project checkout) isn't checked either.

`local build` isn't a warning, just a record. It says the code didn't come from a
package index, so it may never have been committed or published. That's the
other half of the "edited on the box" problem: code built from someone's
uncommitted checkout.

## When a file has changed

The run carries on. On stderr:

```
⚠  which: 2 installed files differ from the package (runspec-linux 0.20.0, runspec-linux-core 0.14.1): runspec_linux/files.py, runspec_linux_core/__init__.py. This run is recorded as modified. Reinstall the package to restore it; "runspec verify" lists the changes.
```

And it is recorded:

- **The run record** (`run_summary`) and the runs index carry
  `"integrity": "modified"` and the changed files, so History shows a red
  `modified` badge on that run, with the files in its tooltip.
- **An audit event**, `runnable.modified` (outcome `detected`), naming the
  package, its version and the changed files. It goes to the venv's
  hash-chained `logs/audit.jsonl` and to syslog / the Windows event log, so the
  person who edited the file can't quietly remove the record once your log
  shipping has picked it up. It is written once per distinct change, so a
  runnable that runs every minute doesn't repeat it; a further edit is a new
  event.

To put it right, reinstall the package:

```bash
pip install --force-reinstall --no-deps runspec-linux==0.20.0
```

## `runspec verify`

Check on demand, for example from a monitoring job or before a change window:

```bash
runspec verify                  # every runnable package in this venv + what it depends on directly
runspec verify runspec-linux    # one package (or the package that ships a runnable: runspec verify which)
runspec verify --all            # every package in the venv
runspec verify --json           # for scripts
```

```
  runspec              0.80.0  index     ok            90 files
  runspec-linux        0.20.0  index     modified      1 of 73 files changed
      changed  runspec_linux/files.py
  runspec-linux-core   0.14.1  index     ok            24 files

✗  1 distribution(s) modified since install. Reinstall to restore: pip install --force-reinstall --no-deps <name>==<version>
```

It exits `1` when anything is modified and records nothing.

## What it can and can't promise

This is **tamper-evident, not tamper-proof**. Anyone who can edit a file in the
venv can also edit pip's `RECORD`, edit runspec itself, or change the runnable so
it never calls `parse()`. Module code also runs when it's imported, before
`parse()` is reached. The check catches the habit of fixing things in place and
forgetting to commit them. It won't stop someone who sets out to hide a change.

If the real worry is a person reading secrets by editing a runnable, the fix is
who owns the code, not a checksum:

- Install the venv as an account the operators can't become, and run tools as
  another account that can read but not write it (`run_as`). The
  `runspec-linux-root` tools already work this way: install them into a
  root-owned venv so only root can change code that root runs.
- Keep secrets out of files on the box: pass them through the console's
  credentials, which reach the runnable at run time and are never written to
  disk or a command line.

## Switching it off

`RUNSPEC_INTEGRITY=off` skips the check, for test suites or a host where a known
local change is accepted for now. There is no `runspec.toml` setting: a package
can't switch off its own check.

## Node runnables

The Node pack (`runspec-node` 0.47.0) runs the same check, with one difference:
npm doesn't record a hash for each installed file, so runspec has to get the
known-good hashes from somewhere else.

**What's checked:** the project folder (the one with `runspec.toml` and
`package.json`, as [deployed](node-deploy.md)) and each of its direct
`dependencies` in `node_modules`, `runspec-node` included.

**Where the hashes come from**, in order:

1. **`runspec-files.json` in the package.** A publisher writes it with
   `runspec manifest`, which hashes every file `npm pack` would publish. Add it
   as the last step of `prepack`, after the build:

    ```json
    "scripts": {
      "build": "tsc",
      "prepack": "npm run build && runspec manifest"
    }
    ```

    If `package.json` has a `files` list, add `runspec-files.json` to it.
    `runspec-node` ships its own list from 0.47.0.

2. **A snapshot from the first run.** A package without a list is hashed the
   first time a runnable runs after it's installed, and the hashes are kept in
   `logs/.integrity-baseline.json`. Later runs compare against that. An edit
   made *before* that first run becomes part of the snapshot and is never
   reported, so publish with a list where you can. After reinstalling a package
   without a list, `runspec verify --snapshot` takes the snapshot again.

For the project folder the snapshot leaves out `logs/`, `bin/` (the shims
`runspec bin` writes), `package-lock.json`, dot-files such as `.runspec_env`,
and `node_modules`.

**Development is left alone.** Nothing is checked when the project folder is in
a git checkout (it has a `.git` folder, or a folder above it does). A dependency
installed as a symlink (`npm link`, `npm install ../my-lib`) is skipped. One
installed from a `.tgz` file or from git is recorded as `local build`, the same
as in Python.

**When a file has changed**, the run goes ahead with the same stderr warning,
and the run record carries `integrity: "modified"` and the changed files when
`[config.logging]` is on, so the console's History shows the badge. There's no
audit event yet: the Node pack doesn't have `audit()`.

```bash
npx runspec verify              # the project + its direct dependencies
npx runspec verify runspec-node # one package (a runnable name means the project)
npx runspec verify --all        # every package in node_modules
npx runspec verify --snapshot   # take the snapshot again for packages without a list
```

```
  mytools       1.0.0    index    list      modified      1 of 3 files changed
      changed  greet.js
  runspec-node  0.47.0   index    list      ok            146 files

✗  1 package(s) modified since install. Reinstall to restore them (npm install, or unpack the package again).
```

The third column says where the hashes came from: `list`, `snapshot`, or
`none` (no list, and no run has taken a snapshot yet).
