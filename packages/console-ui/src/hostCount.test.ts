import { describe, it, expect } from 'vitest'
import { readHostsLabel } from './hostCount'

describe('readHostsLabel()', () => {
  it('names the set it counts', () => {
    expect(readHostsLabel(1)).toBe('local only')
    expect(readHostsLabel(3)).toBe('local + 2 connected')
  })
})
