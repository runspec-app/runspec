/**
 * Settings → Credentials helper: the tab lists what the discovered runnables
 * need, each "Add" opens the dialog pre-filled with a label that matches, a
 * saved credential shows what uses it, and one nothing reads gets a fix hint.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { CredentialsTab } from './SettingsDrawer'
import { hostSummary } from './CredentialGuide'

const { listCreds, guide, matches, save } = vi.hoisted(() => ({
  listCreds: vi.fn(),
  guide: vi.fn(),
  matches: vi.fn(),
  save: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, p) => {
      if (p === 'list_credentials') return listCreds
      if (p === 'credential_guide') return guide
      if (p === 'credential_matches') return matches
      if (p === 'save_credential') return save
      if (p === 'credentials_backend') return async () => ({ available: true, backend: 'test' })
      return vi.fn()
    },
  }),
}))

const GUIDE = {
  hosts: ['local', 'web-01'],
  needs: [
    { prefix: 'JIRA', label: 'Jira', runnables: [{ name: 'jira', hosts: ['local', 'web-01'] }],
      options: [
        { kind: 'token', matches: ['JIRA_TOKEN', 'JIRA_PAT'], hint: 'Jira Personal Access Token.' },
        { kind: 'userpass', matches: ['JIRA_USERNAME', 'JIRA_USER', 'JIRA_PASSWORD'], hint: '' },
      ],
      reads: ['JIRA_PAT', 'JIRA_TOKEN'], other_env: ['JIRA_URL'], covered_by: [] },
    { prefix: 'GRAFANA', label: 'Grafana', runnables: [{ name: 'grafana', hosts: ['local'] }],
      options: [{ kind: 'token', matches: ['GRAFANA_TOKEN', 'GRAFANA_PAT'], hint: '' }],
      reads: [], other_env: [], covered_by: [{ id: 'g', label: 'Grafana', has_secret: true }] },
  ],
  bindings: [
    { runnable: 'deploy', hosts: ['web-01'], arg: 'password', username_arg: 'user', hint: '', label: 'Deploy', covered_by: [] },
  ],
  usage: {
    g: { runnables: [{ name: 'grafana', hosts: ['local'], via: 'env', envs: ['GRAFANA_TOKEN'] }], features: [], hint: null },
    odd: { runnables: [], features: [], hint: { label: 'Jira', kind: 'token', why: 'the label sets JIRA_PAT_…' } },
  },
  features: [{ feature: 'Config Sync', tab: 'configSync', credential: 'gone', status: 'missing' }],
}

describe('Credentials helper', () => {
  beforeEach(() => {
    listCreds.mockReset().mockResolvedValue([
      { id: 'g', label: 'Grafana', kind: 'token', has_secret: true },
      { id: 'odd', label: 'Jira PAT', kind: 'token', has_secret: true },
    ])
    guide.mockReset().mockResolvedValue(GUIDE)
    matches.mockReset().mockResolvedValue([{ name: 'jira', hosts: ['local', 'web-01'], via: 'env', envs: ['JIRA_TOKEN'] }])
    save.mockReset().mockResolvedValue({ id: 'new' })
  })

  it('lists what is not set up and hides covered needs until asked', async () => {
    render(<CredentialsTab />)
    expect(await screen.findByTestId('cred-need-JIRA')).toBeTruthy()
    expect(screen.queryByTestId('cred-need-GRAFANA')).toBeNull()
    expect(screen.getByTestId('cred-binding-deploy-password')).toBeTruthy()
    await userEvent.click(screen.getByRole('switch', { name: 'Show set up' }))
    expect(screen.getByTestId('cred-need-GRAFANA')).toBeTruthy()
  })

  it('Add token opens the dialog pre-filled and saves a matching credential', async () => {
    render(<CredentialsTab />)
    const card = await screen.findByTestId('cred-need-JIRA')
    await userEvent.click(within(card).getByRole('button', { name: /Add token/ }))
    expect((await screen.findByPlaceholderText('Service account (prod)') as HTMLInputElement).value).toBe('Jira')
    await waitFor(() => expect(screen.getByTestId('cred-match-list').textContent).toContain('jira'))
    await userEvent.click(screen.getByRole('button', { name: /Save/ }))
    await waitFor(() => expect(save).toHaveBeenCalled())
    expect(save.mock.calls[0][0]).toMatchObject({ label: 'Jira', kind: 'token' })
  })

  it('binding Add pre-fills the argument and scope', async () => {
    render(<CredentialsTab />)
    const card = await screen.findByTestId('cred-binding-deploy-password')
    await userEvent.click(within(card).getByRole('button', { name: /Add credential for --password/ }))
    await userEvent.click(await screen.findByRole('button', { name: /Save/ }))
    await waitFor(() => expect(save).toHaveBeenCalled())
    expect(save.mock.calls[0][0]).toMatchObject({
      label: 'Deploy', kind: 'userpass', secret_arg: 'password', username_arg: 'user', scope_runnables: ['deploy'],
    })
  })

  it('shows what a credential is used by, a fix for an unused one, and broken feature refs', async () => {
    const onOpenTab = vi.fn()
    render(<CredentialsTab onOpenTab={onOpenTab} />)
    const used = await screen.findByTestId('cred-row-g')
    await waitFor(() => expect(within(used).getByText(/Used by/)).toBeTruthy())
    expect(within(used).getByText(/grafana · local/)).toBeTruthy()

    const odd = screen.getByTestId('cred-row-odd')
    expect(within(odd).getByText(/Not used by any runnable/)).toBeTruthy()
    await userEvent.click(within(odd).getByRole('button', { name: 'Fix…' }))
    expect((await screen.findByPlaceholderText('Service account (prod)') as HTMLInputElement).value).toBe('Jira')

    expect(screen.getByText(/uses credential/).textContent).toContain("doesn't exist")
    await userEvent.click(screen.getByRole('button', { name: 'Open Config Sync' }))
    expect(onOpenTab).toHaveBeenCalledWith('configSync')
  })

  it('no matches warns in the dialog', async () => {
    matches.mockResolvedValue([])
    render(<CredentialsTab />)
    await userEvent.click(await screen.findByRole('button', { name: /(^| )Add$/ }))
    expect(await screen.findByTestId('cred-match-none')).toBeTruthy()
  })
})

describe('hostSummary', () => {
  it('shortens long host lists', () => {
    expect(hostSummary(['a', 'b'])).toBe('a, b')
    expect(hostSummary(['a', 'b', 'c', 'd', 'e'])).toBe('a, b, c +2')
  })
})
