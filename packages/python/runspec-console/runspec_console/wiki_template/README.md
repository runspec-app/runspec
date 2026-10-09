# Team wiki

This repo is the team's shared knowledge, kept as plain Markdown pages in the
[Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format)
(OKF v0.2). Any OKF reader, any Markdown viewer and runspec-console can read it.

It started from the runspec-console wiki template.

## Layout

| Folder | What goes there | `type` |
|---|---|---|
| `runnables/` | one page per runnable | `Runnable` |
| `services/` | systems and applications you look after | `Service` |
| `hosts/` | servers and server groups | `Host`, `Host Group` |
| `runbooks/` | step-by-step procedures | `Playbook` |
| `terms/` | team jargon | `Term` |
| `topics/` | anything else | `Topic` |
| `teams/` | who owns what | `Team` |
| `runspec/` | what runspec and runspec-console do (generated, don't edit) | various |
| `facts/` | Memory Sync's shared facts, one file per person (written by the console) | n/a |
| `tools/` | the lint and site scripts CI runs | n/a |

`index.md` in each folder lists its pages. `log.md` records what changed, newest
first.

## A page

One file per thing, named after it. The frontmatter says what it is; `type` is
the only required field.

```markdown
---
type: Runnable
title: cron
description: Manage a user's crontab on a Linux host.
tags: [linux, scheduling]
---

# cron

What it's for, in a sentence or two.

## How we use it

- We always pass `--run-as svc-batch` on the batch hosts.

## Watch out for

- `clear` removes every line, including ones the console didn't add.
```

Optional fields worth using:

- `sources`: where a statement came from (a doc URL, a ticket, a chat).
- `status`: `draft`, `stable` or `deprecated`.
- `stale_after`: a date after which the page should be checked again. The lint
  job warns once it passes.
- `generated` / `verified`: who wrote the page and who checked it. A page the
  console's agent drafted carries `generated`; add yourself under `verified`
  (`by: human:<you>`) when you've checked it.

Link to other pages from the bundle root: `[cron](/runnables/cron.md)`.

Never put secrets on a page: no passwords, tokens or keys. The lint job fails
when it sees something that looks like one.

## Changing the wiki

1. Work on a branch of your own (the console uses `contrib/<your-name>`).
2. Open a merge request into `main`. Someone else reviews it.
3. CI runs `tools/okf_lint.py` on every merge request.

Check a change locally with `python tools/okf_lint.py` (no install needed).

## Releases

`main` is the working copy. Tag a release when a batch of changes is ready,
for example `v2026.10.09`. The site is published from release tags, so readers
see a reviewed snapshot rather than whatever merged last.

## The `runspec/` folder

These pages are generated from the documentation summaries that ship with
runspec-console. Don't edit them by hand: a refresh would overwrite the change.
To bring them up to date with a newer console, use **Settings → Memory Sync →
Refresh runspec pages** in runspec-console. It pushes the regenerated pages to a
`refresh/runspec-docs` branch for you to review and merge like any other change.

Put your own notes about runspec in `topics/` and link to the generated page.

## CI

Both CI setups are included; delete the one you don't use.

- GitLab: `.gitlab-ci.yml` lints merge requests and publishes GitLab Pages from
  release tags.
- GitHub: `.github/workflows/wiki.yml` lints pull requests and publishes GitHub
  Pages from release tags (turn on Pages with source "GitHub Actions").

The site build installs `mkdocs-material` from your package index. On a network
without internet access, point pip at your internal mirror (for example with a
`PIP_INDEX_URL` CI variable).

Preview the site locally:

```bash
pip install "mkdocs<2" mkdocs-material
python tools/build_site.py --serve
```
