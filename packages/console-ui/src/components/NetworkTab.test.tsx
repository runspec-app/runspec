/**
 * Settings → Network: hydrates the [http] proxy, sends the form on Save, and
 * tests the unsaved settings against a URL.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { NetworkTab, parseNoProxy } from './NetworkTab'

const { getConfig, saveConfig, testProxy, credOptions } = vi.hoisted(() => ({
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
  testProxy: vi.fn(),
  credOptions: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, prop) => {
      if (typeof prop === 'symbol') return undefined
      if (prop === 'get_http_proxy_config') return getConfig
      if (prop === 'save_http_proxy_config') return saveConfig
      if (prop === 'test_http_proxy') return testProxy
      if (prop === 'mcp_credential_options') return credOptions
      return vi.fn().mockResolvedValue(undefined)
    },
  }),
}))

const MANUAL = {
  mode: 'manual', proxy: 'http://proxy.corp:8080', no_proxy: ['corp.local', 'gitlab.internal'],
  credential: '', effective: 'http://proxy.corp:8080, bypass corp.local, gitlab.internal',
}

beforeEach(() => {
  getConfig.mockReset().mockResolvedValue(MANUAL)
  saveConfig.mockReset().mockImplementation(async (p) => ({ ...p, effective: 'saved' }))
  testProxy.mockReset().mockResolvedValue({ ok: true, url: 'https://pypi.org/simple/', status: 200, route: 'via http://proxy.corp:8080' })
  credOptions.mockReset().mockResolvedValue([{ id: 'px', label: 'Proxy', kind: 'userpass' }])
})

describe('parseNoProxy', () => {
  it('splits on commas, spaces and newlines', () => {
    expect(parseNoProxy(' a, b\nc  ,,')).toEqual(['a', 'b', 'c'])
  })
})

describe('Settings → Network', () => {
  it('hydrates the manual proxy and shows what is in use', async () => {
    render(<NetworkTab />)
    await waitFor(() => expect((screen.getByLabelText('Proxy URL') as HTMLInputElement).value).toBe('http://proxy.corp:8080'))
    expect((screen.getByLabelText('Bypass') as HTMLTextAreaElement).value).toBe('corp.local, gitlab.internal')
    expect(screen.getByText(/bypass corp.local, gitlab.internal/)).toBeTruthy()
  })

  it('saves the form as an [http] payload', async () => {
    render(<NetworkTab />)
    const url = await screen.findByLabelText('Proxy URL')
    await waitFor(() => expect((url as HTMLInputElement).value).toBe('http://proxy.corp:8080'))
    fireEvent.change(url, { target: { value: 'proxy2:3128' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveConfig).toHaveBeenCalledWith({
      mode: 'manual', proxy: 'proxy2:3128', no_proxy: ['corp.local', 'gitlab.internal'], credential: '',
    }))
    expect(await screen.findByText('saved')).toBeTruthy()
  })

  it('tests the unsaved settings and reports the route', async () => {
    render(<NetworkTab />)
    await screen.findByLabelText('Proxy URL')
    fireEvent.change(screen.getByLabelText('Test URL'), { target: { value: 'https://example.com' } })
    fireEvent.click(screen.getByRole('button', { name: /Test/ }))
    await waitFor(() => expect(testProxy).toHaveBeenCalledWith(expect.objectContaining({ mode: 'manual' }), 'https://example.com'))
    expect(await screen.findByText(/Reached https:\/\/pypi.org\/simple\/ \(HTTP 200\)/)).toBeTruthy()
    expect(screen.getByText('Route: via http://proxy.corp:8080')).toBeTruthy()
  })

  it('hides the manual fields in system mode', async () => {
    getConfig.mockResolvedValue({ mode: 'system', proxy: '', no_proxy: [], credential: '', effective: 'system' })
    render(<NetworkTab />)
    expect(await screen.findByText(/environment variables, if set/)).toBeTruthy()
    expect(screen.queryByLabelText('Proxy URL')).toBeNull()
  })
})
