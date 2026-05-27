# runspec-console Backlog

Items deferred from active development sessions. Checked into the repo so they survive across machines and sessions.

---

## Deferred (user-requested, needs own session)

### Docs bundled at release
Copy runspec docs (spec, how-to guide) into the `runspec-console` package at release time.
Add an in-app link to view them — for air-gapped / corporate networks where GitHub Pages is blocked.
- Trigger: part of the release workflow (GitHub Actions)
- In-app: a "Help" view or drawer that renders the bundled docs

### Logging required
Make `[config.logging]` mandatory in `runspec.toml` rather than optional.
Auditability is a requirement — every runnable should produce an audit trail.
- Update the spec enforcement in the Python parser (error if `[config.logging]` is absent)
- Update SPEC.md and the inference rules table
- Consider a `runspec check` warning vs hard error for migration period

---

## Console UI open items

### Form field range validation
`ArgDef.range` is defined and passed through discovery but not applied to the `InputNumber` widget in `FormsView.tsx`.
- `handleSubmit` in `RunModal` should check `arg.range[0] <= value <= arg.range[1]` for int/float args
- `InputNumber` `min`/`max` props should also be set for inline feedback

### Always-visible host live-status strip
A compact strip (not in the nav sidebar) showing all hosts with live connected/disconnected dots.
- Discussed: compact row in the header or above the command bar
- Should not require navigating to the Hosts view

### In-app help / how-to view
A "Help" tab or drawer with a getting-started guide — Chainlit-style.
- Covers: adding a runspec.toml, running locally, connecting a remote host, writing your first runnable
- Falls back to bundled docs once the "docs bundled at release" item above is done
- Candidate location: `packages/console-ui/src/views/HelpView.tsx`

### Git sync for jump_hosts.toml
Allow the Jump Hosts list to be pulled from a git repo URL (central team config).
- Teams maintain one `jump_hosts.toml` in a private repo; the console pulls and merges on startup
- Settings tab: "Sync source" URL + "Sync now" button

---

## Commit pending
All 0.19.0 arg-source provenance work (Python + UI) plus SSH key rotation and
`generate-ssh-key` runnable are implemented but not yet committed on `feat/runspec-console`.
