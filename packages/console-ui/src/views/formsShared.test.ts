import { describe, it, expect } from 'vitest'
import { exampleText } from './formsShared'

describe('exampleText()', () => {
  it('marks a hint as an example unless it already is one', () => {
    expect(exampleText('lowercase-with-dashes')).toBe('e.g. lowercase-with-dashes')
    expect(exampleText('e.g. web-01')).toBe('e.g. web-01')
    expect(exampleText('egress-proxy')).toBe('e.g. egress-proxy')
    expect(exampleText(undefined)).toBeUndefined()
  })
})
