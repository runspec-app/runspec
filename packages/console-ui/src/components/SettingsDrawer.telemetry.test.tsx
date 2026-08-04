/**
 * Round-trip test for the Telemetry Settings tab.
 *
 * Verifies the tab hydrates the [telemetry] section from get_config() and, on
 * Save, writes back the enable toggle, endpoint, capture-content flag, and the
 * headers key/value map — the shape config.read_telemetry() consumes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { TelemetryTab } from './SettingsDrawer'

const INITIAL_CONFIG: Record<string, unknown> = {
  telemetry: {
    enabled: true,
    endpoint: 'https://langsmith.internal/otel/v1/traces',
    capture_content: false,
    headers: { 'x-api-key': 'ls-secret' },
    api_key_command: 'keyring get langsmith key',
    api_key_header: 'x-api-key',
  },
}

const { getConfig, saveConfig } = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (typeof prop === 'symbol') return undefined
        if (prop === 'get_config') return getConfig
        if (prop === 'save_config') return saveConfig
        return vi.fn().mockResolvedValue(undefined)
      },
    },
  ),
}))

describe('TelemetryTab Save round-trip', () => {
  beforeEach(() => {
    getConfig.mockReset().mockResolvedValue(INITIAL_CONFIG)
    saveConfig.mockReset().mockResolvedValue(undefined)
  })

  it('hydrates from config and round-trips the telemetry section on Save', async () => {
    render(<TelemetryTab />)

    // Hydrated the endpoint from get_config().
    await screen.findByDisplayValue('https://langsmith.internal/otel/v1/traces')

    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Save' }))
    })

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    const saved = saveConfig.mock.calls[0][0] as { telemetry: Record<string, unknown> }
    expect(saved.telemetry.enabled).toBe(true)
    expect(saved.telemetry.endpoint).toBe('https://langsmith.internal/otel/v1/traces')
    expect(saved.telemetry.capture_content).toBe(false)
    // The headers editor round-trips as a plain object keyed by header name.
    expect(saved.telemetry.headers).toEqual({ 'x-api-key': 'ls-secret' })
    // Sensible defaults are written even when the section omitted them.
    expect(saved.telemetry.protocol).toBe('http/protobuf')
    expect(saved.telemetry.service_name).toBe('runspec-console')
    // The "ship the command, not the key" fields round-trip.
    expect(saved.telemetry.api_key_command).toBe('keyring get langsmith key')
    expect(saved.telemetry.api_key_header).toBe('x-api-key')
  })

  it('the "Use local Phoenix" preset fills the endpoint, enables, and captures content', async () => {
    getConfig.mockResolvedValue({})  // start from a blank telemetry section
    render(<TelemetryTab />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /use local phoenix/i }))
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Save' }))
    })
    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    const saved = saveConfig.mock.calls[0][0] as { telemetry: Record<string, unknown> }
    expect(saved.telemetry.enabled).toBe(true)
    expect(saved.telemetry.endpoint).toBe('http://127.0.0.1:6006/v1/traces')
    expect(saved.telemetry.capture_content).toBe(true)
  })
})
