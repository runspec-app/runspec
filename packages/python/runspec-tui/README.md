# runspec-tui

The runspec-console **Forms** view, in a terminal. Install it into a venv that
has [runspec](https://pypi.org/project/runspec/) runnables, run `runspec-tui`,
and you get a searchable list of those runnables and a typed form for each one.
Fill the form in, press **Ctrl+R**, and the output streams in below it.

It's for the places the desktop console can't go: an SSH session on a server,
a jump host, a container, a terminal on a locked-down machine.

```
pip install runspec-tui      # into the venv that holds your runnables
runspec-tui
```

## What you get

- **The list** — every runnable installed in the venv, grouped by package, with
  its autonomy level. Type `/` to search names, descriptions and command names.
- **The form** — one field per arg, built from `runspec.toml` with core's own
  inference, so it asks for exactly what `parse()` will accept:
  - a switch for a flag, a dropdown for a choice, a checklist for a
    `multiple` choice, one-per-line for other `multiple` args;
  - path and folder fields with Tab completion and a **Browse** picker (F2);
  - `ui = "date" / "datetime" / "time" / "month" / "color"` hints;
  - `password` args masked and never shown again;
  - commands to pick from, with **Inherited** and **This command** sections;
  - args that have a default folded into **Default arguments**;
  - a field that `.runspec_env` or an env var already supplies says
    *from …* and can be left blank.
- **Validation before running** — required fields, types, ranges, patterns and
  group rules (`exactly-one`, …) are checked as you'd get them from `parse()`,
  shown under each field.
- **Streaming output** with the exit code and time, JSON output pretty-printed,
  and a **Debug** switch that turns on `--debug` for the run.

## Keys

| Key | Does |
|---|---|
| `/` | Search runnables |
| `Enter` | Open the highlighted runnable or command |
| `Tab` / `Shift+Tab` | Next / previous field |
| `Ctrl+R` | Run |
| `Ctrl+T` | Stop the run |
| `F2` | Browse for a file or folder |
| `Ctrl+O` | Show or hide the output |
| `Ctrl+Y` | Copy the output |
| `Esc` | Back: out of search, then up a command |
| `Ctrl+P` | Command palette (themes, quit, …) |
| `Ctrl+Q` | Quit |

`runspec-tui --all` also lists runnables marked `discoverable = false`.
`--theme NAME` (or `RUNSPEC_TUI_THEME`) picks a Textual theme.

## How runs work

Each run goes through the same code as `runspec exec`, so it is **recorded**: a
row in the venv's runs index (or a logs-inbox file when you aren't the venv
owner) with the exit code and the tail of the output, labelled `tui`. The
label shows as `label=tui` in `runspec history` and in the Label column of the
runspec-console History tab (runspec 0.75.1 / runspec-console 0.352.0 and
later). To list only these runs:

```
runspec history list --label tui
```

- **Secrets never reach the command line.** A `password` field is passed as
  `RUNSPEC_<RUNNABLE>_ARG_<NAME>` in the environment, never on argv.
- **`run_as` is honoured.** When a runnable runs as another account (by default
  the venv owner), the run escalates with `sudo -n` the same way `runspec exec`
  does, and the values travel on stdin, not argv.
- **Only what you changed is sent.** A field left at its default isn't passed,
  so `.runspec_env` and env vars still apply.
- **The run can't read your keyboard.** Its stdin is `/dev/null`; a runnable
  that prompts gets end-of-file.

## Requirements

Python 3.10+, `runspec` 0.75.0+, and a terminal (it exits with a pointer to
`runspec exec` when stdin or stdout isn't one). It isn't a runnable itself, so
it doesn't appear in `runspec local` or `runspec serve`.

Docs: <https://runspec.app/tui/>
