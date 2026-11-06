import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import GateMark from './GateMark'

describe('GateMark', () => {
  it('draws the bar and triangle in the given colours, hidden from screen readers', () => {
    const { getByTestId } = render(<GateMark size={24} barColor="#eeeeee" triangleColor="#3cc7b6" />)
    const svg = getByTestId('gate-mark')
    expect(svg.getAttribute('width')).toBe('24')
    expect(svg.getAttribute('aria-hidden')).toBe('true')
    const [bar, triangle] = Array.from(svg.querySelectorAll('path'))
    expect(bar.getAttribute('stroke')).toBe('#eeeeee')
    expect(triangle.getAttribute('fill')).toBe('#3cc7b6')
  })
})
