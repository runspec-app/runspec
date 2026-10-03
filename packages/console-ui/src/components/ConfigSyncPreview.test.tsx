/**
 * Config Sync review-then-pull: the preview modal shows each file's diff and
 * headline; the Config Sync tab fetches the preview, pulls exactly the previewed
 * commit, and offers a newer tag when one is available.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import { ConfigSyncPreviewModal, diffLineKind, summaryLine } from './ConfigSyncPreview'
import { ConfigSyncTab } from './SettingsDrawer'
import type { SyncPreview } from '../bridge'

const api = vi.hoisted(() => ({
  get_sync_config: vi.fn(),
  save_sync_config: vi.fn(),
  list_credentials: vi.fn(),
  seed_status: vi.fn(),
  get_docs_config: vi.fn(),
  sync_preview: vi.fn(),
  sync_pull: vi.fn(),
  sync_check_update: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, prop) => {
      if (typeof prop === 'symbol') return undefined
      if (prop in api) return api[prop as keyof typeof api]
      return vi.fn().mockResolvedValue(undefined)
    },
  }),
}))

const PREVIEW: SyncPreview = {
  ok: true, ref: 'v1.2.0', commit: 'c0ffee00aa', current_ref: 'v1.1.0', current_commit: 'beef0000', changed: true,
  files: [
    {
      name: 'runspec_hosts.toml', status: 'changed',
      summary: { kind: 'collection', changed: true, added: ['web2'], updated: [{ id: 'web1', fields: ['hostname'] }], removed: [], unchanged: 3 },
      diff: '--- local/runspec_hosts.toml\n+++ v1.2.0/runspec_hosts.toml\n@@ -1,3 +1,3 @@\n name = "web1"\n-hostname = "old"\n+hostname = "new"\n',
    },
    { name: 'runspec_triggers.toml', status: 'missing' },
  ],
}

describe('pure helpers', () => {
  it('classifies diff lines', () => {
    expect(diffLineKind('+++ b/x')).toBe('file')
    expect(diffLineKind('--- a/x')).toBe('file')
    expect(diffLineKind('@@ -1 +1 @@')).toBe('hunk')
    expect(diffLineKind('+a = 1')).toBe('add')
    expect(diffLineKind('-a = 1')).toBe('del')
    expect(diffLineKind(' a = 1')).toBe('ctx')
  })

  it('summarises a file', () => {
    expect(summaryLine(PREVIEW.files![0])).toBe('1 entry added · 1 updated · 3 unchanged')
    expect(summaryLine(PREVIEW.files![1])).toMatch(/skipped/)
    expect(summaryLine({ name: 'config.toml', status: 'changed', summary: { kind: 'mapping', changed: true, added: ['a', 'b'], updated: [], removed: [] } }))
      .toBe('2 keys added')
  })
})

describe('ConfigSyncPreviewModal', () => {
  it('renders the diff and pulls on accept', () => {
    const onPull = vi.fn()
    render(<ConfigSyncPreviewModal open loading={false} pulling={false} preview={PREVIEW} onPull={onPull} onCancel={() => {}} />)
    expect(screen.getByText('Review changes from v1.2.0')).toBeTruthy()
    expect(screen.getByText('+hostname = "new"').getAttribute('data-kind')).toBe('add')
    expect(screen.getByText('-hostname = "old"').getAttribute('data-kind')).toBe('del')
    fireEvent.click(screen.getByText('Pull these changes'))
    expect(onPull).toHaveBeenCalled()
  })

  it('blocks the pull when a file is invalid', () => {
    const bad: SyncPreview = { ...PREVIEW, files: [{ name: 'runspec_filters.toml', status: 'invalid', error: 'bad' }] }
    render(<ConfigSyncPreviewModal open loading={false} pulling={false} preview={bad} onPull={() => {}} onCancel={() => {}} />)
    expect(screen.getByText(/isn't valid TOML/)).toBeTruthy()
    expect(screen.getByText('Pull these changes').closest('button')?.disabled).toBe(true)
  })
})

describe('ConfigSyncTab review & pull', () => {
  beforeEach(() => {
    api.get_sync_config.mockReset().mockResolvedValue({
      url: 'https://g/cfg.git', ref: 'v1.1.0', credential_id: '', layers: [], subpath: '',
      ca_bundle: '', tls_verify: true, files: ['runspec_hosts.toml'], files_default: ['runspec_hosts.toml'],
      preserve: [], backups_keep: 5, engine: 'dulwich', last_ref: 'v1.1.0', last_commit: 'beef0000', last_synced: '',
    })
    api.save_sync_config.mockReset().mockResolvedValue(undefined)
    api.list_credentials.mockReset().mockResolvedValue([])
    api.seed_status.mockReset().mockResolvedValue({ available: false, stamp: {}, applied: {}, last_applied: '', pending: false })
    api.get_docs_config.mockReset().mockResolvedValue({ dir: '', default: '' })
    api.sync_preview.mockReset().mockResolvedValue(PREVIEW)
    api.sync_pull.mockReset().mockResolvedValue({ ok: true, ref: 'v1.2.0', commit: 'c0ffee00aa', written: ['runspec_hosts.toml'] })
    api.sync_check_update.mockReset().mockResolvedValue({ ok: true, configured: true, available: true, kind: 'tag', current: 'v1.1.0', latest: 'v1.2.0' })
  })

  it('offers the newer tag, previews it, and pulls the previewed commit', async () => {
    render(<ConfigSyncTab />)
    expect(await screen.findByText(/Newer config available/)).toBeTruthy()
    fireEvent.click(screen.getByText('Review v1.2.0'))
    await waitFor(() => expect(api.sync_preview).toHaveBeenCalledWith('v1.2.0'))
    fireEvent.click(await screen.findByText('Pull these changes'))
    await waitFor(() => expect(api.sync_pull).toHaveBeenCalledWith('v1.2.0', 'c0ffee00aa'))
  })
})
