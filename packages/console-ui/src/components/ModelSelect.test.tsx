/**
 * ModelSelect: offers the operator's [llm] models (plus the active model), keeps
 * a hand-edited value selectable, and reports "" when cleared.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { ModelSelect, modelOptions } from './ModelSelect'

const { getConfig } = vi.hoisted(() => ({ getConfig: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => (p === 'get_config' ? getConfig : vi.fn()),
  }),
}))

beforeEach(() => {
  getConfig.mockReset().mockResolvedValue({
    llm: { model: 'claude-sonnet-5', models: ['claude-sonnet-4-6', 'claude-haiku-4-5'] },
  })
})

describe('modelOptions', () => {
  it('puts the active model first when it is not listed, and dedupes', () => {
    expect(modelOptions({ model: 'a', models: ['b', 'c'] })).toEqual(['a', 'b', 'c'])
    expect(modelOptions({ model: 'b', models: ['b', 'c'] })).toEqual(['b', 'c'])
    expect(modelOptions(undefined)).toEqual([])
  })
})

describe('ModelSelect', () => {
  it('lists the configured models and reports the pick', async () => {
    const onChange = vi.fn()
    render(<ModelSelect onChange={onChange} />)
    await waitFor(() => expect(getConfig).toHaveBeenCalled())
    await userEvent.click(screen.getByRole('combobox'))
    await userEvent.click(await screen.findByText('claude-haiku-4-5'))
    expect(onChange).toHaveBeenCalledWith('claude-haiku-4-5')
  })

  it('shows a value that is not in the list', async () => {
    render(<ModelSelect value="claude-opus-5" />)
    await waitFor(() => expect(getConfig).toHaveBeenCalled())
    expect(screen.getByText('claude-opus-5')).toBeTruthy()
  })
})
