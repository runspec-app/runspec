/**
 * Regression test for the LLM Settings tab's Save round-trip.
 *
 * The bug (this branch): `api_key_command` and `api_key_ttl_ms` are hand-edited
 * (or surfaced) under the "Advanced" collapsible panel. antd's Collapse does not
 * mount an unexpanded panel's children, so when the operator saved after editing
 * only a top-level field (provider/model/key) without ever opening Advanced,
 * `form.getFieldsValue()` omitted those fields, the set-or-delete loop saw them as
 * blank, and silently deleted them from config.toml.
 *
 * The fix is `forceRender` on the Advanced panel so its Form.Items are always
 * mounted. This test drives the exact scenario — render, edit one top-level field,
 * Save without touching Advanced — and asserts the adapter keys survive.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { LlmTab } from './SettingsDrawer'

// Config the panel loads on mount: a LangServe adapter wired with a rotating
// token command + cache TTL — the keys that were going missing.
const INITIAL_CONFIG: Record<string, unknown> = {
  llm: {
    provider: 'langserve',
    base_url: 'https://gateway.corp/my-chain',
    api_key_command: 'aws-vend-token --print',
    api_key_ttl_ms: 300000,
    auth_header: 'Authorization',
    auth_scheme: 'Bearer',
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
        if (prop === 'config_dir') return async () => 'C:/cfg'
        if (prop === 'list_providers') return async () => []
        return vi.fn()
      },
    },
  ),
}))

describe('LlmTab Save round-trip', () => {
  beforeEach(() => {
    getConfig.mockReset().mockResolvedValue(INITIAL_CONFIG)
    saveConfig.mockReset().mockResolvedValue(undefined)
  })

  it('preserves api_key_command and api_key_ttl_ms when saving without opening Advanced', async () => {
    render(<LlmTab />)

    // Wait for the form to hydrate from get_config().
    await screen.findByDisplayValue('https://gateway.corp/my-chain')

    // Edit a top-level field only — never expand "Advanced".
    const user = userEvent.setup()
    const baseUrl = screen.getByDisplayValue('https://gateway.corp/my-chain')
    await user.clear(baseUrl)
    await user.type(baseUrl, 'https://gateway.corp/other-chain')

    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Save' }))
    })

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    const saved = saveConfig.mock.calls[0][0] as { llm: Record<string, unknown> }
    expect(saved.llm.base_url).toBe('https://gateway.corp/other-chain')
    // The adapter keys under Advanced must survive an untouched Save.
    expect(saved.llm.api_key_command).toBe('aws-vend-token --print')
    expect(saved.llm.api_key_ttl_ms).toBe(300000)
  })
})
