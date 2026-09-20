# runspec-console — pre-publish testing

The goal: never ship a launch-breaking bug in a central flow (add host, copy SSH
key) with thousands of tests green. The gap that let one through was the
**Python ⇄ TypeScript boundary** — the frontend mocked the bridge (so the real
serializer never ran) and the Python tests never fed the shapes the editor emits
through `save → write file → reload`. Each side was green in isolation; the seam
was untested.

## One command before publish

```bash
python scripts/test_pack.py            # whole pack, aggregated, non-zero on any failure
python scripts/test_pack.py --list     # show every step, run nothing
python scripts/test_pack.py --package runspec-console -v
```

Per Python package it runs `ruff check` / `ruff format --check` / `mypy` (only
the packages CI gates — **runspec-console is intentionally not mypy-checked**,
it carries documented pre-existing debt) / `pytest`; for the frontends
`npm run typecheck` + `npm test` (+ `npm run build` for the Node pack); and the
docs-drift guard. Every step runs to completion so one pass shows the full
picture.

**Gotcha — install extras first.** The pack runs the tools already installed in
the current environment. Console tests import optional extras and will error at
collection if they're missing. Install per package before running:

```bash
pip install -e ".[dev]"     # in each packages/python/<pkg>
# plus, for runspec-console, the extras its tests import, as available:
pip install -e "packages/python/runspec-console[anthropic,credentials,git-sync,schedule,otel,mcp,sandbox,voice]"
pip install jsonschema      # the docs toml-examples check needs it
```

**Gate: do not publish unless the pack is fully green.**

## The boundary-test rule (keep this closed)

Any config the UI writes **must** have a Python test that round-trips it through
the *real* serializer/loader (no mocks), and every config file read at launch
**must** survive a corrupt file without raising. See
`tests/test_config_resilience.py` and `tests/test_save_jump_hosts_robust.py`.

- **One audited serializer.** `runspec_console/tomlsafe.py` is the single TOML
  escaping + quarantine implementation (`basic_string` / `dump_scalar` /
  `quarantine_corrupt` / `load_table_array`). Never hand-roll an
  `f'name = "{v}"'` writer — it emits invalid TOML for any value with a
  `"`/`\`/newline, and a Python `repr` (`None`, `[None]`) for a null. Route
  every collection file's read through `load_table_array` (quarantine-and-continue)
  and every value through `dump_scalar`.
- **Startup resilience.** A malformed config file is moved aside to a timestamped
  `.corrupt-*` sidecar (data preserved) and the loader returns `[]`, so the next
  save writes a clean file and the app still starts.

## Manual smoke checklist (desktop/OS-only, ~15 min)

These can't run headlessly — run on the Windows box before publish. Keep this
list current as flows change.

- [ ] **Fresh install (no config):** app launches; Settings opens; Mail tab
      renders; Jump Hosts — add a host **with** and **without** a path row; Save;
      **restart the app** and confirm it relaunches.
- [ ] **Corrupt-config recovery:** hand-corrupt `runspec_hosts.toml` (and, now
      hardened too, `runspec_filters.toml` / `runspec_credentials.toml` /
      `runspec_groups.toml` / `runspec_profiles.toml` / `runspec_repos.toml` /
      `runspec_self_service.toml`); relaunch; confirm the app starts and a
      `.corrupt-*` quarantine sidecar was created for each.
- [ ] **Copy SSH key:** to one host and "to all" (needs a reachable test host +
      password). Real paramiko + OS-keyfile integration — not coverable
      headlessly; if it fails, capture the `runspec-console.log` line.
- [ ] Anything else central: room/helpdesk join, tray/notifications, first-run
      flows.

## Follow-ups (not yet done)

- **`windows-latest` CI job** for the console package, so the Windows
  test-hygiene gaps (POSIX-only tests running under git-bash, `\` vs `/`
  assertions) can't silently regress. See the handoff §8 triage.
- **Opt-in real-host SSH integration** (`-m realssh`) running `copy_ssh_id` /
  `run_remote` against a localhost sshd, so the paramiko path gets real coverage
  without a desktop.
