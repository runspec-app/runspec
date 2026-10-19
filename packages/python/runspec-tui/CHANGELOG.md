# runspec-tui Changelog

## [0.1.0] — 2026-10-19

### Added

- **First release: the runspec-console Forms view in a terminal.** `runspec-tui`
  lists the runnables installed in its venv (grouped by package, searchable,
  with autonomy levels; `--all` adds `discoverable = false` ones) and builds a
  typed form for each from `runspec.toml` with core's inference: switches,
  choices, multi-choice checklists, one-per-line `multiple` args, path/folder
  fields with Tab completion and a Browse picker, `ui` date/datetime/time/month/
  colour hints, masked `password` args, command picking with Inherited / This
  command sections, and defaulted args folded into **Default arguments**. A
  field supplied by `.runspec_env` or an env var says so and may be left blank.
- **Validation before running** — per-field type/range/pattern checks via
  `runspec.types.coerce` and group rules via `runspec.validator`.
- **Runs through `runspec exec`'s code** (`runspec.runexec.exec_runnable`,
  label `tui`): recorded in the runs index / logs inbox, `run_as` applied with
  the env frame on stdin, secrets as `RUNSPEC_<RUNNABLE>_ARG_<NAME>` env vars,
  stdin `/dev/null`. Output streams live with the exit code and time; JSON
  output is pretty-printed; **Stop** (Ctrl+T) terminates the run; a **Debug**
  switch sets `RUNSPEC_<RUNNABLE>_ARG_DEBUG=1`.
- Needs `runspec` 0.75.0.
