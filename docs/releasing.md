# Releasing

How a version becomes a published release in this monorepo. Read this before
cutting any release — the tagging is **automated**, but publishing is **manual**,
and the two are decoupled on purpose.

## The model in one sentence

Bump a package's version on `main` → the **auto-tag** workflow writes the matching
git tag for history → you **manually dispatch** that package's release workflow on
the tag to actually publish.

## Why publishing is manual

`.github/workflows/auto-tag.yml` ("the tag history writer") runs on every push to
`main`. When a watched package's `version` field changes, it creates the matching
`{prefix}v{version}` tag pointing at the merge commit, so every published version
has a corresponding tag.

It pushes tags with the default `GITHUB_TOKEN`. **By GitHub design, a
`GITHUB_TOKEN` tag push does not trigger other workflows** — so the `*-release.yml`
workflows do **not** fire automatically. auto-tag only backfills history; it never
publishes. Publishing is always an explicit step.

> Do **not** hand-create the release tag. Let the merge land, let auto-tag mint the
> tag, then dispatch the release on it. Creating the tag yourself races auto-tag
> (it'll `tag exists, skip`) and, if pushed with a PAT, can trigger a release
> before you intend.

## Package matrix

| Package | Manifest auto-tag watches | Tag prefix | Release workflow | Publishes to |
|---|---|---|---|---|
| `runspec` (core) | `packages/python/runspec/pyproject.toml` | `v` | `release.yml` | PyPI + GitHub Release |
| `runspec-linux` | `packages/python/runspec-linux/pyproject.toml` | `linux-v` | `linux-release.yml` | PyPI + GitHub Release |
| `runspec-console` | `packages/python/runspec-console/pyproject.toml` | `console-v` | `console-release.yml` | PyPI + GitHub Release |
| `runspec-node` | `packages/node/package.json` | `node-v` | `node-release.yml` | npm + GitHub Release |
| `runspec-chat` | `packages/python/runspec-chat/pyproject.toml` | `chat-v` | `chat-release.yml` | PyPI (see caveat) |
| `registry` | `packages/registry/pyproject.toml` | `registry-v` | `registry-release.yml` | PyPI (archived) |

## Standard release flow

Using `runspec-console` `0.4.0` as the worked example. Same shape for every package —
swap the manifest, tag prefix, and version.

1. **Bump the version** in the package's manifest (the file in the table above), and
   add a `CHANGELOG.md` entry. This is the only change auto-tag keys on — the
   version field must actually differ from the previous value on `main`.
   - For Python packages the published version comes from `pyproject.toml`
     (`hatchling`), not from any `__version__` in source.
2. **Merge to `main`** (normal PR). The branch's final state must contain the bumped
   version.
3. **auto-tag fires** on the push to `main`, sees the changed version, and creates
   e.g. `console-v0.4.0` on the merge commit. (History only — nothing publishes yet.)
4. **Publish via Actions → the package's release workflow → "Run workflow"
   (`workflow_dispatch`)**, with the tag as input, e.g. `console-v0.4.0`.
   - `workflow_dispatch` skips the "tag is on main" guard, and the auto-created tag
     is already on the merge commit, so it just works.
   - The release workflow rebuilds from the tagged commit (for console: builds the
     `console-ui` Vite bundle into the wheel, builds the wheel, smoke-tests it, then
     publishes), so there's no stale-artifact risk.

Order matters: **merge first, then dispatch.** Dispatching before the merge/auto-tag
means the tag doesn't exist yet and the run fails.

## Caveats

- **`runspec-chat` and `registry` have no `workflow_dispatch`** — they only trigger
  on a real tag push. Since auto-tag's `GITHUB_TOKEN` push won't trigger them, a
  release for these needs the tag pushed manually with a PAT (a token that can
  re-trigger workflows). In practice both are inactive: `runspec-chat` is superseded
  by `runspec-console`, and `registry` is archived. Prefer adding a
  `workflow_dispatch` trigger to the workflow over PAT-pushing tags if either is
  revived.
- **The release tag must point at a commit on `main`.** The tag-push path of each
  release workflow enforces this (`git merge-base --is-ancestor`). auto-tag always
  tags the merge commit, so this holds for the standard flow.
- **Re-releasing a version doesn't work** — PyPI/npm reject duplicate versions, and
  auto-tag skips a tag that already exists. Always bump to a fresh version.

## For automated/agent sessions

If you're an agent preparing a release: your job ends at **step 1** (bump + changelog
on the feature branch). Steps 2–4 (merge, then `workflow_dispatch`) are the human's
call. Never push a `{prefix}v*` tag yourself.
