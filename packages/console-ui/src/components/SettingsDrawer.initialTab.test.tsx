/**
 * `initialTab` deep-links Settings to a specific tab — used by the MCP-server
 * "dropped tools" badge/toast, which opens straight to Settings → MCP Server so
 * the operator lands on the warning instead of hunting for it.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import { SettingsDrawer } from './SettingsDrawer'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, prop) => {
      if (typeof prop === 'symbol') return undefined
      if (prop === 'get_config') return async () => ({})
      if (prop === 'config_dir') return async () => 'C:/cfg'
      if (prop === 'get_hosts' || prop === 'get_jump_hosts' || prop === 'get_groups' || prop === 'get_profiles') return async () => []
      if (prop === 'get_mcp_server_config') return async () => ({
        enabled: true, host: '127.0.0.1', port: 8765, token: null,
        default_autonomy: null, confirm_wait: 50, tool_style: 'leaf', expose_builtins: false,
      })
      if (prop === 'mcp_server_status') return async () => ({
        running: true, error: '', host: '127.0.0.1', port: 8765,
        url: 'http://127.0.0.1:8765/mcp', auth: false, tools: 3, builtins: 0, tool_style: 'leaf',
        invalid_tools: [{ name: 'internal-metric', reason: "'9' is not of type 'number'" }],
      })
      return vi.fn().mockResolvedValue(undefined)
    },
  }),
}))

describe('SettingsDrawer initialTab deep-link', () => {
  it('opens directly on the MCP Server tab and shows its dropped-tool warning', async () => {
    render(<SettingsDrawer open initialTab="mcpServer" onClose={() => {}} />)
    // The MCP Server tab is the selected one…
    await waitFor(() => {
      const sel = screen.getAllByRole('tab').find(t => t.getAttribute('aria-selected') === 'true')
      expect(sel?.textContent).toBe('MCP Server')
    })
    // …and its content (the dropped-tool warning) is what's rendered.
    expect(await screen.findByText(/1 runnable\(s\) not published/)).toBeTruthy()
    expect(screen.getByText('internal-metric')).toBeTruthy()
  })
})
