import { describe, it, expect } from 'vitest'

import {
  copilotModelIds, groupProviderOptions, providerKeys, switchProvider, type ProviderOption,
} from './llmProviders'

const OPTIONS: ProviderOption[] = [
  { value: 'anthropic', label: 'Anthropic', kind: 'builtin' },
  { value: 'copilot', label: 'GitHub Copilot', kind: 'builtin' },
  { value: 'mycorp', label: 'mycorp', kind: 'plugin', keys: ['tenant_id', 'effort', 'system'] },
  { value: 'pkg.mod:Adapter', label: 'pkg.mod:Adapter', kind: 'path' },
]
const keysFor = (p: string) => providerKeys(p, OPTIONS)

describe('providerKeys', () => {
  it('adds a plugin\'s own settings but never console-wide ones', () => {
    const keys = providerKeys('mycorp', OPTIONS)
    expect(keys).toContain('tenant_id')
    expect(keys).toContain('base_url')
    expect(keys).not.toContain('effort')
    expect(keys).not.toContain('system')
  })
})

describe('switchProvider', () => {
  const corp = {
    provider: 'mycorp',
    base_url: 'https://gateway.corp/chain',
    api_key_command: 'vend-token --print',
    tenant_id: 'ops',
    model: 'corp-large',
    models: ['corp-large'],
    system: 'Be careful.',
    tool_mode: 'search',
  }

  it('parks the old provider\'s settings and keeps console-wide ones', () => {
    const out = switchProvider(corp, 'mycorp', 'copilot', keysFor)
    expect(out.provider).toBe('copilot')
    expect(out.base_url).toBeUndefined()
    expect(out.tenant_id).toBeUndefined()
    expect(out.model).toBeUndefined()
    expect(out.system).toBe('Be careful.')
    expect(out.tool_mode).toBe('search')
    expect(out.saved_providers).toEqual({
      mycorp: {
        base_url: 'https://gateway.corp/chain',
        api_key_command: 'vend-token --print',
        tenant_id: 'ops',
        model: 'corp-large',
        models: ['corp-large'],
      },
    })
  })

  it('brings everything back when switching back', () => {
    const toCopilot = switchProvider(corp, 'mycorp', 'copilot', keysFor)
    const withModel = { ...toCopilot, model: 'gpt-5', models: ['gpt-5'] }
    const back = switchProvider(withModel, 'copilot', 'mycorp', keysFor)
    expect(back).toEqual({
      ...corp,
      saved_providers: { copilot: { model: 'gpt-5', models: ['gpt-5'] } },
    })
  })

  it('clearing the provider parks its settings too', () => {
    const out = switchProvider(corp, 'mycorp', '', keysFor)
    expect(out.provider).toBeUndefined()
    expect(Object.keys(out.saved_providers as object)).toEqual(['mycorp'])
  })

  it('drops an empty saved_providers table', () => {
    const out = switchProvider({ provider: 'anthropic' }, 'anthropic', 'copilot', keysFor)
    expect(out).toEqual({ provider: 'copilot' })
  })
})

describe('groupProviderOptions', () => {
  it('lists built-ins first, then installed packages, then a module path', () => {
    const groups = groupProviderOptions(OPTIONS)
    expect(groups.map(g => g.label)).toEqual(['Built in', 'From installed packages', 'From config (module path)'])
    expect(groups[0].options.map(o => o.value)).toEqual(['anthropic', 'copilot'])
  })

  it('treats an older backend\'s untyped options as built-ins', () => {
    const groups = groupProviderOptions([{ value: 'anthropic', label: 'Anthropic' }])
    expect(groups).toEqual([{ label: 'Built in', options: [{ value: 'anthropic', label: 'Anthropic' }] }])
  })
})

describe('copilotModelIds', () => {
  it('offers only models the account can use', () => {
    expect(copilotModelIds([
      { id: 'a', name: 'A', enabled: true },
      { id: 'b', name: 'B', enabled: false },
    ])).toEqual(['a'])
    expect(copilotModelIds(undefined)).toEqual([])
  })
})
