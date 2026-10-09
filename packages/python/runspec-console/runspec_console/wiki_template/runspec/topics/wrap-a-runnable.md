---
type: "Topic"
title: "wrap a runnable"
description: "To build a company-specific runnable on a published one, depend on its -core package (for example runspec-linux-core or runspec-jira-core), import the helpers, and bake your defaults into your own pa…"
tags: [runspec, topic]
status: stable
sources:
  - id: S1
    resource: "https://runspec.app/white-label-runnables/"
generated:
  by: runspec-console-seed/fa8abfda5a0e
  at: "2026-10-09"
---
# wrap a runnable

## How to

To build a company-specific runnable on a published one, depend on its -core package (for example runspec-linux-core or runspec-jira-core), import the helpers, and bake your defaults into your own package with its own runspec.toml; only your runnables then surface. runspec rebrand republishes a package under your own name. See https://runspec.app/white-label-runnables/

## See also

- [runspec](/runspec/apps/runspec.md)
