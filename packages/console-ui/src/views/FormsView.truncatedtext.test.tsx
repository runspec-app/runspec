/**
 * Unit tests for TruncatedText — the one-line, ellipsised description used for
 * arg and subcommand descriptions in the Forms run dialog.
 *
 * Regression: the full description must be reachable on hover/click *and* an info
 * icon must be visible, unconditionally — an earlier version gated both on a
 * runtime scrollWidth/clientWidth overflow measurement that ran while the dialog
 * was still animating (and read 0/0 inside a collapsed section), so it often left
 * a clipped description with no tooltip, no help cursor and no icon.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { TruncatedText } from './FormsView'

// The real `bridge` is a Proxy that waits on window.pywebview — replace with a
// no-op stub (FormsView imports it at module load).
vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

const LONG =
  'The number of parallel workers to spawn — higher is faster but uses more memory.'

describe('TruncatedText', () => {
  it('always renders the text with a discoverable info icon and a help cursor', () => {
    const { container } = render(<TruncatedText text={LONG} />)
    // The (truncated) preview text is present regardless of any measurement.
    expect(screen.getByText(LONG)).toBeTruthy()
    // The always-visible ⓘ affordance the user can click.
    expect(container.querySelector('.anticon-info-circle')).toBeTruthy()
    // The container signals interactivity even before any overflow is measured.
    const outer = screen.getByText(LONG).parentElement as HTMLElement
    expect(outer.style.cursor).toBe('help')
  })

  it('reveals the full description on hover via a tooltip (not gated on overflow)', async () => {
    const { container } = render(<TruncatedText text={LONG} />)
    const trigger = container.querySelector('span[style*="cursor"]') as HTMLElement
    fireEvent.mouseEnter(trigger)
    // antd renders the tooltip overlay into the DOM on hover — the full text now
    // appears a second time (preview + overlay).
    await vi.waitFor(() => {
      expect(screen.getAllByText(LONG).length).toBeGreaterThanOrEqual(2)
    })
  })
})
