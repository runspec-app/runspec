/**
 * Rotation progress-bar wiring for the SSH Settings tab.
 *
 * The backend emits a `runspec:rotate_progress` window event per host while a
 * fleet-wide key rotation runs; the tab renders a live Progress bar from it so a
 * large rotation shows movement instead of a frozen "Rotating…" button. This
 * drives rotate_ssh_key through a deferred promise so we can assert the bar
 * appears on a progress event mid-run and clears once the call resolves.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { SshTab } from './SettingsDrawer'

const { getConfig, getPublicKey, configDir, rotateSshKey, generateSshKey } = vi.hoisted(() => ({
  getConfig: vi.fn(),
  getPublicKey: vi.fn(),
  configDir: vi.fn(),
  rotateSshKey: vi.fn(),
  generateSshKey: vi.fn(),
}))

vi.mock('../bridge', () => ({
  bridge: new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (typeof prop === 'symbol') return undefined
        if (prop === 'get_config') return getConfig
        if (prop === 'get_public_key') return getPublicKey
        if (prop === 'config_dir') return configDir
        if (prop === 'rotate_ssh_key') return rotateSshKey
        if (prop === 'generate_ssh_key') return generateSshKey
        return vi.fn().mockResolvedValue(undefined)
      },
    },
  ),
}))

function deferred<T>() {
  let resolve!: (v: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}

describe('SshTab rotation progress bar', () => {
  beforeEach(() => {
    // A committed key so the "Rotate key" button renders (gated on keyPath).
    getConfig.mockReset().mockResolvedValue({ ssh: { identityFile: '/home/u/.ssh/runspec_ed25519' } })
    getPublicKey.mockReset().mockResolvedValue({ ok: true, public_key: 'ssh-ed25519 AAAA' })
    configDir.mockReset().mockResolvedValue('/home/u/.config/runspec-console')
    rotateSshKey.mockReset()
    generateSshKey.mockReset()
  })

  it('shows a live bar on progress events and clears it when rotation finishes', async () => {
    const d = deferred<{ ok: boolean; committed: boolean; per_host: unknown[]; public_key: string; key_path: string; message: string }>()
    rotateSshKey.mockReturnValue(d.promise)

    const user = userEvent.setup()
    render(<SshTab connectedHosts={['prod-1', 'prod-2']} allRemoteHosts={['prod-1', 'prod-2']} />)

    // Open the Rotate confirm and start the rotation.
    await user.click(await screen.findByRole('button', { name: /rotate key/i }))
    await user.click(await screen.findByTestId('risk-ok'))
    await waitFor(() => expect(rotateSshKey).toHaveBeenCalled())

    // A progress event mid-run renders the bar with the current host + count.
    act(() => {
      window.dispatchEvent(new CustomEvent('runspec:rotate_progress', {
        detail: { done: 1, total: 2, host: 'prod-1', phase: 'verified' },
      }))
    })
    await screen.findByText(/Pushing .* verifying 1\/2 hosts/)
    // The current host is surfaced beside the count.
    expect(screen.getAllByText('prod-1').length).toBeGreaterThan(0)

    // Resolving the call ends the run and drops the live bar.
    await act(async () => {
      d.resolve({
        ok: true, committed: true, public_key: 'ssh-ed25519 AAAA',
        key_path: '/home/u/.ssh/runspec_ed25519',
        per_host: [
          { host: 'prod-1', pushed: true, verified: true, skipped: false, error: '' },
          { host: 'prod-2', pushed: true, verified: true, skipped: false, error: '' },
        ],
        message: 'Key rotated — verified on 2 host(s).',
      })
      await d.promise
    })
    await waitFor(() =>
      expect(screen.queryByText(/Pushing .* verifying/)).toBeNull(),
    )
  })

  it('disables Rotate under a scoped host profile', async () => {
    // A scoped profile would orphan its out-of-profile hosts, so rotation is only
    // offered under All hosts; here the button is present but disabled.
    render(<SshTab connectedHosts={['prod-1']} allRemoteHosts={['prod-1']} activeProfile="prod" />)
    const btn = await screen.findByRole('button', { name: /rotate key/i })
    expect((btn as HTMLButtonElement).disabled).toBe(true)
    // No confirm action is reachable while it's gated.
    expect(screen.queryByRole('button', { name: /^rotate$/i })).toBeNull()
    expect(rotateSshKey).not.toHaveBeenCalled()
  })
})

describe('SshTab Generate new key', () => {
  beforeEach(() => {
    getConfig.mockReset().mockResolvedValue({ ssh: { identityFile: '/home/u/.ssh/runspec_ed25519' } })
    getPublicKey.mockReset().mockResolvedValue({ ok: true, public_key: 'ssh-ed25519 AAAA' })
    configDir.mockReset().mockResolvedValue('/home/u/.config/runspec-console')
    generateSshKey.mockReset().mockResolvedValue({ ok: true, public_key: 'ssh-ed25519 BBBB', key_path: '/home/u/.ssh/runspec_ed25519' })
  })

  it('asks first and keeps OK off until "generate" is typed', async () => {
    const user = userEvent.setup()
    render(<SshTab connectedHosts={['prod-1']} allRemoteHosts={['prod-1']} />)
    await user.click(await screen.findByRole('button', { name: /generate new key/i }))

    // The destructive ask names the key and says hosts will refuse this machine.
    expect((await screen.findByTestId('risk-action')).textContent).toContain('/home/u/.ssh/runspec_ed25519')
    expect(screen.getByText(/will refuse this machine/)).toBeTruthy()
    const ok = screen.getByTestId('risk-ok') as HTMLButtonElement
    expect(ok.disabled).toBe(true)

    await user.type(screen.getByLabelText('Type generate to confirm'), 'generat')
    expect(ok.disabled).toBe(true)
    await user.type(screen.getByLabelText('Type generate to confirm'), 'e')
    expect(ok.disabled).toBe(false)
    await user.click(ok)
    await waitFor(() => expect(generateSshKey).toHaveBeenCalledTimes(1))
  })

  it('Cancel generates nothing', async () => {
    const user = userEvent.setup()
    render(<SshTab connectedHosts={['prod-1']} allRemoteHosts={['prod-1']} />)
    await user.click(await screen.findByRole('button', { name: /generate new key/i }))
    await screen.findByTestId('risk-ok')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByTestId('risk-ok')).toBeNull())
    expect(generateSshKey).not.toHaveBeenCalled()
  })
})
