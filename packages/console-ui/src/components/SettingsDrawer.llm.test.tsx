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

const { getConfig, saveConfig, defaultSystemPrompt } = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
  defaultSystemPrompt: vi.fn(),
}))

const DEFAULT_PROMPT = 'You are an assistant embedded in runspec-console.\n\nAsk one brief clarifying question instead of guessing.'

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
        if (prop === 'default_system_prompt') return defaultSystemPrompt
        return vi.fn()
      },
    },
  ),
}))

describe('LlmTab Save round-trip', () => {
  beforeEach(() => {
    getConfig.mockReset().mockResolvedValue(INITIAL_CONFIG)
    saveConfig.mockReset().mockResolvedValue(undefined)
    defaultSystemPrompt.mockReset().mockResolvedValue(DEFAULT_PROMPT)
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

describe('LlmTab model selector', () => {
  beforeEach(() => {
    saveConfig.mockReset().mockResolvedValue(undefined)
  })

  it('renders the operator-defined models as strict dropdown options and round-trips the list', async () => {
    getConfig.mockReset().mockResolvedValue({
      llm: {
        provider: 'anthropic',
        model: 'claude-sonnet-4-6',
        models: ['claude-sonnet-4-6', 'claude-opus-4-1', 'claude-haiku-4-5'],
      },
    })
    render(<LlmTab />)
    await screen.findByText('Available models')

    // The active-model field is a Select whose options are exactly the list.
    const user = userEvent.setup()
    // Opening the Model field shows exactly the operator-defined list as options
    // (a strict dropdown, not a free-text input). findByRole throws if absent.
    await user.click(screen.getByLabelText('Model'))
    await screen.findByRole('option', { name: 'claude-sonnet-4-6' })
    await screen.findByRole('option', { name: 'claude-opus-4-1' })

    // An untouched Save round-trips both the active model and the full list intact.
    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Save' }))
    })
    await waitFor(() => expect(saveConfig).toHaveBeenCalled())
    const saved = saveConfig.mock.calls[0][0] as { llm: Record<string, unknown> }
    expect(saved.llm.model).toBe('claude-sonnet-4-6')
    expect(saved.llm.models).toEqual(['claude-sonnet-4-6', 'claude-opus-4-1', 'claude-haiku-4-5'])
  })

  it('keeps an active model that is not in the list selectable (prepended on load)', async () => {
    getConfig.mockReset().mockResolvedValue({
      llm: {
        provider: 'anthropic',
        model: 'claude-experimental',
        models: ['claude-sonnet-4-6', 'claude-opus-4-1'],
      },
    })
    render(<LlmTab />)
    await screen.findByText('Available models')

    const user = userEvent.setup()
    await user.click(screen.getByLabelText('Model'))
    // The hand-edited model is offered even though it wasn't in `models`.
    await screen.findByRole('option', { name: 'claude-experimental' })

    await act(async () => {
      await user.click(screen.getByRole('button', { name: 'Save' }))
    })
    await waitFor(() => expect(saveConfig).toHaveBeenCalled())
    const saved = saveConfig.mock.calls[0][0] as { llm: Record<string, unknown> }
    expect(saved.llm.model).toBe('claude-experimental')
    // Prepended so it survives the round-trip and stays a valid option.
    expect(saved.llm.models).toEqual(['claude-experimental', 'claude-sonnet-4-6', 'claude-opus-4-1'])
  })
})


describe('LlmTab system prompt default', () => {
  beforeEach(() => {
    saveConfig.mockReset().mockResolvedValue(undefined)
    defaultSystemPrompt.mockReset().mockResolvedValue(DEFAULT_PROMPT)
  })

  it('pre-fills the field with the built-in default when [llm] system is unset', async () => {
    getConfig.mockReset().mockResolvedValue({ llm: { provider: 'anthropic', model: 'm' } })
    render(<LlmTab />)
    const area = (await screen.findByDisplayValue(/You are an assistant embedded/)) as HTMLTextAreaElement
    expect(area.value).toBe(DEFAULT_PROMPT)
    // Unchanged default ⇒ no "Reset to default" affordance.
    expect(screen.queryByText('Reset to default')).toBeNull()
  })

  it('saving the unchanged default writes no system key', async () => {
    getConfig.mockReset().mockResolvedValue({ llm: { provider: 'anthropic', model: 'm' } })
    render(<LlmTab />)
    await screen.findByDisplayValue(/You are an assistant embedded/)
    const user = userEvent.setup()
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Save' })) })
    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    const saved = saveConfig.mock.calls[0][0] as { llm: Record<string, unknown> }
    expect(saved.llm.system).toBeUndefined()
  })

  it('shows a configured prompt as-is, and an edit is written; reset restores the default', async () => {
    getConfig.mockReset().mockResolvedValue({ llm: { provider: 'anthropic', model: 'm', system: 'Only speak in haiku.' } })
    render(<LlmTab />)
    const area = (await screen.findByDisplayValue('Only speak in haiku.')) as HTMLTextAreaElement
    const user = userEvent.setup()
    await user.clear(area)
    await user.type(area, 'Be terse.')
    await act(async () => { await user.click(screen.getByRole('button', { name: 'Save' })) })
    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1))
    expect((saveConfig.mock.calls[0][0] as { llm: Record<string, unknown> }).llm.system).toBe('Be terse.')

    await act(async () => { await user.click(screen.getByText('Reset to default')) })
    expect((screen.getByDisplayValue(/You are an assistant embedded/) as HTMLTextAreaElement).value).toBe(DEFAULT_PROMPT)
  })
})
