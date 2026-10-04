import { describe, it, expect } from 'vitest'
import { fitView, forceLayout, neighbourhood, nodeRadius, parseAbout, splitEntity, typeColor, zoomAt, TYPE_COLORS } from './knowledgeGraph'

describe('knowledgeGraph', () => {
  const nodes = [
    { id: 'host:a', weight: 4 }, { id: 'host:b', weight: 1 },
    { id: 'runnable:x', weight: 9 }, { id: 'term:island', weight: 1 },
  ]
  const edges = [
    { source: 'host:a', target: 'runnable:x', weight: 5 },
    { source: 'host:b', target: 'runnable:x', weight: 1 },
  ]

  it('lays out deterministically and fits the result into the viewport', () => {
    const a = forceLayout(nodes, edges, 400, 300)
    const b = forceLayout(nodes, edges, 400, 300)
    expect(a.size).toBe(4)
    for (const [id, p] of a) expect(b.get(id)).toEqual(p)
    const v = fitView(a, 400, 300, 10)
    for (const p of a.values()) {
      const sx = p.x * v.k + v.x
      const sy = p.y * v.k + v.y
      expect(sx).toBeGreaterThanOrEqual(0); expect(sx).toBeLessThanOrEqual(400)
      expect(sy).toBeGreaterThanOrEqual(0); expect(sy).toBeLessThanOrEqual(300)
    }
  })

  it('keeps disconnected islands near the cluster and follows the canvas shape', () => {
    const star = [{ id: 'h', weight: 30 }, ...Array.from({ length: 14 }, (_, i) => ({ id: `r${i}`, weight: 5 }))]
    const isl = Array.from({ length: 4 }, (_, i) => ({ id: `iso${i}`, weight: 1 }))
    const links = star.slice(1).map(n => ({ source: 'h', target: n.id, weight: 3 }))
    const p = forceLayout([...star, ...isl], links, 1400, 560)
    const span = (ids: string[]) => {
      const xs = ids.map(i => p.get(i)!.x), ys = ids.map(i => p.get(i)!.y)
      return [Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys)]
    }
    const [sw, sh] = span(star.map(n => n.id))
    const [aw, ah] = span([...p.keys()])
    expect((sw * sh) / (aw * ah)).toBeGreaterThan(0.5) // the cluster isn't a speck among outliers
    expect(aw).toBeGreaterThan(ah) // wide canvas, wide drawing
  })

  it('zooms about a fixed screen point and clamps', () => {
    const v = zoomAt({ x: 10, y: 20, k: 1 }, 2, 100, 100)
    expect(v.k).toBe(2)
    // the graph point under (100,100) stays under it
    expect((100 - 10) / 1).toBeCloseTo((100 - v.x) / v.k)
    expect(zoomAt({ x: 0, y: 0, k: 1 }, 1000, 0, 0).k).toBe(5)
    expect(fitView(new Map(), 100, 100)).toEqual({ x: 0, y: 0, k: 1 })
  })

  it('pulls linked nodes closer than unlinked ones', () => {
    const p = forceLayout(nodes, edges, 600, 600)
    const d = (x: string, y: string) => Math.hypot(p.get(x)!.x - p.get(y)!.x, p.get(x)!.y - p.get(y)!.y)
    expect(d('host:a', 'runnable:x')).toBeLessThan(d('host:a', 'term:island'))
  })

  it('handles empty and single-node graphs', () => {
    expect(forceLayout([], []).size).toBe(0)
    expect(forceLayout([{ id: 'x', weight: 1 }], [], 100, 100).get('x')).toEqual({ x: 50, y: 50 })
  })

  it('finds neighbourhoods, sizes, colours and parses entity refs', () => {
    expect([...neighbourhood('runnable:x', edges)].sort()).toEqual(['host:a', 'host:b', 'runnable:x'])
    expect(nodeRadius(100)).toBeGreaterThan(nodeRadius(1))
    expect(typeColor('host')).toBe(TYPE_COLORS.host)
    expect(typeColor('weird')).toBe(TYPE_COLORS.topic)
    expect(splitEntity('file:/var/log/a:b')).toEqual(['file', '/var/log/a:b'])
    expect(splitEntity('bare')).toEqual(['topic', 'bare'])
    expect(parseAbout('host:a, host:a\nterm:b,')).toEqual(['host:a', 'term:b'])
  })
})
