/**
 * BrowseModal — the path-picker can select the current folder in BOTH modes.
 *
 * Regression for the reported UX gap: a path arg that takes a folder (or a
 * file-or-folder arg like install-into-venv's --project) was unpickable in file
 * mode — you could only click a file or descend into a directory. The modal now
 * always offers "Use this folder".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { BrowseModal } from './PathInput'

const { listDir } = vi.hoisted(() => ({ listDir: vi.fn() }))

vi.mock('../bridge', () => ({
  bridge: new Proxy({}, { get: (_t, p) => (p === 'list_dir' ? listDir : vi.fn()) }),
}))

beforeEach(() => {
  listDir.mockReset().mockResolvedValue({
    cwd: '/home/me/venvs/app',
    parent: '/home/me/venvs',
    entries: [
      { name: 'bin', type: 'directory' },
      { name: 'pyvenv.cfg', type: 'file' },
    ],
    truncated: false,
  })
})

async function open(props: Record<string, unknown>) {
  const onPick = vi.fn()
  const onClose = vi.fn()
  render(<BrowseModal host="prod-1" onPick={onPick} onClose={onClose} {...props} />)
  await screen.findByText('pyvenv.cfg') // wait for the listing to load
  return { onPick, onClose }
}

describe('BrowseModal folder selection', () => {
  it('offers "Use this folder" in file mode and picks the current dir', async () => {
    const { onPick } = await open({}) // dirsOnly undefined → file mode
    const btn = screen.getByRole('button', { name: 'Use this folder' })
    await act(async () => {
      await userEvent.setup().click(btn)
    })
    expect(onPick).toHaveBeenCalledWith('/home/me/venvs/app')
  })

  it('still lets you click a file in file mode', async () => {
    const { onPick } = await open({})
    await act(async () => {
      await userEvent.setup().click(screen.getByText('pyvenv.cfg'))
    })
    expect(onPick).toHaveBeenCalledWith('/home/me/venvs/app/pyvenv.cfg')
  })

  it('titles the modal as a folder picker in directory mode', async () => {
    await open({ dirsOnly: true })
    expect(screen.getByText('Select a folder · prod-1')).toBeTruthy()
    // "Use this folder" is the primary (and only) selection action here.
    expect(screen.getByRole('button', { name: 'Use this folder' })).toBeTruthy()
  })
})
