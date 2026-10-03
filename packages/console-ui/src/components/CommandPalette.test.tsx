/**
 * The Ctrl+K palette filters the actions it's handed as you type and runs the
 * chosen one (closing itself first).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { CommandPalette, type PaletteAction } from './CommandPalette'

function actions(run: () => void): PaletteAction[] {
  return [
    { id: 'tab:history', label: 'History', group: 'Go to', run: vi.fn() },
    { id: 'runnable:disk-usage', label: 'disk-usage', group: 'Runnables', detail: '3 hosts', run },
    { id: 'host:web-01', label: 'web-01', group: 'Hosts', run: vi.fn() },
  ]
}

describe('CommandPalette', () => {
  it('filters by label as you type', async () => {
    render(<CommandPalette open onClose={() => {}} actions={actions(vi.fn())} />)
    const input = screen.getByPlaceholderText(/Type a command/)
    await act(async () => { fireEvent.change(input, { target: { value: 'disk' } }) })
    expect(screen.getByText('disk-usage')).toBeTruthy()
    expect(screen.queryByText('web-01')).toBeNull()
  })

  it('closes and runs the selected action on Enter', async () => {
    vi.useFakeTimers()
    const run = vi.fn()
    const onClose = vi.fn()
    render(<CommandPalette open onClose={onClose} actions={actions(run)} />)
    const input = screen.getByPlaceholderText(/Type a command/)
    await act(async () => { fireEvent.change(input, { target: { value: 'disk' } }) })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    expect(onClose).toHaveBeenCalled()
    await act(async () => { vi.runAllTimers() })
    expect(run).toHaveBeenCalledOnce()
    vi.useRealTimers()
  })
})
