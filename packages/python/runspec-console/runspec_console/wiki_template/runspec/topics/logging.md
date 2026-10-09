---
type: "Topic"
title: "logging"
description: "Runnables log through the standard logger (logging.getLogger(__name__) in Python); logging and the run audit record are on by default and land in the venv's logs/ folder."
tags: [runspec, topic]
status: stable
sources:
  - id: S1
    resource: "https://runspec.app/logging/"
  - id: S2
    resource: "https://runspec.app/audit/"
generated:
  by: runspec-console-seed/35aba7e34884
  at: "2026-10-09"
---
# logging

## Notes

Runnables log through the standard logger (logging.getLogger(__name__) in Python); logging and the run audit record are on by default and land in the venv's logs/ folder. Call runspec.audit(event, outcome, target=...) for actions that must be provable later. See https://runspec.app/logging/ and https://runspec.app/audit/

## See also

- [runspec](/runspec/apps/runspec.md)
- [audit](/runspec/topics/audit.md)
