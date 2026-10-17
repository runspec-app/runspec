/**
 * Round-trip test for the Telemetry Settings tab.
 *
 * Verifies the tab hydrates the [telemetry] section from get_config() and, on
 * Save, writes back the enable toggle, endpoint, capture-content flag, and the
 * headers key/value map — the shape config.read_telemetry() consumes.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createElement } from 'react'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { TelemetryTab } from './SettingsDrawer'
import { FeaturesContext } from '../features'

const INITIAL_CONFIG: Record<string, unknown> = {
  telemetry: {
    enabled: true,
    endpoint: 'https://langsmith.internal/otel/v1/traces',
    capture_content: false,
    max_content_chars: 8000,
    headers: { 'x-api-key': 'ls-secret' },
    api_key_command: 'keyring get langsmith key',
    api_key_header: 'x-api-key',
    credential: 'langsmith-key',
    credential_header: 'x-api-key',
    metrics: {
      enabled: true,
      endpoint: 'https://grafana.internal/otlp/v1/metrics',
      export_interval: 30,
      headers: { Authorization: 'Basic abc' },
      credential: 'grafana-key',
    },
  },
}

const { getConfig, saveConfig, sendTestTrace, sendTestMetric, credOptions } = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
  sendTestTrace: vi.fn(),
  sendTestMetric: vi.fn(),
  credOptions: vi.fn(),
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
        if (prop === 'send_test_metric') return sendTestMetric
        if (prop === 'mcp_credential_options') return credOptions
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
    credOptions.mockReset().mockResolvedValue([
      { id: 'langsmith-key', label: 'LangSmith', kind: 'token' },
      { id: 'grafana-key', label: 'Grafana', kind: 'userpass' },
    ])
    sendTestMetric.mockReset().mockResolvedValue({
      ok: true,
      instruments: ['runspec.tool.runs', 'runspec.agent.turns'],
      endpoint: 'https://grafana.internal/otlp/v1/metrics',
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
    // service_name is left blank when unset — the backend derives it from the
    // branded app slug — and label (→ service.instance.id) is blank too.
    expect(saved.telemetry.service_name).toBe('')
    expect(saved.telemetry.label).toBe('')
    // The "ship the command, not the key" fields round-trip.
    expect(saved.telemetry.api_key_command).toBe('keyring get langsmith key')
    expect(saved.telemetry.api_key_header).toBe('x-api-key')
    // The nested metrics section round-trips as its own signal (separate endpoint
    // + auth from traces).
    const metrics = saved.telemetry.metrics as Record<string, unknown>
    expect(metrics.enabled).toBe(true)
    expect(metrics.endpoint).toBe('https://grafana.internal/otlp/v1/metrics')
    expect(metrics.export_interval).toBe(30)
    expect(metrics.headers).toEqual({ Authorization: 'Basic abc' })
    // Keychain credential picks round-trip per signal (ids only — never secrets).
    expect(saved.telemetry.credential).toBe('langsmith-key')
    expect(saved.telemetry.credential_header).toBe('x-api-key')
    expect(metrics.credential).toBe('grafana-key')
  })

  it('"Send test metric" saves first, calls the bridge, and shows the result', async () => {
    render(<TelemetryTab />)
    await screen.findByDisplayValue('https://grafana.internal/otlp/v1/metrics')
    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: /send test metric/i }))
    })
    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(sendTestMetric).toHaveBeenCalledTimes(1))
    await screen.findByText(/test metric sent/i)
    await screen.findByText(/grafana\.internal/)
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

  it('the "Use Arize AX" preset fills generic OTLP fields: project, space_id header, api_key header (not the endpoint)', async () => {
    getConfig.mockResolvedValue({})
    render(<TelemetryTab />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /use arize ax/i }))
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Save' }))
    })
    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    const saved = saveConfig.mock.calls[0][0] as { telemetry: Record<string, unknown> }
    expect(saved.telemetry.enabled).toBe(true)
    // Region-specific, so not defaulted.
    expect(saved.telemetry.endpoint).toBe('')
    expect(saved.telemetry.project_name).toBe('runspec-console')
    expect(saved.telemetry.credential_header).toBe('api_key')
    expect(saved.telemetry.headers).toEqual({ space_id: '' })
  })

  it('round-trips the project name', async () => {
    getConfig.mockResolvedValue({ telemetry: { enabled: true, project_name: 'rc-dev' } })
    render(<TelemetryTab />)
    await screen.findByDisplayValue('rc-dev')
    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Save' }))
    })
    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    const saved = saveConfig.mock.calls[0][0] as { telemetry: Record<string, unknown> }
    expect(saved.telemetry.project_name).toBe('rc-dev')
  })

  it('shows a rejected export as a failure with the backend reason', async () => {
    sendTestTrace.mockResolvedValue({
      ok: false,
      reason: 'the backend rejected the export: Failed to export spans batch code: 401',
      export: { ok: false, error: 'code: 401', spans: 3 },
    })
    render(<TelemetryTab />)
    await screen.findByDisplayValue('https://langsmith.internal/otel/v1/traces')
    const user = userEvent.setup()
    await act(async () => {
      await user.click(screen.getByRole('button', { name: /send test trace/i }))
    })
    await screen.findByText(/could not send a test trace/i)
    await screen.findByText(/batch code: 401/)
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

  it('renders metrics-only when AI is off (traces controls hidden, metrics + Save shown)', async () => {
    await act(async () => {
      render(
        createElement(FeaturesContext.Provider, { value: { ai: false } },
          createElement(TelemetryTab)),
      )
    })
    await waitFor(() => expect(getConfig).toHaveBeenCalled())
    // Trace-only controls are gone.
    expect(screen.queryByText('Enable tracing')).toBeNull()
    expect(screen.queryByRole('button', { name: /send test trace/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /use local phoenix/i })).toBeNull()
    // Metrics + the shared resource identity + Save remain.
    expect(screen.getByText('Enable metrics')).toBeTruthy()
    expect(screen.getByRole('button', { name: /send test metric/i })).toBeTruthy()
    expect(screen.getByText('Instance label')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy()
  })
})
