/**
 * Jump Hosts tab for large fleets: a group overview (one row per group, with
 * member + connected counts) that drills into a group's host rows, plus a
 * search box / profile filter that flattens across groups.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { message } from 'antd'

import { SettingsDrawer, buildHostGroups, filterHosts, groupSummary } from './SettingsDrawer'
import type { JumpHost } from '../bridge'

const { getJumpHosts, getHosts, getGroups, getProfiles, saveJumpHosts } = vi.hoisted(() => ({
  getJumpHosts: vi.fn(),
  getHosts: vi.fn(),  // must NOT be consulted — no live state on this tab
  getGroups: vi.fn(),
  getProfiles: vi.fn(),
  saveJumpHosts: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, prop) => {
      if (typeof prop === 'symbol') return undefined
      if (prop === 'get_jump_hosts') return getJumpHosts
      if (prop === 'get_hosts') return getHosts
      if (prop === 'get_groups') return getGroups
      if (prop === 'get_profiles') return getProfiles
      if (prop === 'save_jump_hosts') return saveJumpHosts
      if (prop === 'get_config') return async () => ({})
      if (prop === 'config_dir') return async () => 'C:/cfg'
      return vi.fn().mockResolvedValue(undefined)
    },
  }),
}))

const HOSTS: JumpHost[] = [
  { name: 'web-01', hostname: 'web-01.corp', user: 'deploy', group: 'Production', profiles: ['app-support'] },
  { name: 'web-02', hostname: 'web-02.corp', user: 'deploy', group: 'Production', profiles: ['app-support', 'devops'] },
  { name: 'db-01', hostname: 'db-01.corp', group: 'Production' },
  { name: 'stg-01', hostname: 'stg-01.corp', group: 'Staging', profiles: ['devops'] },
  { name: 'lab', hostname: '10.0.0.9' },
]

describe('pure helpers', () => {
  it('filterHosts matches name/hostname/user/group and narrows by profile', () => {
    expect(filterHosts(HOSTS, 'web', '').map(h => h.name)).toEqual(['web-01', 'web-02'])
    expect(filterHosts(HOSTS, '10.0', '').map(h => h.name)).toEqual(['lab'])
    expect(filterHosts(HOSTS, 'DEPLOY', '').map(h => h.name)).toEqual(['web-01', 'web-02'])
    expect(filterHosts(HOSTS, 'stag', '').map(h => h.name)).toEqual(['stg-01'])
    expect(filterHosts(HOSTS, '', 'devops').map(h => h.name)).toEqual(['web-02', 'stg-01'])
    expect(filterHosts(HOSTS, 'web', 'devops').map(h => h.name)).toEqual(['web-02'])
  })

  it('groupSummary counts members and collects profiles; buildHostGroups keeps order', () => {
    expect(buildHostGroups(HOSTS).map(g => g.name)).toEqual(['Production', 'Staging', ''])
    const sum = groupSummary(HOSTS)
    expect(sum[0]).toMatchObject({ name: 'Production', profiles: ['app-support', 'devops'] })
    expect(sum[0].hosts).toHaveLength(3)
    expect(sum[1]).toMatchObject({ name: 'Staging', profiles: ['devops'] })
    expect(sum[2]).toMatchObject({ name: '', profiles: [] })
  })
})

// These render the whole Settings drawer and click through an antd modal, which
// takes ~5s under jsdom — right at vitest's default 5s timeout, so they flaked
// on a loaded runner. Give them headroom rather than trimming what they cover.
describe('Jump Hosts tab', { timeout: 20_000 }, () => {
  beforeEach(() => {
    getJumpHosts.mockReset().mockResolvedValue(HOSTS)
    getHosts.mockReset().mockResolvedValue([])
    getGroups.mockReset().mockResolvedValue([{ name: 'Production', description: 'Customer-facing' }, { name: 'Staging' }])
    getProfiles.mockReset().mockResolvedValue([{ name: 'app-support' }, { name: 'devops' }])
    saveJumpHosts.mockReset().mockResolvedValue(undefined)
  })

  async function openTab() {
    render(<SettingsDrawer open onClose={() => {}} />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('tab', { name: 'Jump Hosts' }))
    await screen.findByText('5 hosts · 3 groups')
    return user
  }

  it('shows a group overview and drills into a group', async () => {
    const user = await openTab()
    // Overview: one row per group with counts — no host rows yet.
    expect(screen.getByRole('button', { name: 'Open group Production' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open group Ungrouped' })).toBeTruthy()
    expect(screen.getByText('Customer-facing')).toBeTruthy()
    expect(screen.queryByText('web-01')).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Open group Production' }))
    await screen.findByText('web-01')
    expect(screen.getByText('web-02')).toBeTruthy()
    expect(screen.getByText('db-01')).toBeTruthy()
    expect(screen.queryByText('stg-01')).toBeNull()
    expect(screen.getByRole('button', { name: 'Edit web-01' })).toBeTruthy()

    await user.click(screen.getByRole('button', { name: '← All groups' }))
    await screen.findByRole('button', { name: 'Open group Staging' })
    expect(screen.queryByText('web-01')).toBeNull()
  })

  it('search flattens across groups; the profile filter narrows', async () => {
    const user = await openTab()
    await user.type(screen.getByRole('textbox', { name: 'Search hosts' }), 'web')
    await screen.findByText('2 of 5 hosts match')
    expect(screen.getByText('web-01')).toBeTruthy()
    expect(screen.getByText('web-02')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Open group Production' })).toBeNull()

    await user.clear(screen.getByRole('textbox', { name: 'Search hosts' }))
    await screen.findByRole('button', { name: 'Open group Production' })

    // Profile filter: devops → web-02 + stg-01 across two groups.
    await user.click(screen.getAllByTitle('Filter by host profile')[0].querySelector('input')!)
    await user.click(await screen.findByTitle('devops'))
    await screen.findByText('2 of 5 hosts match')
    expect(screen.getByText('web-02')).toBeTruthy()
    expect(screen.getByText('stg-01')).toBeTruthy()
    // Managing host entries never reads live connection state.
    expect(getHosts).not.toHaveBeenCalled()
  })

  async function openEdit(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole('button', { name: 'Open group Production' }))
    await user.click(await screen.findByRole('button', { name: 'Edit web-01' }))
    await screen.findByText('Edit — web-01')
  }

  // Regression: a rejected save (e.g. the backend can't write the TOML) must
  // surface an error and keep the modal open — it used to throw silently, so the
  // modal never closed and gave no feedback. (The Save button being reachable is
  // the open signal; antd keeps the closed modal's text mounted but hidden.)
  it('keeps the edit modal open and shows an error when the save fails', async () => {
    saveJumpHosts.mockRejectedValueOnce(new Error('Invalid value (at line 4, column 18)'))
    const errSpy = vi.spyOn(message, 'error')
    const user = await openTab()
    await openEdit(user)

    await user.click(screen.getByRole('button', { name: /save/i }))

    expect(saveJumpHosts).toHaveBeenCalledTimes(1)
    expect(errSpy).toHaveBeenCalledWith(expect.stringContaining("Couldn't save host"))
    // Modal still open (Save button still reachable) for the operator to retry.
    expect(screen.getByRole('button', { name: /save/i })).toBeTruthy()
    errSpy.mockRestore()
  })

  it('closes the edit modal on a successful save', async () => {
    const user = await openTab()
    await openEdit(user)

    await user.click(screen.getByRole('button', { name: /save/i }))

    // Modal closed → the Save button leaves the accessibility tree.
    await waitFor(() => expect(screen.queryByRole('button', { name: /save/i })).toBeNull())
    expect(saveJumpHosts).toHaveBeenCalledTimes(1)
  })

  // Regression: Provision opened from inside the host editor rendered beneath
  // it. The editor sits in the drawer, so antd stacks it at drawer + 100; the
  // Provision dialog must land above that, not just above a bare modal.
  it('opens Provision new venv on top of the host editor', async () => {
    const user = await openTab()
    await openEdit(user)
    const zOf = (title: string) => {
      const wrap = screen.getByText(title).closest('.ant-modal-wrap') as HTMLElement
      return Number(wrap.style.zIndex)
    }
    const editorZ = zOf('Edit — web-01')

    await user.click(screen.getByRole('button', { name: /Provision new venv/ }))
    await screen.findByText('Provision a runspec venv')

    expect(zOf('Provision a runspec venv')).toBeGreaterThan(editorZ)
  })
})
