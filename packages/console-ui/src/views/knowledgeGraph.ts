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
  trigger: '#ef4444',
  schedule: '#eab308',
  runbook: '#84cc16',
  feature: '#a855f7',
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
 * Fruchterman–Reingold layout shaped to a `width`×`height` canvas. Connected
 * nodes pull together (heavier edges pull harder), every pair repels, and
 * gravity keeps disconnected islands next to the main cluster. Positions are
 * not clamped to the box — draw them through {@link fitView}. Deterministic for
 * the same input.
 */
export function forceLayout(
  nodes: LayoutNode[],
  edges: LayoutEdge[],
  width = 900,
  height = 600,
  iterations = 260,
  gravity = 0.2,
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
  // Gravity pulls everything toward the centre so disconnected islands sit near
  // the main cluster instead of being flung to the edges; it's weaker along the
  // longer side so the drawing takes the canvas's shape.
  const gx = (gravity * k / 10) * Math.min(1, height / width)
  const gy = (gravity * k / 10) * Math.min(1, width / height)
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
      d.x += (cx - p.x) * gx
      d.y += (cy - p.y) * gy
      const len = Math.max(0.01, Math.sqrt(d.x * d.x + d.y * d.y))
      const step = Math.min(len, temp)
      p.x += (d.x / len) * step
      p.y += (d.y / len) * step
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

/** A pan/zoom transform: screen = graph * k + (x, y). */
export interface View { x: number; y: number; k: number }

export const MIN_ZOOM = 0.2
export const MAX_ZOOM = 5

const clampZoom = (k: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, k))

/**
 * The transform that fits every position (padded by `pad` for node + label)
 * into a `width`×`height` viewport, centred. Never zooms in past 1.6× so a
 * two-node wiki doesn't fill the screen with two giant circles.
 */
export function fitView(pos: Map<string, Point>, width: number, height: number, pad = 48): View {
  if (pos.size === 0 || width <= 0 || height <= 0) return { x: 0, y: 0, k: 1 }
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
  for (const p of pos.values()) {
    x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y)
    x1 = Math.max(x1, p.x); y1 = Math.max(y1, p.y)
  }
  const bw = Math.max(1, x1 - x0 + pad * 2)
  const bh = Math.max(1, y1 - y0 + pad * 2)
  const k = clampZoom(Math.min(width / bw, height / bh, 1.6))
  return { k, x: width / 2 - ((x0 + x1) / 2) * k, y: height / 2 - ((y0 + y1) / 2) * k }
}

/** Zoom by `factor` keeping the screen point (`sx`, `sy`) fixed. */
export function zoomAt(view: View, factor: number, sx: number, sy: number): View {
  const k = clampZoom(view.k * factor)
  const f = k / view.k
  return { k, x: sx - (sx - view.x) * f, y: sy - (sy - view.y) * f }
}
