/**
 * Round-trip test for the Settings → Voice tab: hydrates from get_voice_config,
 * lists microphones, and on Save sends the `[voice]` section (without the
 * read-only status fields) to save_voice_config, adopting what comes back.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { VoiceTab } from './SettingsDrawer'

const { getVoiceConfig, saveVoiceConfig, listVoiceDevices, browseFolder } = vi.hoisted(() => ({
  getVoiceConfig: vi.fn(),
  saveVoiceConfig: vi.fn(),
  listVoiceDevices: vi.fn(),
  browseFolder: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, prop) => {
      if (typeof prop === 'symbol') return undefined
      if (prop === 'get_voice_config') return getVoiceConfig
      if (prop === 'save_voice_config') return saveVoiceConfig
      if (prop === 'list_voice_devices') return listVoiceDevices
      if (prop === 'browse_folder') return browseFolder
      return vi.fn().mockResolvedValue(undefined)
    },
  }),
}))

const CONFIG = {
  enabled: true, language: 'en', model: 'medium-streaming', model_dir: null, device: null,
  update_interval: 0.5, silence_timeout: 8, keep_loaded: true,
  available: true, reason: '', model_loaded: true,
  models: ['tiny-streaming', 'base-streaming', 'small-streaming', 'medium-streaming'],
}

beforeEach(() => {
  getVoiceConfig.mockReset().mockResolvedValue(CONFIG)
  saveVoiceConfig.mockReset().mockImplementation(async (section) => ({ ...CONFIG, ...section, model_loaded: false }))
  listVoiceDevices.mockReset().mockResolvedValue([
    { index: 1, name: 'Headset mic', default: true },
    { index: 4, name: 'Webcam mic', default: false },
  ])
  browseFolder.mockReset().mockResolvedValue('C:/models/en')
})

describe('Settings → Voice', () => {
  it('hydrates the section and shows the engine status', async () => {
    render(<VoiceTab />)
    expect(await screen.findByText(/engine loaded — next press is instant/)).toBeTruthy()
    expect((screen.getByLabelText('Language') as HTMLInputElement).value).toBe('en')
    expect((screen.getByLabelText('Silence timeout') as HTMLInputElement).value).toBe('8')
    expect(screen.getByLabelText('Keep model loaded').getAttribute('aria-checked')).toBe('true')
  })

  it('shows the unavailable reason when the extra is missing', async () => {
    getVoiceConfig.mockResolvedValue({ ...CONFIG, available: false, reason: 'moonshine-voice not installed' })
    render(<VoiceTab />)
    expect(await screen.findByText(/unavailable — moonshine-voice not installed/)).toBeTruthy()
  })

  it('saves the edited section without the read-only fields and adopts the result', async () => {
    render(<VoiceTab />)
    await screen.findByText(/engine loaded/)

    fireEvent.click(screen.getByLabelText('Keep model loaded'))
    fireEvent.change(screen.getByLabelText('Silence timeout'), { target: { value: '15' } })
    fireEvent.click(screen.getByText('Browse…'))
    await waitFor(() => expect((screen.getByLabelText('Vendored model directory') as HTMLInputElement).value).toBe('C:/models/en'))

    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(saveVoiceConfig).toHaveBeenCalledTimes(1))
    const sent = saveVoiceConfig.mock.calls[0][0]
    expect(sent).toEqual({
      enabled: true, language: 'en', model: 'medium-streaming', model_dir: 'C:/models/en', device: null,
      update_interval: 0.5, silence_timeout: 15, keep_loaded: false,
    })
    expect(sent).not.toHaveProperty('models')
    expect(sent).not.toHaveProperty('available')
    // The backend released the engine (keep_loaded off) — status reflects it.
    expect(await screen.findByText(/engine not loaded/)).toBeTruthy()
  })
})
