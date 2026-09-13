/**
 * Round-trip test for the "MCP Server" Settings tab (the console's own MCP
 * server). Verifies it hydrates from get_mcp_server_config(), renders live
 * status from mcp_server_status(), and on Save writes the [mcp_server] shape.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { McpServerTab } from './SettingsDrawer'

const { getCfg, saveCfg, statusFn } = vi.hoisted(() => ({
  getCfg: vi.fn(),
  saveCfg: vi.fn(),
  statusFn: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (typeof prop === 'symbol') return undefined
        if (prop === 'get_mcp_server_config') return getCfg
        if (prop === 'save_mcp_server_config') return saveCfg
        if (prop === 'mcp_server_status') return statusFn
        return vi.fn().mockResolvedValue(undefined)
      },
    },
  ),
}))

const OFF = { running: false, error: '', host: '127.0.0.1', port: 8765, url: '', auth: false, tools: 0 }

describe('McpServerTab', () => {
  beforeEach(() => {
    getCfg.mockReset().mockResolvedValue({
      enabled: false, host: '127.0.0.1', port: 8765, token: null,
      default_autonomy: null, confirm_wait: 50, tool_style: 'leaf',
    })
    saveCfg.mockReset().mockResolvedValue({ ...OFF, running: true, url: 'http://127.0.0.1:8765/mcp', tools: 12 })
    statusFn.mockReset().mockResolvedValue(OFF)
  })

  it('hydrates config and shows not-running status', async () => {
    await act(async () => { render(<McpServerTab />) })
    await waitFor(() => expect(getCfg).toHaveBeenCalled())
    expect(statusFn).toHaveBeenCalled()
    expect(await screen.findByText('not running')).toBeTruthy()
  })

  it('enables + saves the [mcp_server] shape', async () => {
    const user = userEvent.setup()
    await act(async () => { render(<McpServerTab />) })
    await waitFor(() => expect(getCfg).toHaveBeenCalled())

    // flip the enable switch on
    await user.click(screen.getByRole('switch'))
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saveCfg).toHaveBeenCalledTimes(1))
    const payload = saveCfg.mock.calls[0][0]
    expect(payload).toMatchObject({
      enabled: true,
      host: '127.0.0.1',
      port: 8765,
      token: null,
      default_autonomy: null,
      confirm_wait: 50,
      tool_style: 'leaf',
    })
  })

  it('renders the running URL + connect hint from status', async () => {
    statusFn.mockResolvedValue({ ...OFF, running: true, url: 'http://127.0.0.1:8765/mcp', tools: 7 })
    await act(async () => { render(<McpServerTab />) })
    expect(await screen.findByText('http://127.0.0.1:8765/mcp')).toBeTruthy()
    expect(screen.getByText(/7 tool\(s\)/)).toBeTruthy()
  })
})
