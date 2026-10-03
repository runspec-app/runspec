/**
 * The shortcut sheet renders from the registry and hides AI-only shortcuts
 * when the console runs with AI off.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ShortcutsHelp } from './ShortcutsHelp'
import { FeaturesContext } from '../features'

describe('ShortcutsHelp', () => {
  it('lists the shortcuts grouped, with their keys', () => {
    render(<ShortcutsHelp open onClose={() => {}} />)
    expect(screen.getByText('Command palette')).toBeTruthy()
    expect(screen.getByText('Approve the pending confirmation')).toBeTruthy()
    expect(screen.getByText('New chat')).toBeTruthy()
    expect(screen.getByText(/keep their usual meaning/)).toBeTruthy()
  })

  it('hides AI-only shortcuts when AI is off', () => {
    render(
      <FeaturesContext.Provider value={{ ai: false }}>
        <ShortcutsHelp open onClose={() => {}} />
      </FeaturesContext.Provider>,
    )
    expect(screen.queryByText('New chat')).toBeNull()
    expect(screen.queryByText('Stop the running turn')).toBeNull()
    expect(screen.getByText('Deny the pending confirmation')).toBeTruthy()
  })
})
