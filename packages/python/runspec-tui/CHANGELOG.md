# runspec-tui Changelog

## [0.2.0] — 2026-11-09

### Added

- **Schedules screen (F3, or `runspec-tui --schedules`)** for the venv's
  `runspec-scheduler`, shown when it is installed: every schedule with its
  state (ok / failing / being handled / dealt with / paused / offered / won't
  run), runnable, when, next fire and last run, a strip with the backend,
  daemon, keeper, venv zone and package-schedules policy, and a detail pane
  with the description, package words, recent runs and last output. Re-reads
  `status` every 10 s on a worker thread.
- Acting on schedules: I'm on it / Dealt with / Release (`runspec history
  claim|resolve|release --label sched:<id>`), Run now (`run-now --detach`),
  turn on/off (`enable`/`disable`, so a package schedule keeps following its
  package), change the time, remove (a package schedule goes back to its
  package's version), sync crontab, restart the daemon, settings (venv zone,
  package policy, daemon time limits) and switching backend. Writes run as the
  venv owner (`sudo -n -u <owner>`, values on stdin via the env frame) and are
  followed by a crontab sync on the cron backend.
- **Add a schedule from a form:** Add (a) switches the forms to schedule mode;
  Ctrl+R validates the form, keeps only changed values, refuses `password`
  fields, then asks for the id and when it runs.
- Needs `runspec-scheduler` 0.10.0 for package schedules, the venv zone and
  `enable`/`disable`.

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
