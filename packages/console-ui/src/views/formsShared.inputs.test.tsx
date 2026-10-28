/**
 * Forms run dialog inputs keep what the operator picks.
 *
 * Time / date-time pickers commit the pick without an OK click, and an optional
 * choice field can be cleared again after an option is picked.
 *
 * Regression: antd's TimePicker (and DatePicker with showTime) default to
 * `needConfirm`, so clicking a new hour/minute only previewed it — closing the
 * popup without pressing OK reset the field to its value, which for a defaulted
 * arg looked like the time "snapping back" to the default.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'

import { ArgInput } from './formsShared'
import type { ArgDef } from '../bridge'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn() }),
}))

function openAndPick(container: HTMLElement, cellText: string) {
  const input = container.querySelector('input') as HTMLInputElement
  fireEvent.mouseDown(input)
  fireEvent.click(input)
  const cell = Array.from(document.querySelectorAll('.ant-picker-time-panel-cell-inner'))
    .find(el => el.textContent === cellText) as HTMLElement
  expect(cell).toBeTruthy()
  fireEvent.click(cell)
  return input
}

async function closePicker(input: HTMLInputElement) {
  fireEvent.keyDown(input, { key: 'Escape' })
  fireEvent.blur(input)
  await act(async () => { await new Promise(r => setTimeout(r, 50)) })
}

describe('ArgInput time pickers', () => {
  it('a defaulted time arg takes a new hour without pressing OK', async () => {
    const arg = { name: 'at', type: 'str', ui: 'time', default: '09:00:00', required: false } as ArgDef
    const onChange = vi.fn()
    const { container } = render(<ArgInput arg={arg} host="" runnable="" value="09:00:00" onChange={onChange} />)
    const input = openAndPick(container, '14')
    await closePicker(input)
    expect(onChange).toHaveBeenCalledWith('14:00:00')
  })

  it('a defaulted datetime arg takes a new hour without pressing OK', async () => {
    const arg = { name: 'when', type: 'str', ui: 'datetime', required: false } as ArgDef
    const onChange = vi.fn()
    const { container } = render(
      <ArgInput arg={arg} host="" runnable="" value="2026-10-28T09:00:00.000Z" onChange={onChange} />)
    const input = openAndPick(container, '23')
    await closePicker(input)
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange.mock.calls[0][0]).toMatch(/^2026-10-2\dT/)
  })
})

describe('ArgInput choice field', () => {
  function clearIcon(container: HTMLElement) {
    return container.querySelector('.ant-select-clear') as HTMLElement | null
  }

  it('an optional choice can be cleared after picking an option', () => {
    const arg = { name: 'fmt', type: 'choice', options: ['json', 'yaml'], required: false } as ArgDef
    const onChange = vi.fn()
    const { container } = render(<ArgInput arg={arg} host="" runnable="" value="json" onChange={onChange} />)
    const clear = clearIcon(container)
    expect(clear).toBeTruthy()
    fireEvent.mouseDown(clear as HTMLElement)
    fireEvent.click(clear as HTMLElement)
    expect(onChange).toHaveBeenCalled()
    expect(onChange.mock.calls[0][0]).toBeUndefined()
  })

  it('a required choice has no clear button', () => {
    const arg = { name: 'fmt', type: 'choice', options: ['json', 'yaml'], required: true } as ArgDef
    const { container } = render(<ArgInput arg={arg} host="" runnable="" value="json" onChange={vi.fn()} />)
    expect(clearIcon(container)).toBeNull()
  })
})
