/**
 * The Files tab: panes load this computer (workspace) and the selected host,
 * dragging a file across opens the drop dialog with the saved filters, Copy
 * starts the right transfer, Shift-drop skips the dialog, and a finished copy
 * refreshes the destination pane.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, createEvent, waitFor, within, act } from '@testing-library/react'

import { FilesView } from './FilesView'
import { accountNote } from './DropDialog'
import type { Host } from '../../bridge'

const m = vi.hoisted(() => ({
  listDir: vi.fn(),
  startTransfer: vi.fn(),
  startRelay: vi.fn(),
  startFanout: vi.fn(),
  makeDir: vi.fn(),
  viewFile: vi.fn(),
  renamePath: vi.fn(),
  deletePaths: vi.fn(),
  filesAccounts: vi.fn(),
}))

vi.mock('../../bridge', () => ({
  bridge: new Proxy({}, {
    get: (_t, prop) => {
      switch (prop) {
        case 'list_dir': return m.listDir
        case 'start_transfer': return m.startTransfer
        case 'start_relay': return m.startRelay
        case 'start_fanout': return m.startFanout
        case 'make_dir': return m.makeDir
        case 'view_file': return m.viewFile
        case 'rename_path': return m.renamePath
        case 'delete_paths': return m.deletePaths
        case 'files_accounts': return m.filesAccounts
        case 'current_workspace_path': return () => Promise.resolve('/ws')
        case 'get_filters': return () => Promise.resolve([
          { name: 'errors', pipeline: "grep -E 'ERROR|WARN'" },
          { name: 'id-lines', pipeline: 'grep -F {{arg}}', param_label: 'Order id' },
        ])
        case 'get_saved_transfers': return () => Promise.resolve([
          { id: 'daily', title: 'Daily app logs', host: 'app-01', paths: ['/var/log/app/app.log'], direction: 'pull', source: 'app-01', dest: 'workspace' },
        ])
        case 'list_credentials': return () => Promise.resolve([])
        case 'check_dir_exists': return () => Promise.resolve({})
        default: return vi.fn(() => Promise.resolve())
      }
    },
  }),
}))

const HOSTS = [
  { name: 'local', connected: true, runnableCount: 0, groups: [] },
  { name: 'app-01', connected: true, runnableCount: 3, groups: [], group: 'prod' },
  { name: 'app-02', connected: true, runnableCount: 3, groups: [], group: 'prod' },
] as Host[]

function listing(host: string, path: string) {
  if (host === 'local') {
    return { cwd: path || '/home/me', parent: '/', entries: [{ name: 'notes.md', type: 'file', size: 10, mtime: 0 }], truncated: false }
  }
  return {
    cwd: path || '/var/log/app', parent: '/var/log', truncated: false,
    entries: [
      { name: 'archive', type: 'directory', size: 0, mtime: 0 },
      { name: 'app.log', type: 'file', size: 4096, mtime: 0 },
      { name: 'gc.log', type: 'file', size: 100, mtime: 0 },
    ],
  }
}

const listed = (host: string, path: string) => m.listDir.mock.calls.some(c => c[0] === host && c[1] === path)

// jsdom has no DataTransfer; this is enough for our handlers.
function dt() {
  const data: Record<string, string> = {}
  return {
    data,
    get types() { return Object.keys(data) },
    setData: (k: string, v: string) => { data[k] = v },
    getData: (k: string) => data[k] ?? '',
    files: [] as File[],
    effectAllowed: 'all',
    dropEffect: 'none',
  }
}

async function dragFile(name: string, toSide: 'left' | 'right', shift = false) {
  const row = await screen.findByText(name)
  const transfer = dt()
  fireEvent.dragStart(row.closest('[role=option]')!, { dataTransfer: transfer })
  const pane = screen.getByTestId(`pane-${toSide}`)
  fireEvent.dragOver(pane, { dataTransfer: transfer })
  // jsdom has no DragEvent, so modifier keys need setting by hand.
  const ev = createEvent.drop(pane, { dataTransfer: transfer })
  Object.defineProperty(ev, 'shiftKey', { value: shift })
  fireEvent(pane, ev)
}

describe('FilesView', () => {
  beforeEach(() => {
    localStorage.clear()
    m.listDir.mockReset().mockImplementation((host: string, path: string) => Promise.resolve(listing(host, path)))
    m.startTransfer.mockReset().mockResolvedValue({ id: 't1' })
    m.startRelay.mockReset().mockResolvedValue({ id: 't2' })
    m.startFanout.mockReset().mockResolvedValue({ id: 't3' })
    m.makeDir.mockReset().mockResolvedValue({ path: '/var/log/app/new' })
    m.renamePath.mockReset().mockResolvedValue({ path: '/var/log/app/app.old' })
    m.deletePaths.mockReset().mockImplementation((_h: string, paths: string[]) => Promise.resolve({ deleted: paths, failed: [] }))
    m.filesAccounts.mockReset().mockImplementation((h: string) => Promise.resolve(h === 'local' ? { login: '', accounts: [] } : { login: 'ops', accounts: ['svc'] }))
    m.viewFile.mockReset().mockResolvedValue({ size: 10, text: 'line one\nline two', lines: 2, partial: false, binary: false })
  })

  it('opens the workspace on the left and the selected host on the right', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    await waitFor(() => expect(listed('local', '/ws')).toBe(true))
    expect(listed('app-01', '')).toBe(true)
    expect(await screen.findByText('app.log')).toBeTruthy()
    expect(screen.getByText('notes.md')).toBeTruthy()
    expect(screen.getByText('Daily app logs')).toBeTruthy()
  })

  it('drag to this computer opens the drop dialog and copies through a filter', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    await dragFile('app.log', 'left')
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Copy app.log to this computer')).toBeTruthy()
    fireEvent.click(await within(dialog).findByText('errors'))
    fireEvent.change(within(dialog).getByLabelText('Save as name'), { target: { value: 'app-errors.log' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(m.startTransfer).toHaveBeenCalledWith(
      'app-01', ['/var/log/app/app.log'], 'local', '/ws', 'errors', undefined, 'app-errors.log', undefined,
    ))
    // The filter is remembered for this folder.
    expect(localStorage.getItem('runspec.files.lastFilter')).toContain('host:app-01|/var/log/app')

    // A finished copy refreshes the pane showing the destination folder.
    const before = m.listDir.mock.calls.filter(c => c[0] === 'local').length
    act(() => { window.dispatchEvent(new CustomEvent('runspec:transfer_end', { detail: { id: 't1', ok: true, files: 1, total: 1 } })) })
    await waitFor(() => expect(m.listDir.mock.calls.filter(c => c[0] === 'local').length).toBe(before + 1))
    expect(await screen.findByText('Done')).toBeTruthy()
  })

  it('needs the value of a filter that takes one', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    await dragFile('app.log', 'left')
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(await within(dialog).findByText('id-lines'))
    const copy = within(dialog).getByRole('button', { name: 'Copy' }) as HTMLButtonElement
    expect(copy.disabled).toBe(true)
    fireEvent.change(within(dialog).getByLabelText('Order id'), { target: { value: 'ORD-1' } })
    expect(copy.disabled).toBe(false)
  })

  it('Shift-drop copies the whole file with no dialog', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    await dragFile('gc.log', 'left', true)
    await waitFor(() => expect(m.startTransfer).toHaveBeenCalledWith(
      'app-01', ['/var/log/app/gc.log'], 'local', '/ws', undefined, undefined, undefined, undefined,
    ))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('host to host is a relay', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    await screen.findByText('notes.md')
    // Put app-02 in the left pane.
    const leftSelect = within(screen.getByTestId('pane-left')).getByRole('combobox')
    fireEvent.mouseDown(leftSelect)
    fireEvent.click(await screen.findByTitle('app-02'))
    await waitFor(() => expect(listed('app-02', '')).toBe(true))
    await waitFor(() => expect(within(screen.getByTestId('pane-left')).getByText('gc.log')).toBeTruthy())
    const row = within(screen.getByTestId('pane-right')).getByText('app.log')
    const transfer = dt()
    fireEvent.dragStart(row.closest('[role=option]')!, { dataTransfer: transfer })
    fireEvent.drop(screen.getByTestId('pane-left'), { dataTransfer: transfer })
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Relay' }))
    await waitFor(() => expect(m.startRelay).toHaveBeenCalledWith(
      { kind: 'host', host: 'app-01' }, ['/var/log/app/app.log'], 'app-02', '/var/log/app', undefined, undefined, undefined, undefined, undefined,
    ))
  })

  it('relays out of a pane browsing as an owner as that owner', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    await screen.findByText('notes.md')
    fireEvent.mouseDown(within(screen.getByTestId('pane-left')).getByRole('combobox'))
    fireEvent.click(await screen.findByTitle('app-02'))
    await waitFor(() => expect(within(screen.getByTestId('pane-left')).getByText('gc.log')).toBeTruthy())
    await within(screen.getByTestId('pane-right')).findByText('as ops')
    fireEvent.mouseDown(within(screen.getByTestId('pane-right')).getAllByRole('combobox')[1])
    fireEvent.click(await screen.findByTitle('as svc (venv owner)'))
    await waitFor(() => expect(m.listDir.mock.calls.some(c => c[0] === 'app-01' && c[6] === 'svc')).toBe(true))
    const row = within(screen.getByTestId('pane-right')).getByText('app.log')
    const transfer = dt()
    fireEvent.dragStart(row.closest('[role=option]')!, { dataTransfer: transfer })
    fireEvent.drop(screen.getByTestId('pane-left'), { dataTransfer: transfer })
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Reads it as svc; writes it as the SSH login.')).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Relay' }))
    await waitFor(() => expect(m.startRelay).toHaveBeenCalledWith(
      { kind: 'host', host: 'app-01' }, ['/var/log/app/app.log'], 'app-02', '/var/log/app', undefined, undefined, undefined, 'svc', undefined,
    ))
  })

  it('double-clicking a file views its end', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    fireEvent.doubleClick(await screen.findByText('app.log'))
    await waitFor(() => expect(m.viewFile).toHaveBeenCalledWith('app-01', '/var/log/app/app.log', 500, undefined))
    expect(await screen.findByText(/line two/)).toBeTruthy()
  })

  it('creates a folder in the pane', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    await screen.findByText('app.log')
    fireEvent.click(within(screen.getByTestId('pane-right')).getByRole('button', { name: 'New folder' }))
    fireEvent.change(await screen.findByLabelText('New folder name'), { target: { value: 'new' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(m.makeDir).toHaveBeenCalledWith('app-01', '/var/log/app', 'new', undefined))
  })

  it('renames a file after Approve', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    fireEvent.click(await screen.findByText('app.log'))
    fireEvent.click(within(screen.getByTestId('pane-right')).getByRole('button', { name: 'Rename' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/Runs as ops/)).toBeTruthy()
    fireEvent.change(within(dialog).getByLabelText('New name'), { target: { value: 'app.old' } })
    expect(m.renamePath).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(m.renamePath).toHaveBeenCalledWith('app-01', '/var/log/app', 'app.log', 'app.old', undefined))
  })

  it('deletes only after Approve, and Cancel deletes nothing', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    const row = (await screen.findByText('gc.log')).closest('[role=option]')!
    fireEvent.click(row)
    fireEvent.keyDown(row, { key: 'Delete' })
    let dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Delete gc.log on app-01?')).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: /Cancel/ }))
    await waitFor(() => expect(screen.queryByText('Delete gc.log on app-01?')).toBeNull())
    expect(m.deletePaths).not.toHaveBeenCalled()

    fireEvent.click(within(screen.getByTestId('pane-right')).getByRole('button', { name: 'Delete' }))
    dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(m.deletePaths).toHaveBeenCalledWith('app-01', ['/var/log/app/gc.log'], undefined))
  })

  it('browses a host as a venv owner', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    await screen.findByText('app.log')
    await within(screen.getByTestId('pane-right')).findByText('as ops')
    const asSelect = within(screen.getByTestId('pane-right')).getAllByRole('combobox')[1]
    fireEvent.mouseDown(asSelect)
    fireEvent.click(await screen.findByTitle('as svc (venv owner)'))
    await waitFor(() => expect(m.listDir.mock.calls.some(c => c[0] === 'app-01' && c[1] === '/var/log/app' && c[6] === 'svc')).toBe(true))
    fireEvent.doubleClick(await within(screen.getByTestId('pane-right')).findByText('app.log'))
    await waitFor(() => expect(m.viewFile).toHaveBeenCalledWith('app-01', '/var/log/app/app.log', 500, 'svc'))
  })

  it('shows Browse as once discovery finds the venv owners', async () => {
    // The tab opened before discovery: no owners known yet.
    m.filesAccounts.mockResolvedValue({ login: 'ops', accounts: [] })
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    await screen.findByText('app.log')
    await waitFor(() => expect(m.filesAccounts).toHaveBeenCalledWith('app-01'))
    expect(within(screen.getByTestId('pane-right')).queryByText('as ops')).toBeNull()
    m.filesAccounts.mockResolvedValue({ login: 'ops', accounts: ['svc'] })
    act(() => { window.dispatchEvent(new CustomEvent('runspec:runnables_updated', { detail: {} })) })
    expect(await within(screen.getByTestId('pane-right')).findByText('as ops')).toBeTruthy()
  })

  it('copies out of a pane browsing as an owner as that owner', async () => {
    render(<FilesView hosts={HOSTS} selectedHost="app-01" active />)
    await screen.findByText('app.log')
    await within(screen.getByTestId('pane-right')).findByText('as ops')
    fireEvent.mouseDown(within(screen.getByTestId('pane-right')).getAllByRole('combobox')[1])
    fireEvent.click(await screen.findByTitle('as svc (venv owner)'))
    await waitFor(() => expect(m.listDir.mock.calls.some(c => c[0] === 'app-01' && c[6] === 'svc')).toBe(true))
    await dragFile('gc.log', 'left')
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Reads the file as svc.')).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Copy' }))
    await waitFor(() => expect(m.startTransfer).toHaveBeenCalledWith(
      'app-01', ['/var/log/app/gc.log'], 'local', '/ws', undefined, undefined, undefined, 'svc',
    ))
  })
})

describe('accountNote', () => {
  it('says which account each end runs as', () => {
    expect(accountNote('upload', '', 'svc')).toBe('Copies as svc: the file lands owned by svc.')
    expect(accountNote('fanout', undefined, 'svc')).toBe('Copies as svc: the file lands owned by svc.')
    expect(accountNote('download', 'svc')).toBe('Reads the file as svc.')
    expect(accountNote('relay', 'svc', 'app')).toBe('Reads it as svc and writes it as app: the file lands owned by app.')
    expect(accountNote('relay', '', 'app')).toBe('Writes it as app: the file lands owned by app.')
    expect(accountNote('relay')).toBe('')
    expect(accountNote('upload', 'svc')).toBe('')
  })
})
