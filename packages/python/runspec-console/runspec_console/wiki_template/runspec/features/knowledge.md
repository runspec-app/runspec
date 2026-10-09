---
type: "Feature"
title: "knowledge"
description: "The Knowledge tab is the team wiki: facts about hosts, runnables, files and jargon."
tags: [runspec, feature]
status: stable
generated:
  by: runspec-console-seed/afef400e98aa
  at: "2026-10-09"
---
# knowledge

## How to

Advice that holds wherever a runnable is used (which args keep formatting, which verb to start with) belongs in the arg and runnable descriptions in its runspec.toml, where every agent sees it. Advice specific to your team goes in a wiki fact about runnable:<name>, which the agent sees when it searches for or describes that runnable. Keep pins for tips that matter on almost every turn.

## Notes

The Knowledge tab is the team wiki: facts about hosts, runnables, files and jargon. Capture reads run history and config (no AI), Suggest from chats proposes facts for the inbox, up to 6 pinned facts ride every chat, facts about a runnable are shown when the agent searches for or describes it, and Memory Sync shares approved facts through a git repo.

## See also

- [runspec-console](/runspec/apps/runspec-console.md)
- [runspec](/runspec/apps/runspec.md)
- [agent](/runspec/features/agent.md)
