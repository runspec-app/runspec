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
    max_content_chars: 8000,
    headers: { 'x-api-key': 'ls-secret' },
    api_key_command: 'keyring get langsmith key',
    api_key_header: 'x-api-key',
  },
}

const { getConfig, saveConfig, sendTestTrace } = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
  sendTestTrace: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (typeof prop === 'symbol') return undefined
        if (prop === 'get_config') return getConfig
        if (prop === 'save_config') return saveConfig
        if (prop === 'send_test_trace') return sendTestTrace
        return vi.fn().mockResolvedValue(undefined)
      },
    },
  ),
}))

describe('TelemetryTab Save round-trip', () => {
  beforeEach(() => {
    getConfig.mockReset().mockResolvedValue(INITIAL_CONFIG)
    saveConfig.mockReset().mockResolvedValue(undefined)
    sendTestTrace.mockReset().mockResolvedValue({
      ok: true,
      trace_id: '0123456789abcdef0123456789abcdef',
      spans: ['runspec.agent.turn', 'chat runspec-test-model', 'execute_tool run_runnable'],
      endpoint: 'https://langsmith.internal/otel/v1/traces',
      capture_content: false,
      flushed: true,
    })
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
    // The content-size ceiling round-trips (0 = no limit; here a set ceiling).
    expect(saved.telemetry.max_content_chars).toBe(8000)
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

  it('"Send test trace" saves first, calls the bridge, and shows the result', async () => {
    render(<TelemetryTab />)
    await screen.findByDisplayValue('https://langsmith.internal/otel/v1/traces')
    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: /send test trace/i }))
    })
    // Saved before sending, then emitted the trace.
    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(sendTestTrace).toHaveBeenCalledTimes(1))
    // The success result surfaces the trace id.
    await screen.findByText(/test trace sent/i)
    await screen.findByText(/0123456789abcdef0123456789abcdef/)
  })
})
