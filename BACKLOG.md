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

