/**
 * Settings → SSH → Terminal look: loads [terminal] from config, previews the
 * theme, and saves only the fields that are set.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { TerminalLook, lookFromConfig, lookToConfig } from './TerminalLook'

const { getConfig, saveConfig, terminalThemes, getGroups } = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
  terminalThemes: vi.fn(),
  getGroups: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'get_config') return getConfig
      if (p === 'save_config') return saveConfig
      if (p === 'terminal_themes') return terminalThemes
      if (p === 'get_groups') return getGroups
      return vi.fn()
    },
  }),
}))

const NORD = {
  id: 'nord', label: 'Nord', fg: '#d8dee9', bg: '#2e3440', cursor: '#d8dee9',
  ansi: Array(16).fill('#bf616a'),
}

beforeEach(() => {
  vi.clearAllMocks()
  terminalThemes.mockResolvedValue([NORD])
  getGroups.mockResolvedValue([{ name: 'prod' }])
  saveConfig.mockResolvedValue(undefined)
})

describe('lookFromConfig / lookToConfig', () => {
  it('round-trips and drops unset fields', () => {
    const look = lookFromConfig({ theme: 'nord', font_size: 11, group_themes: { prod: 'red-alert' } })
    expect(look.font).toBe('')
    expect(look.cursor_blink).toBeNull()
    expect(lookToConfig(look)).toEqual({ theme: 'nord', font_size: 11, group_themes: { prod: 'red-alert' } })
  })

  it('an untouched look writes nothing', () => {
    expect(lookToConfig(lookFromConfig(undefined))).toEqual({})
  })

  it('skips half-filled group rows', () => {
    const look = lookFromConfig({})
    look.group_themes = [{ group: '', theme: 'red-alert' }, { group: 'prod ', theme: 'nord' }]
    expect(lookToConfig(look)).toEqual({ group_themes: { prod: 'nord' } })
  })
})

describe('TerminalLook', () => {
  it('previews the saved theme and keeps other config on save', async () => {
    getConfig.mockResolvedValue({ ssh: { user: 'ops' }, terminal: { theme: 'nord', font: 'Consolas' } })
    render(<TerminalLook />)
    await waitFor(() => expect(screen.getByTestId('terminal-preview').style.background).toBe('rgb(46, 52, 64)'))
    await userEvent.click(screen.getByRole('button', { name: /save terminal look/i }))
    await waitFor(() => expect(saveConfig).toHaveBeenCalled())
    expect(saveConfig.mock.calls[0][0]).toEqual({ ssh: { user: 'ops' }, terminal: { theme: 'nord', font: 'Consolas' } })
  })

  it('removes the [terminal] section when everything is cleared', async () => {
    getConfig.mockResolvedValue({ ssh: {} })
    render(<TerminalLook />)
    await userEvent.click(screen.getByRole('button', { name: /save terminal look/i }))
    await waitFor(() => expect(saveConfig).toHaveBeenCalled())
    expect(saveConfig.mock.calls[0][0]).toEqual({ ssh: {} })
  })

  it('adds a group colours row', async () => {
    getConfig.mockResolvedValue({})
    render(<TerminalLook />)
    await userEvent.click(screen.getByRole('button', { name: /add group colours/i }))
    expect(screen.getByRole('button', { name: /remove group colours/i })).toBeTruthy()
  })
})
