import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { LogCleanupModal, cleanupProblem, policyText } from './LogCleanupModal'

const { api } = vi.hoisted(() => ({ api: {} as Record<string, ReturnType<typeof vi.fn>> }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, p: string) => (api[p] ??= vi.fn(() => Promise.resolve({ ok: true }))) }),
}))

const T = { host: 'web-1', group: 'ops' }

beforeEach(() => {
  for (const k of Object.keys(api)) delete api[k]
  api.logs_retention = vi.fn(() => Promise.resolve({
    ok: true, group: 'ops', file: '/opt/venvs/ops/runspec_logs_retention.toml',
    policy: { compact_after: '7d', delete_after: '90d' },
    last: { ts: '2026-11-07T03:10:00Z', compacted: 4, deleted: 1, freed_bytes: 2048 },
  }))
})

describe('helpers', () => {
  it('checks durations, sizes and the order', () => {
    const f = (c = '', d = '', m = '') => cleanupProblem({ compact_after: c, delete_after: d, max_total_size: m })
    expect(f()).toBe('')
    expect(f('7d', '90d', '5GB')).toBe('')
    expect(f('', '', '500 mb')).toBe('')
    expect(f('90d', '7d')).toMatch(/shorter/)
    expect(f('soon')).toMatch(/isn't a duration/)
    expect(f('', '', 'lots')).toMatch(/isn't a size/)
    expect(f('off', '90d')).toBe('')
    expect(f('never', 'off', '0')).toBe('')
  })

  it('describes a policy', () => {
    expect(policyText({ compact_after: '7d', max_total_size: '5GB' })).toBe('compress after 7d, keep at most 5GB')
    expect(policyText(null)).toBe('')
  })
})

describe('LogCleanupModal', () => {
  it('shows the policy and its last pass, and saves a change', async () => {
    api.logs_retention_set = vi.fn(() => Promise.resolve({ ok: true, group: 'ops', policy: { compact_after: '7d', delete_after: '30d' }, as_user: 'svc' }))
    render(<LogCleanupModal target={T} onClose={() => {}} />)
    expect(await screen.findByText('compress after 7d, delete after 90d')).toBeTruthy()
    expect(screen.getByText(/compressed 4, deleted 1/)).toBeTruthy()
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save.hasAttribute('disabled')).toBe(true)
    fireEvent.change(screen.getByLabelText('Delete after'), { target: { value: '30d' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.logs_retention_set).toHaveBeenCalledWith('web-1', 'ops', { compact_after: '7d', delete_after: '30d', max_total_size: '' }))
    await waitFor(() => expect(api.logs_retention).toHaveBeenCalledTimes(2))
  })

  it('clearing every field removes the policy', async () => {
    render(<LogCleanupModal target={T} onClose={() => {}} />)
    await screen.findByText('compress after 7d, delete after 90d')
    fireEvent.change(screen.getByLabelText('Compress after'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('Delete after'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Remove policy' }))
    await waitFor(() => expect(api.logs_retention_set).toHaveBeenCalledWith('web-1', 'ops', { off: true }))
  })

  it('previews the saved policy', async () => {
    api.logs_retention_run = vi.fn(() => Promise.resolve({ ok: true, group: 'ops', dry_run: true, compacted: 2, deleted: 5, freed_bytes: 1024 }))
    render(<LogCleanupModal target={T} onClose={() => {}} />)
    await screen.findByText('compress after 7d, delete after 90d')
    fireEvent.click(screen.getByRole('button', { name: 'Preview now' }))
    expect(await screen.findByText(/would compress 2 run files and delete 5/)).toBeTruthy()
    expect(api.logs_retention_run).toHaveBeenCalledWith('web-1', 'ops', true)
  })

  it('Apply now shows what it would delete, then needs the venv name typed', async () => {
    api.logs_retention_run = vi.fn((_h: string, _g: string, dry: boolean) => Promise.resolve(
      { ok: true, group: 'ops', dry_run: dry, compacted: 2, deleted: 5, freed_bytes: 1024 }))
    render(<LogCleanupModal target={T} onClose={() => {}} />)
    await screen.findByText('compress after 7d, delete after 90d')
    fireEvent.click(screen.getByRole('button', { name: 'Apply now…' }))
    expect((await screen.findByTestId('risk-action')).textContent).toMatch(/Delete 5 files/)
    expect(api.logs_retention_run).toHaveBeenCalledWith('web-1', 'ops', true)
    const ok = screen.getByTestId('risk-ok')
    expect(ok.hasAttribute('disabled')).toBe(true)
    fireEvent.change(screen.getByLabelText('Type ops to confirm'), { target: { value: 'ops' } })
    expect(ok.hasAttribute('disabled')).toBe(false)
    fireEvent.click(ok)
    await waitFor(() => expect(api.logs_retention_run).toHaveBeenCalledWith('web-1', 'ops', false))
  })

  it('Apply now with nothing to remove just says so', async () => {
    api.logs_retention_run = vi.fn(() => Promise.resolve({ ok: true, group: 'ops', dry_run: true, compacted: 0, deleted: 0, freed_bytes: 0 }))
    render(<LogCleanupModal target={T} onClose={() => {}} />)
    await screen.findByText('compress after 7d, delete after 90d')
    fireEvent.click(screen.getByRole('button', { name: 'Apply now…' }))
    expect(await screen.findByText('Nothing to compress or delete right now.')).toBeTruthy()
    expect(screen.queryByTestId('risk-action')).toBeNull()
  })

  it('says to upgrade an old runspec', async () => {
    api.logs_retention = vi.fn(() => Promise.resolve({ ok: false, group: 'ops', too_old: true, error: "this venv's runspec has no log retention policy (needs runspec 0.80.0 or later)" }))
    render(<LogCleanupModal target={T} onClose={() => {}} />)
    expect(await screen.findByText(/needs runspec 0.80.0/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })
})
