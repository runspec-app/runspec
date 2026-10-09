---
type: "Feature"
title: "hosts"
description: "Hosts are configured in Settings → Jump Hosts (runspec_hosts.toml)."
tags: [runspec, feature]
status: stable
generated:
  by: runspec-console-seed/85b104e63b3b
  at: "2026-10-09"
---
# hosts

## How to

Settings → Jump Hosts → Provision venv… creates a runspec venv on one or many hosts (location, Python interpreter, run-as owner, packages) and shares its logs inbox; Deprovision… removes one after a typed confirmation, keeping logs by default.

## Notes

Hosts are configured in Settings → Jump Hosts (runspec_hosts.toml). Groups target a runnable at every host in the group at once; profiles are a switchable working set of hosts the sidebar loads. The agent can see hosts with console_status and edit them with manage_hosts and manage_groups.

## See also

- [runspec-console](/runspec/apps/runspec-console.md)
- [venv](/runspec/topics/venv.md)
