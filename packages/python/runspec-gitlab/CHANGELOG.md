# Changelog

All notable changes to `runspec-gitlab` are documented here.

## [0.3.0] — 2026-10-10

### Added

- **15 write verbs**, all `autonomy = "confirm"` (the 17 read verbs stay
  `autonomous`):
  - issues: `create-issue`, `update-issue` (title/description, add/remove
    labels, assignees, milestone, due date, close/reopen), `add-issue-note`;
  - merge requests: `create-merge-request` (`--draft`, reviewers, squash,
    remove source branch), `update-merge-request`, `add-merge-request-note`,
    `merge-merge-request` (`--sha` guard, `--when-pipeline-succeeds`);
  - repository: `create-branch`, `write-file` (creates or replaces a text file
    in one commit, from `--content` or `--content-file`, optionally on a new
    branch via `--start-branch`);
  - pipelines/jobs: `create-pipeline` (`--variable KEY=VALUE`),
    `retry-pipeline`, `cancel-pipeline`, `retry-job`, `cancel-job`, `play-job`.
- **4 delete verbs**, all `autonomy = "manual"` so an agent can never run
  them (a person can, from the CLI or the console's Forms tab):
  `delete-issue`, `delete-branch`, `delete-file`, `delete-pipeline`.
- Assignees and reviewers are taken as usernames. Writes and deletes need a
  token with the `api` scope.
- Requires `runspec-gitlab-core >= 0.3.0`.

## [0.2.1] — 2026-09-20

### Changed

- **Packaging metadata for the runspec-console Marketplace.** Add a README
  (rendered in the Marketplace card's Details view) and publish `keywords`
  and Documentation / Source / Changelog / Issues project URLs, so the
  package's Marketplace card shows a description, tags and links. No
  runnable, argument, or behaviour changes.

## [0.2.0]

Reconciled the verb set against GitLab's canonical MCP tool registry
(`app/services/mcp/tools/manager.rb` + the `route_setting :mcp` declarations in
the REST route files) and closed the gaps. **17 read verbs** now (all
`autonomy = "autonomous"`).

- **Renamed** `get-merge-request-changes` → `get-merge-request-diffs` — matches
  the canonical `get_merge_request_diffs` tool and its `/diffs` endpoint; the old
  verb used the deprecated `/changes` endpoint. **Breaking** vs 0.1.0.
- **Added** canonical MR read tools: `get-merge-request-commits`,
  `get-merge-request-notes`, `get-merge-request-pipelines`,
  `get-merge-request-conflicts`.
- `get-merge-request-conflicts` is **best-effort** — GitLab has no public
  `/api/v4` conflicts endpoint (its MCP server calls an internal service in
  process), so it relays the non-public web-UI endpoint
  (`/{project}/-/merge_requests/:iid/conflicts`) and returns its JSON verbatim;
  the shape is GitLab's and may change between versions.
- Docs now separate the canonical 1:1 ports from the extra REST reads GitLab has
  no MCP tool for (`whoami`, `get-project`, `search-projects`, `list-issues`,
  `get-file`, `list-tree`).
- Requires `runspec-gitlab-core >= 0.2.0`.

## [0.1.0]

Initial release. One runnable, `gitlab`, with a read verb per tool of the
[GitLab MCP server](https://docs.gitlab.com/user/model_context_protocol/mcp_server/),
**ported onto the GitLab REST API (v4)** rather than proxying that server — so it
runs over the runspec-console SSH fleet with no extra process. `runspec serve`
re-expands each verb into its own leaf MCP tool (`gitlab_get-issue`,
`gitlab_list-pipelines`, …).

- Verbs (all `autonomy = "autonomous"` — read-only): `whoami`, `get-project`,
  `search-projects`, `list-issues`, `get-issue`, `list-merge-requests`,
  `get-merge-request`, `get-merge-request-changes`, `list-pipelines`,
  `get-pipeline-jobs`, `get-job-log`, `get-file`, `list-tree`.
- Global `--env` (dev/uat/prod) selects `GITLAB_<ENV>_*` env vars; auth is
  `GITLAB_URL` (default `https://gitlab.com`) + `GITLAB_TOKEN`. `project` accepts
  a numeric id or `group/project` path and falls back to `GITLAB_PROJECT`.
- Thin wrapper over `runspec-gitlab-core` (the dependency-free REST logic core).
