---
type: "Topic"
title: "runspec.toml"
description: "runspec.toml lives inside the package directory (mypkg/runspec.toml), not the project root."
tags: [runspec, topic]
status: stable
sources:
  - id: S1
    resource: "https://runspec.app/format/"
generated:
  by: runspec-console-seed/8c20094f6fe0
  at: "2026-10-09"
---
# runspec.toml

## What it is

runspec.toml lives inside the package directory (mypkg/runspec.toml), not the project root. Each runnable is a top-level section such as [greeter]; [config] is the only reserved name. Format reference: https://runspec.app/format/

## See also

- [runspec](/runspec/apps/runspec.md)
