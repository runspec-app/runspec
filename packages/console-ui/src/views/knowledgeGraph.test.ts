import { describe, it, expect } from 'vitest'
import { forceLayout, neighbourhood, nodeRadius, parseAbout, splitEntity, typeColor, TYPE_COLORS } from './knowledgeGraph'

describe('knowledgeGraph', () => {
  const nodes = [
    { id: 'host:a', weight: 4 }, { id: 'host:b', weight: 1 },
    { id: 'runnable:x', weight: 9 }, { id: 'term:island', weight: 1 },
  ]
  const edges = [
    { source: 'host:a', target: 'runnable:x', weight: 5 },
    { source: 'host:b', target: 'runnable:x', weight: 1 },
  ]

  it('lays every node out inside the box, deterministically', () => {
    const a = forceLayout(nodes, edges, 400, 300)
    const b = forceLayout(nodes, edges, 400, 300)
    expect(a.size).toBe(4)
    for (const [id, p] of a) {
      expect(p.x).toBeGreaterThanOrEqual(20)
      expect(p.x).toBeLessThanOrEqual(380)
      expect(p.y).toBeGreaterThanOrEqual(20)
      expect(p.y).toBeLessThanOrEqual(280)
      expect(b.get(id)).toEqual(p)
    }
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
