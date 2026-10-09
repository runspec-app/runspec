---
type: "Feature"
title: "config sync"
description: "Config Sync pulls the console's config files (hosts, triggers, runbooks, groups and more) from a git repo at a tag, merging without overwriting local entries; a package can also ship a config seed."
tags: [runspec, feature]
status: stable
generated:
  by: runspec-console-seed/eaaf2992ed82
  at: "2026-10-09"
---
# config sync

## Notes

Config Sync pulls the console's config files (hosts, triggers, runbooks, groups and more) from a git repo at a tag, merging without overwriting local entries; a package can also ship a config seed. It only pulls; it never writes back.

## See also

- [runspec-console](/runspec/apps/runspec-console.md)
