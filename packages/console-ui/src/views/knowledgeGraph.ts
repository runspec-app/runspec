// Pure helpers for the Knowledge tab's entity graph: a small deterministic
// force-directed layout (no graph library — the wiki graph is at most a few
// hundred nodes), node sizing, and the per-type colour + label set.

export interface LayoutNode { id: string; weight: number }
export interface LayoutEdge { source: string; target: string; weight: number }
export interface Point { x: number; y: number }

/** Mid-tone colours readable on both the dark and light surfaces. */
export const TYPE_COLORS: Record<string, string> = {
  host: '#3b82f6',
  group: '#6366f1',
  profile: '#8b5cf6',
  runnable: '#10b981',
  file: '#f59e0b',
  term: '#ec4899',
  app: '#14b8a6',
  service: '#06b6d4',
  team: '#f97316',
  topic: '#94a3b8',
}

export const typeColor = (type: string): string => TYPE_COLORS[type] ?? TYPE_COLORS.topic

/** Node radius from its weight (confirmations of the facts about it). */
export function nodeRadius(weight: number): number {
  return Math.round(5 + Math.min(14, Math.sqrt(Math.max(1, weight)) * 2.2))
}

// Deterministic pseudo-random in [0,1) from a string, so a layout is stable
// between refreshes (the same wiki draws the same picture).
function hash01(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return ((h >>> 0) % 100000) / 100000
}

/**
 * Fruchterman–Reingold layout into a `width`×`height` box. Connected nodes pull
 * together (heavier edges pull harder), every pair repels, a weak gravity keeps
 * disconnected islands on screen. Deterministic for the same input.
 */
export function forceLayout(
  nodes: LayoutNode[],
  edges: LayoutEdge[],
  width = 900,
  height = 600,
  iterations = 260,
): Map<string, Point> {
  const pos = new Map<string, Point>()
  const n = nodes.length
  if (n === 0) return pos
  const cx = width / 2
  const cy = height / 2
  nodes.forEach((node, i) => {
    const a = (i / n) * Math.PI * 2 + hash01(node.id) * 0.5
    const r = Math.min(width, height) * (0.2 + hash01(node.id + '#') * 0.25)
    pos.set(node.id, { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r })
  })
  if (n === 1) {
    pos.set(nodes[0].id, { x: cx, y: cy })
    return pos
  }
  const k = Math.sqrt((width * height) / n) * 0.75
  const ids = nodes.map(nd => nd.id)
  const links = edges.filter(e => pos.has(e.source) && pos.has(e.target) && e.source !== e.target)
  let temp = Math.min(width, height) / 8
  const cool = temp / (iterations + 1)
  for (let it = 0; it < iterations; it++) {
    const disp = new Map<string, Point>(ids.map(id => [id, { x: 0, y: 0 }]))
    for (let i = 0; i < n; i++) {
      const pi = pos.get(ids[i])!
      const di = disp.get(ids[i])!
      for (let j = i + 1; j < n; j++) {
        const pj = pos.get(ids[j])!
        let dx = pi.x - pj.x
        let dy = pi.y - pj.y
        let d2 = dx * dx + dy * dy
        if (d2 < 0.01) { dx = 0.1; dy = 0.1; d2 = 0.02 }
        const d = Math.sqrt(d2)
        const f = (k * k) / d
        const fx = (dx / d) * f
        const fy = (dy / d) * f
        di.x += fx; di.y += fy
        const dj = disp.get(ids[j])!
        dj.x -= fx; dj.y -= fy
      }
    }
    for (const e of links) {
      const ps = pos.get(e.source)!
      const pt = pos.get(e.target)!
      const dx = ps.x - pt.x
      const dy = ps.y - pt.y
      const d = Math.max(0.1, Math.sqrt(dx * dx + dy * dy))
      const f = ((d * d) / k) * (1 + Math.log1p(Math.max(0, e.weight)) * 0.25)
      const fx = (dx / d) * f
      const fy = (dy / d) * f
      const ds = disp.get(e.source)!
      const dt = disp.get(e.target)!
      ds.x -= fx; ds.y -= fy
      dt.x += fx; dt.y += fy
    }
    for (const id of ids) {
      const p = pos.get(id)!
      const d = disp.get(id)!
      d.x += (cx - p.x) * 0.02 * k / 10
      d.y += (cy - p.y) * 0.02 * k / 10
      const len = Math.max(0.01, Math.sqrt(d.x * d.x + d.y * d.y))
      const step = Math.min(len, temp)
      p.x = Math.min(width - 20, Math.max(20, p.x + (d.x / len) * step))
      p.y = Math.min(height - 20, Math.max(20, p.y + (d.y / len) * step))
    }
    temp = Math.max(0.5, temp - cool)
  }
  return pos
}

/** Ids of `id` and its direct neighbours (for hover/selection highlighting). */
export function neighbourhood(id: string, edges: LayoutEdge[]): Set<string> {
  const out = new Set<string>([id])
  for (const e of edges) {
    if (e.source === id) out.add(e.target)
    else if (e.target === id) out.add(e.source)
  }
  return out
}

/** `"host:web-01"` → `["host", "web-01"]` (a bare name is a topic). */
export function splitEntity(ref: string): [string, string] {
  const i = ref.indexOf(':')
  return i > 0 ? [ref.slice(0, i), ref.slice(i + 1)] : ['topic', ref]
}

/** Parse a comma/newline-separated entity list from the fact editor. */
export function parseAbout(text: string): string[] {
  return [...new Set(text.split(/[,\n]/).map(s => s.trim()).filter(Boolean))]
}
