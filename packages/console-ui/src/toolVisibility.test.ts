import { describe, it, expect } from 'vitest'
import { runnableHidden } from './toolVisibility'

describe('runnableHidden', () => {
  it('matches a name glob', () => {
    expect(runnableHidden('outlook-unread', 'm365', 'local', ['outlook-*'])).toBe(true)
    expect(runnableHidden('system-info', 'sys', 'local', ['outlook-*'])).toBe(false)
  })

  it('matches group: and host: qualifiers', () => {
    expect(runnableHidden('x', 'm365', 'h', ['group:m365'])).toBe(true)
    expect(runnableHidden('x', 'sys', 'h', ['group:m365'])).toBe(false)
    expect(runnableHidden('x', 'g', 'prod-1', ['host:prod-*'])).toBe(true)
    expect(runnableHidden('x', 'g', 'dev-1', ['host:prod-*'])).toBe(false)
  })

  it('hides nothing for empty/blank patterns', () => {
    expect(runnableHidden('outlook-unread', 'm365', 'local', [])).toBe(false)
    expect(runnableHidden('outlook-unread', 'm365', 'local', null)).toBe(false)
    expect(runnableHidden('outlook-unread', 'm365', 'local', ['   '])).toBe(false)
  })

  it('escapes regex metacharacters in the literal part', () => {
    expect(runnableHidden('a.b', undefined, undefined, ['a.b'])).toBe(true)
    expect(runnableHidden('axb', undefined, undefined, ['a.b'])).toBe(false)
  })
})
