---
description: Show the runspec fleet hosts and their connection status
---

Call the `fleet_hosts` MCP tool from the runspec-fleet server and present the
result as a short table: each host, whether it is connected, how many venvs and
tools it exposes, and any error. If a host is disconnected, suggest checking its
SSH reachability (`ssh <host>`) and that `runspec` is installed in the configured
venv.
