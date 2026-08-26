---
description: Re-discover runspec runnables across all fleet hosts now
---

Call the `fleet_refresh` MCP tool from the runspec-mcp server to re-discover the
runnables installed across every configured host, then briefly report how many
tools are now available and whether the set changed.

(Discovery is already automatic — the fleet gateway re-scans on a cadence and the
tool list updates on its own. Use this only to force an immediate refresh, e.g.
right after installing a new runnable on a host.)
