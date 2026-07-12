/**
 * Component tests for the structured-table row cap in OutputPanel.
 *
 * A runnable returning a large JSON array-of-objects (one compact line, so the
 * streaming path doesn't fire) parses into a table that used to mount a cell per
 * column per row all at once — tens of thousands of cells for a 10k-row result,
 * hanging the card after the stream completed. The table now renders a bounded
 * initial slice and reveals the rest in chunks on demand. These drive the real
 * render via StructuredOutput and assert the cap + incremental expansion.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { StructuredOutput } from './OutputPanel'

vi.mock('../bridge', () => ({ bridge: new Proxy({}, { get: () => vi.fn() }) }))

// A JSON array of `n` objects, each a distinct scalar row (rendered as one <tr>).
function bigTable(n: number): string {
  return JSON.stringify(Array.from({ length: n }, (_, i) => ({ idx: i, host: `box-${i}` })))
}

const dataRows = () => screen.getAllByRole('row').filter(r => r.querySelector('td'))

describe('JsonTable row cap', () => {
  it('caps the initial render and shows a "show more" affordance', () => {
    render(<StructuredOutput text={bigTable(5000)} />)
    // Only the first 200 (TABLE_ROW_CAP) data rows mount, not all 5,000.
    expect(dataRows()).toHaveLength(200)
    expect(screen.getByText(/showing 200 of 5,000 rows/)).toBeTruthy()
    // First row present, a far-down row is not yet rendered.
    expect(screen.getByText('box-0')).toBeTruthy()
    expect(screen.queryByText('box-4999')).toBeNull()
  })

  it('reveals more rows in a chunk when the affordance is clicked', async () => {
    const user = userEvent.setup()
    render(<StructuredOutput text={bigTable(5000)} />)
    expect(dataRows()).toHaveLength(200)

    await act(async () => { await user.click(screen.getByText(/show 2,000 more/)) })

    // 200 + TABLE_ROW_STEP (2,000) rows now shown; the note updates.
    expect(dataRows()).toHaveLength(2200)
    expect(screen.getByText(/showing 2,200 of 5,000 rows/)).toBeTruthy()
  })

  it('renders a small table whole with no affordance', () => {
    render(<StructuredOutput text={bigTable(10)} />)
    expect(dataRows()).toHaveLength(10)
    expect(screen.queryByText(/show .* more/)).toBeNull()
  })
})
