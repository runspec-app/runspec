// Pure runnable-id ↔ Runnable resolution helpers, split out of FormsView so App
// can call matchRunnableForEdit without importing the (heavy) FormsView module —
// keeping FormsView off the first-paint path (it lazy-loads with the Forms tab).
// FormsView re-exports these for back-compat; the colocated test lives in
// formsMatch.test.ts.

import type { Runnable } from '../bridge'

// Anthropic tool names allow only [a-zA-Z0-9_-] (capped at 64). Mirrors the
// backend bridge_agent_tools._sanitize_tool_name so the console can reconstruct
// a runnable's agent tool id.
export function sanitizeToolName(raw: string): string {
  return raw.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 64)
}

// The search-mode `run_runnable` `id` (and the dedup-mode tool name): the
// sanitised runnable **name**. Mirrors bridge_agent_tools._dedup_tool_name, which
// stopped group-qualifying in 0.254.0 — a runnable's group is the venv/package it
// was discovered in, so qualifying split one cross-platform runnable into a tool
// per venv, each pinned to one host.
export function dedupToolName(r: { group?: string; name: string }): string {
  return sanitizeToolName(r.name)
}

// The legacy group-qualified spelling of that id. No longer emitted, but it is
// still written down in a saved runbook step / trigger allowlist / eval fixture,
// and the backend still routes it (bridge_agent_tools._dedup_tool_aliases), so
// edit-and-rerun must still resolve it.
export function legacyDedupToolName(r: { group?: string; name: string }): string {
  const g = r.group || ''
  return sanitizeToolName(g ? `${g}__${r.name}` : r.name)
}

// Resolve an edit-and-rerun target to a discovered Runnable. `idOrName` may be a
// plain runnable name (human run / History reruns), a search-mode `run_runnable`
// dedup id (`sanitize(group__name)`), or a `host__name` tool id; `host` may be a
// concrete host, a venv/group, or empty. Mirrors the backend's
// `_runnables_for_id` / `_resolve_id_host` so the console resolves an agent tool
// call the same way it does — the previous name-only match silently failed on a
// dedup id, so the pencil did nothing.
export function matchRunnableForEdit(
  runnables: Runnable[], idOrName: string, host: string,
): Runnable | undefined {
  const uniqueHost = (rs: Runnable[]) => (new Set(rs.map(r => r.host)).size === 1 ? rs[0] : undefined)
  // 1. Exact host + name — the human run / History path (a real name + host).
  const exact = runnables.find(r => r.host === host && r.name === idOrName)
  if (exact) return exact
  // 2. A dedup tool id (search mode): match the reconstructed name — or its
  //    legacy group__name spelling — then pin the host: concrete host wins, else
  //    a group match, else a unique host.
  const dedup = runnables.filter(
    r => dedupToolName(r) === idOrName || legacyDedupToolName(r) === idOrName,
  )
  if (dedup.length) {
    return dedup.find(r => r.host === host) ?? dedup.find(r => r.group === host) ?? uniqueHost(dedup)
  }
  // 3. An explicit host__name id (per-host tool name).
  const sep = idOrName.indexOf('__')
  if (sep > 0) {
    const hit = runnables.find(r => r.host === idOrName.slice(0, sep) && r.name === idOrName.slice(sep + 2))
    if (hit) return hit
  }
  // 4. Name-only, where the given `host` is really a host or a venv/group.
  return uniqueHost(runnables.filter(r => r.name === idOrName && (r.host === host || r.group === host)))
}
