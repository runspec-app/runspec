/**
 * Forms search across the profile: the scope toggle, the "more on other hosts"
 * hint, and the host picker on a card found on several hosts.
 */
import { beforeEach, describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'

import { FormsView } from './FormsView'
import { FeaturesContext } from '../features'
import type { Host, Runnable } from '../bridge'

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: () => vi.fn().mockResolvedValue(undefined) }),
}))

const hosts = [
  { name: 'local', connected: true, runnableCount: 1, groups: [] },
  { name: 'web-01', connected: true, runnableCount: 1, groups: [] },
  { name: 'web-02', connected: true, runnableCount: 1, groups: [] },
] as Host[]
// No args + no subcommands = runs straight away on pick, so the run callback
// shows which host was chosen.
const rn = (name: string, host: string) => ({ name, host, group: 'ops', description: `${name} tool`, args: [] }) as unknown as Runnable
const runnables = [rn('uptime', 'local'), rn('restart-web', 'web-01'), rn('restart-web', 'web-02')]

beforeEach(() => window.localStorage.clear())

function renderForms() {
  const onRun = vi.fn()
  render(
    <FeaturesContext.Provider value={{ ai: true }}>
      <FormsView runnables={runnables} hosts={hosts} selectedHost="local" activeScope={[]} activeProfile="web" onRunRunnable={onRun} />
    </FeaturesContext.Provider>,
  )
  return onRun
}

const search = (q: string) => fireEvent.change(screen.getByPlaceholderText(/Search name/), { target: { value: q } })

describe('Forms → search across the profile', () => {
  it('offers the other hosts when the selected one has no match', () => {
    renderForms()
    search('restart')
    expect(screen.queryByText('restart-web')).toBeNull()
    expect(screen.getByText(/1 more match on other hosts/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Search all of web' }))
    expect(screen.getByText('restart-web')).toBeTruthy()
    expect(screen.getByText('2 hosts')).toBeTruthy()
    expect(window.localStorage.getItem('runspec.forms.scope')).toBe('profile')
  })

  it('a card on several hosts asks which host to run on', async () => {
    const onRun = renderForms()
    fireEvent.click(screen.getByText('All in web'))
    fireEvent.click(screen.getByText('restart-web'))
    expect(onRun).not.toHaveBeenCalled()
    const menu = await screen.findByRole('menu')
    fireEvent.click(within(menu).getByText('web-02'))
    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({ name: 'restart-web', host: 'web-02' }), {}, [])
  })

  it('a card on one host runs there directly', () => {
    const onRun = renderForms()
    fireEvent.click(screen.getByText('uptime'))
    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({ host: 'local' }), {}, [])
  })
})
