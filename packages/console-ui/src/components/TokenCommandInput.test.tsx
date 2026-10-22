/**
 * TokenCommandInput — the Token command / API key command field: Browse… swaps
 * in the picked program (keeping arguments, quoting a spaced path) and Check
 * shows what the backend would run, without running it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import { TokenCommandInput, withExecutable, describeCheck } from './TokenCommandInput'

const { browseExecutable, checkCommand } = vi.hoisted(() => ({
  browseExecutable: vi.fn(),
  checkCommand: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, prop) => {
      if (typeof prop === 'symbol') return undefined
      if (prop === 'browse_executable') return browseExecutable
      if (prop === 'check_api_key_command') return checkCommand
      return vi.fn().mockResolvedValue(undefined)
    },
  }),
}))

function Harness({ initial }: { initial: string }) {
  const [v, setV] = useState(initial)
  return (
    <>
      <TokenCommandInput value={v} onChange={setV} />
      <span data-testid="value">{v}</span>
    </>
  )
}

beforeEach(() => {
  browseExecutable.mockReset()
  checkCommand.mockReset()
})

describe('withExecutable', () => {
  it('replaces the program and keeps the arguments', () => {
    expect(withExecutable('get-key --scope llm', 'C:/v/Scripts/get-key.exe'))
      .toBe('C:/v/Scripts/get-key.exe --scope llm')
  })
  it('replaces a quoted program', () => {
    expect(withExecutable('"C:/Old Dir/k.exe" a b', 'C:/v/k.exe')).toBe('C:/v/k.exe a b')
  })
  it('quotes a path with spaces', () => {
    expect(withExecutable('', 'C:/Program Files/Corp/get-key.exe'))
      .toBe('"C:/Program Files/Corp/get-key.exe"')
  })
})

describe('describeCheck', () => {
  it('names the folders searched when the program is missing', () => {
    const text = describeCheck({ command: 'get-key', executable: 'get-key', found: false, searched: ['C:\\v\\Scripts'] })
    expect(text).toContain('C:\\v\\Scripts')
    expect(text).toContain('Browse')
  })
})

describe('TokenCommandInput', () => {
  it('Browse… fills in the picked program', async () => {
    browseExecutable.mockResolvedValue('C:/v/Scripts/get-key.exe')
    render(<Harness initial="get-key --scope llm" />)
    fireEvent.click(screen.getByRole('button', { name: /Browse/ }))
    await waitFor(() => expect(screen.getByTestId('value').textContent).toBe('C:/v/Scripts/get-key.exe --scope llm'))
  })

  it('a cancelled Browse… leaves the command alone', async () => {
    browseExecutable.mockResolvedValue('')
    render(<Harness initial="get-key" />)
    fireEvent.click(screen.getByRole('button', { name: /Browse/ }))
    await waitFor(() => expect(browseExecutable).toHaveBeenCalled())
    expect(screen.getByTestId('value').textContent).toBe('get-key')
  })

  it('Check shows the resolved command', async () => {
    checkCommand.mockResolvedValue({
      command: 'C:\\v\\Scripts\\get-key.exe x', executable: 'C:\\v\\Scripts\\get-key.exe', found: true, searched: ['C:\\v\\Scripts'],
    })
    render(<Harness initial="get-key x" />)
    fireEvent.click(screen.getByRole('button', { name: 'Check' }))
    expect(await screen.findByText('Runs C:\\v\\Scripts\\get-key.exe x')).toBeTruthy()
    expect(checkCommand).toHaveBeenCalledWith('get-key x')
  })

  it('Check is disabled for an empty command', () => {
    render(<Harness initial="" />)
    expect((screen.getByRole('button', { name: 'Check' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
