import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'

import { SettingsColumns } from './SettingsColumns'

describe('SettingsColumns', () => {
  it('renders each child as a column of the shared grid', () => {
    render(<SettingsColumns><div>left</div><div>right</div></SettingsColumns>)
    const grid = screen.getByText('left').parentElement!
    expect(grid.className).toBe('rs-settings-cols')
    expect(grid.children).toHaveLength(2)
    expect(screen.getByText('right').parentElement).toBe(grid)
  })
})
