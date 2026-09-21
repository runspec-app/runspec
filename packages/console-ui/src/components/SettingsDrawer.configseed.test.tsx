/**
 * Config Sync tab — the Config seed panel must surface a failed seed apply.
 * A single malformed bundled file aborts the whole seed (backend returns the
 * error on seed_status), so the operator sees why nothing was configured
 * instead of a silently-minimal install.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

import { ConfigSyncTab } from './SettingsDrawer'
import type { SeedStatus } from '../bridge'

const { getSyncConfig, listCredentials, seedStatus, getDocsConfig } = vi.hoisted(() => ({
  getSyncConfig: vi.fn(),
  listCredentials: vi.fn(),
  seedStatus: vi.fn(),
  getDocsConfig: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, prop) => {
      if (typeof prop === 'symbol') return undefined
      if (prop === 'get_sync_config') return getSyncConfig
      if (prop === 'list_credentials') return listCredentials
      if (prop === 'seed_status') return seedStatus
      if (prop === 'get_docs_config') return getDocsConfig
      return vi.fn().mockResolvedValue(undefined)
    },
  }),
}))

const SYNC_CONFIG = {
  url: '', ref: '', credential_id: '', layers: [], subpath: '',
  ca_bundle: '', tls_verify: true, files: [], files_default: [],
  preserve: [], backups_keep: 5,
}

function seed(over: Partial<SeedStatus>): SeedStatus {
  return { available: true, stamp: { 'jason-console': '1.0.0' }, applied: {}, last_applied: '', pending: true, ...over }
}

beforeEach(() => {
  getSyncConfig.mockReset().mockResolvedValue(SYNC_CONFIG)
  listCredentials.mockReset().mockResolvedValue([])
  getDocsConfig.mockReset().mockResolvedValue({ dir: '', default: '' })
  seedStatus.mockReset()
})

describe('Config seed panel', () => {
  it('shows the apply error when a bundled file is malformed', async () => {
    seedStatus.mockResolvedValue(seed({
      error: 'runspec_repos.toml is not valid TOML — nothing was written: Expected newline (at line 1, column 9)',
    }))
    render(<ConfigSyncTab />)

    expect(await screen.findByText('Config seed failed to apply')).toBeTruthy()
    expect(screen.getByText(/runspec_repos\.toml is not valid TOML/)).toBeTruthy()
  })

  it('shows no error banner when the seed applied cleanly', async () => {
    seedStatus.mockResolvedValue(seed({ applied: { 'jason-console': '1.0.0' }, pending: false, error: '' }))
    render(<ConfigSyncTab />)

    // The panel renders (Bundled line) but no failure banner.
    expect(await screen.findByText(/Bundled:/)).toBeTruthy()
    expect(screen.queryByText('Config seed failed to apply')).toBeNull()
  })
})
