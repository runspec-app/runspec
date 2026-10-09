---
type: "Feature"
title: "agent"
description: "The console agent runs runnables through search_runnables, describe_runnable and run_runnable (or one tool per runnable outside search mode)."
tags: [runspec, feature]
status: stable
generated:
  by: runspec-console-seed/f019ddb84fa7
  at: "2026-10-09"
---
# agent

## What it is

The console agent runs runnables through search_runnables, describe_runnable and run_runnable (or one tool per runnable outside search mode). In search mode, built-in tools that are not always loaded are listed in a menu and used via describe_tool and use_tool.

## How to

To fetch a log whose exact path is unknown, use list_files on the host first (or a runnable that reports where the app logs), take the path from its output, then pass that path to download_file or relay_file; never guess a path.

Tool output can feed the next call: read a value from one runnable's or tool's result (a path, an id, a host name) and pass it as an argument to the next. Long output is saved to the chat workspace, where workspace_grep, workspace_json_get or workspace_tail pull out just the value needed.

## Notes

Built-in file tools: list_files browses a directory on the local machine or any host, download_file copies a file from a host, and relay_file copies between hosts, optionally through a file filter.

Each chat has a workspace folder for files the agent makes or receives: workspace_ls, workspace_read, workspace_head, workspace_tail, workspace_grep, workspace_json_get, workspace_write, workspace_materialize and workspace_info. run_python_sandboxed and run_sql_sandboxed run code in a WASM sandbox that can only see the workspace.

On Windows with Outlook, the agent has Outlook tools: outlook_inbox, outlook_search, outlook_get_message, outlook_calendar, outlook_reminders, outlook_resolve_recipient and outlook_event_join_url read; outlook_draft, outlook_send, outlook_save_attachment, outlook_move, outlook_mark_read, outlook_categorize, outlook_create_event and outlook_respond_invite need approval.

Repos (Settings → Repos) are git checkouts the agent may read and edit locally: repo_list, repo_sync, repo_status, repo_read, repo_grep, repo_diff and repo_write. It never commits or pushes.

Agent action tools: create_document (markdown to a file or host), create_schedule, emit_event, open_url, open_file, reveal_in_folder, play_sound, run_runbook and run_saved_run. fetch_url reads a web page; console_status reports the hosts, the current time and what is running.

Wiki tools, also offered over the MCP server: search_wiki, wiki_page, wiki_graph, list_wiki_inbox and propose_fact run without approval; review_wiki_fact, edit_wiki_fact and retire_wiki_fact need approval; pin_wiki_fact pins a fact into every chat.

search_marketplace finds runnable packages that could be installed but are not; the agent can only recommend them, installing stays an operator action in the Marketplace tab.

## See also

- [runspec-console](/runspec/apps/runspec-console.md)
- [files](/runspec/features/files.md)
- [knowledge](/runspec/features/knowledge.md)
- [marketplace](/runspec/features/marketplace.md)
- [outlook](/runspec/features/outlook.md)
- [repos](/runspec/features/repos.md)
- [workspace](/runspec/features/workspace.md)
